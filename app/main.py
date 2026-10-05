from __future__ import annotations

import logging
import secrets
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, func, select
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

from .config import get_settings
from .db import get_profile, get_session, init_db, kv_get
from .i18n import APP_STATUSES, translator
from .llm import LLMError, llm_status
from .models import Application, EmailEvent, Job, utcnow
from .services import ai, gmail, linkedin, pipeline
from .services.documents import extract_text
from .sources import ALL_SOURCES

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("jobhunter")
settings = get_settings()
BASE = Path(__file__).parent
scheduler = BackgroundScheduler(timezone="UTC")


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    scheduler.add_job(pipeline.run_search, "interval", hours=settings.search_interval_hours,
                      next_run_time=utcnow() + timedelta(minutes=2), id="search", max_instances=1, coalesce=True)
    scheduler.add_job(pipeline.run_email_sync, "interval", minutes=settings.email_sync_minutes,
                      next_run_time=utcnow() + timedelta(minutes=5), id="email", max_instances=1, coalesce=True)
    scheduler.add_job(linkedin.run_sync, "interval", days=settings.linkedin_sync_days,
                      next_run_time=utcnow() + timedelta(minutes=1), id="linkedin", max_instances=1, coalesce=True)
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="Job Hunter", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")
# Cache-busting token for static assets: changes whenever the stylesheet changes
templates.env.globals["asset_v"] = int((BASE / "static" / "app.css").stat().st_mtime)
OPEN_PATHS = ("/login", "/static", "/healthz")


@app.middleware("http")
async def password_gate(request: Request, call_next):
    if settings.app_password and not request.url.path.startswith(OPEN_PATHS):
        if not request.session.get("auth"):
            return RedirectResponse("/login", status_code=303)
    return await call_next(request)


# Added after the gate so it wraps it (outermost middleware runs first)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, same_site="lax", max_age=60 * 60 * 24 * 30)


def flash(request: Request, msg: str, kind: str = "ok") -> None:
    request.session.setdefault("flash", []).append([kind, msg])


LOCAL_TZ = ZoneInfo(settings.timezone)


def _local(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(LOCAL_TZ)


templates.env.filters["localdate"] = lambda v: _local(v).strftime("%Y.%m.%d.") if v else ""
templates.env.filters["localdt"] = lambda v: _local(v).strftime("%Y.%m.%d. %H:%M") if v else ""


def kv_time(s: Session, key: str) -> str:
    """Background jobs store 'ISO timestamp · stats'; show only the local time."""
    raw = kv_get(s, key).split(" · ")[0]
    try:
        return _local(datetime.fromisoformat(raw)).strftime("%Y.%m.%d. %H:%M")
    except ValueError:
        return ""


def count_where(s: Session, model, *conds) -> int:
    return s.exec(select(func.count()).select_from(model).where(*conds)).one()


def render(request: Request, s: Session, name: str, **ctx) -> HTMLResponse:
    profile = get_profile(s)
    ctx.update(
        request=request,
        t=translator(profile.ui_lang),
        lang=profile.ui_lang,
        profile=profile,
        cfg=settings,
        flashes=request.session.pop("flash", []),
        statuses=APP_STATUSES,
        drafts_count=count_where(s, Application, Application.status == "draft"),
        suggested_count=count_where(s, Job, Job.status == "suggested"),
    )
    return templates.TemplateResponse(request, name, ctx)


def back(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def _referer_path(request: Request, default: str) -> str:
    """Redirect back to the page the form was on (same-origin path only)."""
    from urllib.parse import urlsplit

    ref = urlsplit(request.headers.get("referer", ""))
    if ref.netloc and ref.netloc != request.url.netloc:
        return default
    return (ref.path + (f"?{ref.query}" if ref.query else "")) or default


def _in_background(fn) -> None:
    threading.Thread(target=fn, daemon=True).start()


# ---------------------------------------------------------------- auth

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, s: Session = Depends(get_session)):
    return render(request, s, "login.html")


@app.post("/login")
def login(request: Request, password: str = Form(...)):
    if settings.app_password and secrets.compare_digest(password, settings.app_password):
        request.session["auth"] = True
        return back("/")
    flash(request, "login_failed", "error")
    return back("/login")


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/lang/{code}")
def set_lang(code: str, request: Request, s: Session = Depends(get_session)):
    p = get_profile(s)
    p.ui_lang = code if code in ("hu", "en") else "hu"
    s.add(p)
    s.commit()
    return back(_referer_path(request, "/"))


# ---------------------------------------------------------------- dashboard

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, s: Session = Depends(get_session)):
    counts = dict(s.exec(select(Application.status, func.count()).group_by(Application.status)).all())
    recent = s.exec(select(Application).where(Application.status != "draft")
                    .order_by(Application.updated_at.desc()).limit(6)).all()
    return render(request, s, "dashboard.html", counts=counts, recent=recent,
                  last_search_at=kv_time(s, "last_search"), last_sync_at=kv_time(s, "last_email_sync"),
                  llm=llm_status(), gmail_ok=gmail.connected(), li=linkedin.status(s))


@app.post("/run/search")
def run_search(request: Request):
    _in_background(pipeline.run_search)
    flash(request, "started_bg")
    return back(_referer_path(request, "/"))


@app.post("/run/sync")
def run_sync(request: Request):
    _in_background(pipeline.run_email_sync)
    flash(request, "started_bg")
    return back(_referer_path(request, "/applications"))


# ---------------------------------------------------------------- jobs

@app.get("/jobs", response_class=HTMLResponse)
def jobs(request: Request, show: str = "suggested", s: Session = Depends(get_session)):
    q = select(Job)
    if show != "all":
        q = q.where(Job.status == show)
    rows = s.exec(q.order_by(Job.score.desc(), Job.fetched_at.desc()).limit(200)).all()
    tab_counts = dict(s.exec(select(Job.status, func.count()).group_by(Job.status)).all())
    tab_counts["all"] = sum(tab_counts.values())
    return render(request, s, "jobs.html", jobs=rows, show=show, tab_counts=tab_counts)


@app.post("/jobs/{job_id}/prepare")
def prepare(job_id: int, request: Request, s: Session = Depends(get_session)):
    job = s.get(Job, job_id) or _404()
    try:
        a = pipeline.prepare_draft(s, get_profile(s), job)
    except LLMError as e:
        flash(request, str(e), "error")
        return back("/jobs")
    return back(f"/drafts#app-{a.id}")


@app.post("/jobs/{job_id}/dismiss")
def dismiss(job_id: int, s: Session = Depends(get_session)):
    job = s.get(Job, job_id) or _404()
    job.status = "dismissed"
    s.add(job)
    s.commit()
    return back("/jobs")


# ---------------------------------------------------------------- approval queue

@app.get("/drafts", response_class=HTMLResponse)
def drafts(request: Request, s: Session = Depends(get_session)):
    rows = s.exec(select(Application).where(Application.status == "draft").order_by(Application.created_at.desc())).all()
    jobs_by_id = {j.id: j for j in s.exec(select(Job).where(Job.id.in_([a.job_id for a in rows if a.job_id]))).all()}
    return render(request, s, "drafts.html", apps=rows, jobs=jobs_by_id, gmail_ok=gmail.connected())


EDITABLE_FIELDS = ("status", "cover_letter_subject", "cover_letter", "salary_expectation", "contact_name",
                   "contact_email", "contact_phone", "language", "notes")


async def _save_form(request: Request, s: Session, a: Application) -> str:
    """Apply the editable fields present in the posted form; returns the 'next' redirect target."""
    form = await request.form()
    for k in EDITABLE_FIELDS:
        v = form.get(k)
        if isinstance(v, str):
            setattr(a, k, v if k == "cover_letter" else v.strip())
    if form.get("status") == "applied" and not a.applied_at:
        a.applied_at = utcnow()
    a.updated_at = utcnow()
    s.add(a)
    s.commit()
    nxt = str(form.get("next") or "/applications")
    return nxt if nxt.startswith("/") else "/applications"


@app.post("/applications/{app_id}/update")
async def update_application(app_id: int, request: Request, s: Session = Depends(get_session)):
    a = s.get(Application, app_id) or _404()
    nxt = await _save_form(request, s, a)
    flash(request, "saved")
    return back(nxt)


@app.post("/applications/{app_id}/regenerate")
async def regenerate(app_id: int, request: Request, s: Session = Depends(get_session)):
    a = s.get(Application, app_id) or _404()
    await _save_form(request, s, a)  # keep edits such as a changed letter language
    job = s.get(Job, a.job_id) if a.job_id else None
    if not job:
        flash(request, "no_job_linked", "error")
        return back("/drafts")
    try:
        letter = await run_in_threadpool(ai.write_cover_letter, get_profile(s), job, a.language, a.salary_expectation)
    except LLMError as e:
        flash(request, str(e), "error")
        return back(f"/drafts#app-{a.id}")
    a.cover_letter_subject, a.cover_letter = letter.subject, letter.body
    s.add(a)
    s.commit()
    flash(request, "letter_rewritten")
    return back(f"/drafts#app-{a.id}")


@app.post("/applications/{app_id}/send")
async def send_application(app_id: int, request: Request, s: Session = Depends(get_session)):
    """Explicit user approval: save the reviewed draft, then send it through Gmail."""
    a = s.get(Application, app_id) or _404()
    await _save_form(request, s, a)
    if not a.contact_email:
        flash(request, "missing_recipient", "error")
        return back(f"/drafts#app-{a.id}")
    p = get_profile(s)
    cv = settings.upload_dir / p.cv_filename if p.cv_filename else None
    try:
        thread_id = await run_in_threadpool(gmail.send, a.contact_email, a.cover_letter_subject, a.cover_letter, cv)
    except Exception as e:
        log.exception("send failed")
        flash(request, f"Gmail: {e}", "error")
        return back(f"/drafts#app-{a.id}")
    pipeline.mark_applied(a, "email", thread_id)
    _close_job(s, a)
    s.add(a)
    s.commit()
    flash(request, "sent_ok")
    return back("/applications")


@app.post("/applications/{app_id}/manual")
async def mark_manual(app_id: int, request: Request, s: Session = Depends(get_session)):
    a = s.get(Application, app_id) or _404()
    await _save_form(request, s, a)
    pipeline.mark_applied(a, "manual")
    _close_job(s, a)
    s.add(a)
    s.commit()
    flash(request, "marked_applied")
    return back("/applications")


@app.post("/applications/{app_id}/delete")
def delete_application(app_id: int, s: Session = Depends(get_session)):
    a = s.get(Application, app_id) or _404()
    if a.job_id and (job := s.get(Job, a.job_id)):
        job.status = "dismissed"
        s.add(job)
    for ev in s.exec(select(EmailEvent).where(EmailEvent.application_id == a.id)).all():
        ev.application_id = None
        s.add(ev)
    s.delete(a)
    s.commit()
    return back("/drafts")


def _close_job(s: Session, a: Application) -> None:
    if a.job_id and (job := s.get(Job, a.job_id)):
        job.status = "applied"
        s.add(job)


# ---------------------------------------------------------------- tracker

@app.get("/applications", response_class=HTMLResponse)
def tracker(request: Request, status: str = "", s: Session = Depends(get_session)):
    q = select(Application).where(Application.status != "draft")
    if status:
        q = q.where(Application.status == status)
    rows = s.exec(q.order_by(Application.updated_at.desc())).all()
    status_counts = dict(s.exec(select(Application.status, func.count())
                                .where(Application.status != "draft").group_by(Application.status)).all())
    return render(request, s, "applications.html", apps=rows, filter=status, status_counts=status_counts,
                  last_sync_at=kv_time(s, "last_email_sync"), gmail_ok=gmail.connected())


@app.get("/applications/{app_id}", response_class=HTMLResponse)
def application_detail(app_id: int, request: Request, s: Session = Depends(get_session)):
    a = s.get(Application, app_id) or _404()
    events = s.exec(select(EmailEvent).where(EmailEvent.application_id == a.id)
                    .order_by(EmailEvent.received_at.desc())).all()
    job = s.get(Job, a.job_id) if a.job_id else None
    return render(request, s, "application_detail.html", a=a, events=events, job=job)


@app.post("/applications/new")
def add_manual(
    request: Request,
    company: str = Form(...), position: str = Form(...), job_url: str = Form(""),
    salary_expectation: str = Form(""), contact_phone: str = Form(""), contact_email: str = Form(""),
    s: Session = Depends(get_session),
):
    a = Application(company=company.strip(), position=position.strip(), job_url=job_url.strip(),
                    salary_expectation=salary_expectation.strip(), contact_phone=contact_phone.strip(),
                    contact_email=contact_email.strip())
    pipeline.mark_applied(a, "manual")
    s.add(a)
    s.commit()
    flash(request, "added")
    return back("/applications")


# ---------------------------------------------------------------- profile

@app.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request, s: Session = Depends(get_session)):
    return render(request, s, "profile.html")


@app.post("/profile")
def save_profile(
    request: Request,
    full_name: str = Form(""), email: str = Form(""), phone: str = Form(""), location: str = Form(""),
    search_keywords: str = Form(""), salary_huf: str = Form(""), salary_eur: str = Form(""),
    min_score: int = Form(65), notes: str = Form(""), profiles_text: str = Form(""),
    want_remote: bool = Form(False), want_hungary: bool = Form(False),
    s: Session = Depends(get_session),
):
    p = get_profile(s)
    for k, v in dict(full_name=full_name, email=email, phone=phone, location=location,
                     search_keywords=search_keywords, salary_huf=salary_huf, salary_eur=salary_eur,
                     notes=notes, profiles_text=profiles_text).items():
        setattr(p, k, v.strip())
    p.min_score = max(0, min(100, min_score))
    p.want_remote, p.want_hungary = want_remote, want_hungary
    s.add(p)
    s.commit()
    flash(request, "saved")
    return back("/profile")


@app.post("/profile/upload")
async def upload(request: Request, kind: str = Form(...), file: UploadFile = File(...),
                 s: Session = Depends(get_session)):
    data = await file.read()
    if len(data) > 10 * 1024 * 1024:
        flash(request, "File too large (max 10 MB)", "error")
        return back("/profile")
    safe_name = Path(file.filename or "upload.txt").name
    text = extract_text(safe_name, data)
    p = get_profile(s)
    if kind == "cv":
        (settings.upload_dir / safe_name).write_bytes(data)
        p.cv_filename, p.cv_text = safe_name, text
    else:
        p.profiles_text = (p.profiles_text + "\n\n" + text).strip()
    s.add(p)
    s.commit()
    if kind == "cv" and not p.search_keywords:
        return await analyze(request, s)
    return back("/profile")


@app.post("/profile/analyze")
async def analyze(request: Request, s: Session = Depends(get_session)):
    p = get_profile(s)
    try:
        ins = ai.analyze_profile(p)
    except LLMError as e:
        flash(request, str(e), "error")
        return back("/profile")
    p.search_keywords = ", ".join(ins.search_keywords)
    s.add(p)
    s.commit()
    flash(request, ins.summary)
    return back("/profile")


# ---------------------------------------------------------------- settings / gmail

@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, s: Session = Depends(get_session)):
    gmail_email = ""
    if gmail.connected():
        try:
            gmail_email = gmail.account_email()
        except Exception as e:
            gmail_email = f"error: {e}"
    return render(request, s, "settings.html", llm=llm_status(), gmail_configured=gmail.configured(),
                  gmail_email=gmail_email, sources=[(x.name, x.enabled()) for x in ALL_SOURCES],
                  li=linkedin.status(s), li_last_sync_at=kv_time(s, linkedin.SYNC_KEY))


# ---------------------------------------------------------------- linkedin

@app.post("/linkedin/token")
def linkedin_token(request: Request, token: str = Form(...), s: Session = Depends(get_session)):
    linkedin.set_token(s, token)
    return linkedin_sync(request, s)


@app.post("/linkedin/sync")
def linkedin_sync(request: Request, s: Session = Depends(get_session)):
    stats = linkedin.run_sync()
    if stats["errors"]:
        flash(request, "; ".join(stats["errors"]), "error")
        return back("/settings")
    flash(request, f"LinkedIn: {stats['records']} records / {stats['domains']} sections, "
                   f"+{stats['applications_added']} applications")
    s.expire_all()
    p = get_profile(s)
    if p.linkedin_text and not p.search_keywords:
        try:
            ins = ai.analyze_profile(p)
            p.search_keywords = ", ".join(ins.search_keywords)
            s.add(p)
            s.commit()
        except LLMError as e:
            flash(request, str(e), "error")
    return back("/settings")


@app.post("/linkedin/disconnect")
def linkedin_disconnect(s: Session = Depends(get_session)):
    linkedin.set_token(s, "")
    return back("/settings")


@app.get("/gmail/connect")
def gmail_connect():
    if not gmail.configured():
        return back("/settings")
    return RedirectResponse(gmail.auth_url(), status_code=303)


@app.get("/gmail/callback")
def gmail_callback(request: Request):
    callback = f"{settings.base_url.rstrip('/')}/gmail/callback?{request.url.query}"
    try:
        gmail.finish_auth(callback)
    except Exception as e:
        log.exception("gmail oauth failed")
        flash(request, f"Gmail: {e}", "error")
    return back("/settings")


@app.post("/gmail/disconnect")
def gmail_disconnect():
    gmail.disconnect()
    return back("/settings")


def _404():
    raise HTTPException(status_code=404)

"""Background jobs: search -> prefilter -> LLM score -> draft; and Gmail status sync."""
from __future__ import annotations

import logging
import re
import threading

from sqlmodel import Session, select

from ..config import get_settings
from ..db import get_profile, kv_set, session_scope
from ..llm import LLMError
from ..models import Application, EmailEvent, Job, Profile, utcnow
from ..sources import ALL_SOURCES, JobIn
from ..sources.base import find_phone, letter_language
from . import ai, gmail

log = logging.getLogger(__name__)
_lock = threading.Lock()

# Status order: an email can only move an application forward (except rejection/offer).
STATUS_RANK = {"draft": 0, "applied": 1, "received": 2, "interview": 3, "offer": 4, "rejected": 4, "withdrawn": 4}


# ---------------------------------------------------------------- search

def prefilter_score(profile: Profile, job: JobIn | Job) -> int:
    """Cheap keyword overlap so only plausible jobs are sent to the LLM."""
    title = job.title.lower()
    text = f"{title} {job.description[:4000].lower()}"
    score = 0
    for kw in profile.keywords():
        k = kw.lower()
        if k in title:
            score += 40
        else:
            words = [w for w in re.split(r"\W+", k) if len(w) > 2]
            score += sum(10 for w in words if w in title)
            score += sum(3 for w in words if w in text)
    return min(score, 100)


def _upsert(s: Session, profile: Profile, j: JobIn) -> bool:
    exists = s.exec(select(Job).where(Job.source == j.source, Job.external_id == j.external_id)).first()
    if exists:
        return False
    s.add(
        Job(
            source=j.source,
            external_id=j.external_id,
            title=j.title,
            company=j.company,
            location=j.location,
            is_remote=j.is_remote,
            country=j.country,
            url=j.url,
            apply_email=j.apply_email,
            description=j.description,
            salary_text=j.salary_text,
            posted_at=j.posted_at,
            prefilter_score=prefilter_score(profile, j),
            language=letter_language(j.country, j.location, j.is_remote),
        )
    )
    return True


def run_search() -> dict:
    if not _lock.acquire(blocking=False):
        return {"skipped": "already running"}
    try:
        return _run_search()
    finally:
        _lock.release()


def _run_search() -> dict:
    settings = get_settings()
    stats = {"fetched": 0, "new": 0, "scored": 0, "suggested": 0, "drafts": 0, "errors": []}
    with session_scope() as s:
        profile = get_profile(s)
        keywords = profile.keywords()
        if not keywords:
            stats["errors"].append("No search keywords – upload a CV on the Profile page first.")
            return stats
        for src in ALL_SOURCES:
            if not src.enabled():
                continue
            try:
                jobs = src.fetch(keywords, want_remote=profile.want_remote, want_hungary=profile.want_hungary)
            except Exception as e:  # one broken source must not stop the others
                log.exception("source %s failed", src.name)
                stats["errors"].append(f"{src.name}: {e}")
                continue
            stats["fetched"] += len(jobs)
            for j in jobs:
                if j.external_id and _upsert(s, profile, j):
                    stats["new"] += 1
            s.commit()

        # LLM scoring of the most promising unscored jobs
        candidates = s.exec(
            select(Job)
            .where(Job.status == "new", Job.prefilter_score >= 10)
            .order_by(Job.prefilter_score.desc(), Job.fetched_at.desc())
            .limit(settings.max_llm_scores_per_run)
        ).all()
        for job in candidates:
            try:
                res = ai.score_job(profile, job)
            except LLMError as e:
                stats["errors"].append(str(e))
                break
            job.score, job.score_reason = res.score, res.reason
            job.apply_email = job.apply_email or res.contact_email
            job.status = "suggested" if res.score >= profile.min_score else "low"
            s.add(job)
            s.commit()
            stats["scored"] += 1
            if job.status == "suggested":
                stats["suggested"] += 1
                if settings.auto_prepare_drafts and stats["drafts"] < settings.max_drafts_per_run:
                    try:
                        prepare_draft(s, profile, job, res)
                        stats["drafts"] += 1
                    except LLMError as e:
                        stats["errors"].append(str(e))
        # Jobs with no keyword overlap at all are not worth an LLM call
        for job in s.exec(select(Job).where(Job.status == "new", Job.prefilter_score < 10)).all():
            job.status = "low"
            s.add(job)
        s.commit()
        kv_set(s, "last_search", f"{utcnow().isoformat(timespec='minutes')} · {stats}")
    return stats


def prepare_draft(s: Session, profile: Profile, job: Job, match: ai.MatchResult | None = None) -> Application:
    existing = s.exec(select(Application).where(Application.job_id == job.id)).first()
    if existing:
        return existing
    salary = profile.salary_huf if job.language == "hu" else (profile.salary_eur or profile.salary_huf)
    letter = ai.write_cover_letter(profile, job, job.language, salary)
    app = Application(
        job_id=job.id,
        company=job.company,
        position=job.title,
        job_url=job.url,
        language=job.language,
        cover_letter_subject=letter.subject,
        cover_letter=letter.body,
        salary_expectation=salary,
        contact_name=match.contact_name if match else "",
        contact_email=job.apply_email or (match.contact_email if match else ""),
        contact_phone=(match.contact_phone if match else "") or find_phone(job.description),
        method="email" if job.apply_email else "manual",
    )
    job.status = "drafted"
    s.add(job)
    s.add(app)
    s.commit()
    s.refresh(app)
    return app


# ---------------------------------------------------------------- email sync

def _domain(email: str) -> str:
    return email.split("@")[-1].lower() if "@" in email else ""


def _norm(name: str) -> str:
    name = name.lower()
    name = re.sub(r"\b(kft|zrt|nyrt|bt|ltd|llc|inc|gmbh|plc|co|corp|group|hungary)\b\.?", "", name)
    return re.sub(r"[^a-z0-9áéíóöőúüű]", "", name)


GENERIC_DOMAINS = {"gmail.com", "outlook.com", "hotmail.com", "yahoo.com", "freemail.hu", "citromail.hu"}
JOB_WORDS = re.compile(
    r"application|applied|interview|candidate|position|recruit|hiring|offer|unfortunately|"
    r"jelentkez|pályáz|interjú|állás|pozíció|munkakör|ajánlat|sajnos|felvétel",
    re.I,
)


def match_application(mail: gmail.Mail, apps: list[Application]) -> Application | None:
    for a in apps:
        if a.gmail_thread_id and a.gmail_thread_id == mail.thread_id:
            return a
    dom = _domain(mail.sender_email)
    for a in apps:
        if a.contact_email and dom and dom not in GENERIC_DOMAINS and _domain(a.contact_email) == dom:
            return a
    hay = _norm(f"{mail.sender} {dom} {mail.subject}")
    best = None
    for a in apps:
        key = _norm(a.company)
        if len(key) >= 3 and key in hay:
            if best is None or len(key) > len(_norm(best.company)):
                best = a
    return best


def run_email_sync() -> dict:
    stats = {"checked": 0, "matched": 0, "updated": 0, "errors": []}
    if not gmail.connected():
        stats["errors"].append("Gmail not connected")
        return stats
    settings = get_settings()
    with session_scope() as s:
        apps = s.exec(select(Application).where(Application.status != "draft")).all()
        if not apps:
            return stats
        known = set(s.exec(select(EmailEvent.gmail_id)).all())
        try:
            mails = gmail.list_recent(settings.email_lookback_days, known)
        except Exception as e:
            log.exception("gmail list failed")
            stats["errors"].append(f"gmail: {e}")
            return stats
        companies = [a.company for a in apps]
        for mail in mails:
            stats["checked"] += 1
            app = match_application(mail, apps)
            event = EmailEvent(
                gmail_id=mail.id,
                thread_id=mail.thread_id,
                from_addr=f"{mail.sender} <{mail.sender_email}>",
                subject=mail.subject,
                snippet=mail.snippet[:500],
                received_at=mail.received_at,
            )
            # Only spend LLM calls on mails that are plausibly about an application.
            if app is None and not JOB_WORDS.search(f"{mail.subject} {mail.snippet}"):
                s.add(event)
                continue
            try:
                cls = ai.classify_email(mail.sender, mail.subject, mail.body or mail.snippet, companies)
            except LLMError as e:
                stats["errors"].append(str(e))
                break
            if app is None and cls.is_job_related and cls.company:
                key = _norm(cls.company)
                app = next((a for a in apps if key and _norm(a.company) == key), None)
            if app and cls.is_job_related:
                stats["matched"] += 1
                event.application_id = app.id
                event.detected_status = cls.status
                changed = _apply_email_update(app, mail, cls)
                stats["updated"] += int(changed)
                s.add(app)
            s.add(event)
            s.commit()
        s.commit()
        kv_set(s, "last_email_sync", f"{utcnow().isoformat(timespec='minutes')} · {stats}")
    return stats


def _apply_email_update(app: Application, mail: gmail.Mail, cls: ai.EmailClassification) -> bool:
    changed = False
    if cls.status in STATUS_RANK and STATUS_RANK[cls.status] >= STATUS_RANK.get(app.status, 0) and cls.status != app.status:
        app.status = cls.status
        changed = True
    phone = cls.contact_phone or find_phone(mail.body)
    if phone and not app.contact_phone:
        app.contact_phone = phone
        changed = True
    if cls.contact_name and not app.contact_name:
        app.contact_name = cls.contact_name
    if not app.contact_email and _domain(mail.sender_email) not in GENERIC_DOMAINS:
        app.contact_email = mail.sender_email
    if mail.received_at and (not app.last_email_at or mail.received_at > app.last_email_at):
        app.last_email_at = mail.received_at
    app.updated_at = utcnow()
    return changed


def mark_applied(app: Application, method: str, thread_id: str = "") -> None:
    app.status = "applied"
    app.method = method
    app.applied_at = utcnow()
    app.updated_at = utcnow()
    if thread_id:
        app.gmail_thread_id = thread_id

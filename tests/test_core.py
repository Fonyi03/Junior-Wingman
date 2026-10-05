from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.models import Application, Profile
from app.services import gmail, pipeline
from app.sources.base import find_apply_email, find_phone, html_to_text, letter_language
from app.sources.providers import parse_arbeitnow, parse_jsearch, parse_remoteok, parse_remotive


def test_letter_language_rule():
    assert letter_language("HU", "", False) == "hu"
    assert letter_language("", "Budapest, Hungary", False) == "hu"
    assert letter_language("", "Remote – Europe", True) == "en"
    assert letter_language("DE", "Berlin", False) == "en"


def test_html_and_contact_extraction():
    text = html_to_text("<p>Send your CV to <b>karrier@acme.hu</b> or call +36 30 123 4567</p>")
    assert "karrier@acme.hu" in text
    assert find_apply_email(text) == "karrier@acme.hu"
    assert find_apply_email("contact john.doe@acme.hu") == ""  # personal address, not an application inbox
    assert find_phone(text).startswith("+36 30 123")


def test_parsers():
    r = parse_remotive({"id": 1, "title": "Python Dev", "company_name": "A", "url": "u",
                        "candidate_required_location": "Europe", "description": "<p>x</p>",
                        "publication_date": "2026-10-01T10:00:00"})
    assert r.is_remote and r.description == "x" and r.posted_at == datetime(2026, 10, 1, 10, tzinfo=timezone.utc)
    a = parse_arbeitnow({"slug": "s", "title": "T", "company_name": "C", "location": "Budapest",
                         "remote": False, "url": "u", "description": "d", "created_at": 1700000000})
    assert a.country == "HU"
    ok = parse_remoteok({"id": 5, "position": "DevOps", "company": "X", "salary_min": 50000, "salary_max": 70000})
    assert ok.salary_text == "$50,000 – $70,000"
    js = parse_jsearch({"job_id": "j1", "job_title": "Backend", "employer_name": "E", "job_city": "Budapest",
                        "job_country": "hu", "job_is_remote": False, "job_description": "jobs@e.hu"})
    assert js.country == "HU" and js.apply_email == "jobs@e.hu"


def test_prefilter():
    p = Profile(search_keywords="Python developer, DevOps engineer")
    job = parse_remotive({"id": 2, "title": "Senior Python Developer", "description": "Django"})
    other = parse_remotive({"id": 3, "title": "Sales Manager", "description": "CRM"})
    assert pipeline.prefilter_score(p, job) >= 40
    assert pipeline.prefilter_score(p, other) < 10


def _mail(**kw):
    base = dict(id="m", thread_id="t", sender="", sender_email="", subject="", snippet="", body="", received_at=None)
    base.update(kw)
    return gmail.Mail(**base)


def test_email_matching():
    apps = [
        Application(id=1, company="Acme Kft.", contact_email="hr@acme.hu", gmail_thread_id="T1"),
        Application(id=2, company="Globex Hungary Zrt", contact_email=""),
    ]
    assert pipeline.match_application(_mail(thread_id="T1"), apps).id == 1
    assert pipeline.match_application(_mail(sender_email="noreply@acme.hu"), apps).id == 1
    assert pipeline.match_application(_mail(sender="Globex Talent", sender_email="x@greenhouse.io"), apps).id == 2
    assert pipeline.match_application(_mail(sender_email="friend@gmail.com", subject="hi"), apps) is None


def test_status_only_moves_forward():
    from app.services.ai import EmailClassification

    a = Application(status="interview")
    cls = EmailClassification(is_job_related=True, company="", status="received")
    pipeline._apply_email_update(a, _mail(), cls)
    assert a.status == "interview"
    cls.status = "rejected"
    pipeline._apply_email_update(a, _mail(body="Hívjon: +36 1 234 5678"), cls)
    assert a.status == "rejected" and a.contact_phone


def test_pages_render():
    from app.main import app

    with TestClient(app) as c:
        for path in ["/", "/jobs", "/drafts", "/applications", "/profile", "/settings", "/healthz"]:
            assert c.get(path).status_code == 200, path
        r = c.post("/applications/new", data={"company": "Acme", "position": "Dev", "salary_expectation": "1M Ft",
                                               "contact_phone": "+36 30 111 2222"}, follow_redirects=True)
        assert r.status_code == 200 and "Acme" in r.text and "+36 30 111 2222" in r.text
        c.get("/lang/en")
        assert "My applications" in c.get("/applications").text


def test_datetimes_roundtrip_timezone_aware():
    from sqlmodel import select

    from app.db import init_db, session_scope

    init_db()
    with session_scope() as s:
        s.add(Application(company="TZ", applied_at=datetime(2026, 1, 1, tzinfo=timezone.utc)))
        s.commit()
        a = s.exec(select(Application).where(Application.company == "TZ")).first()
        assert a.applied_at.tzinfo is not None
        assert a.created_at < datetime.now(timezone.utc)

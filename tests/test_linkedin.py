import httpx
import pytest
from sqlalchemy import create_engine, inspect, text

from app import db
from app.models import Application
from app.services import linkedin


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_domain_follows_pagination_until_no_data():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["Linkedin-Version"] == "202312"
        assert req.headers["Authorization"] == "Bearer tok"
        start = int(req.url.params["start"])
        calls.append(start)
        if start < 2:
            return httpx.Response(200, json={
                "paging": {"links": [{"rel": "next", "href": "..."}]},
                "elements": [{"snapshotDomain": "POSITIONS", "snapshotData": [{"Title": f"Dev {start}"}]}],
            })
        return httpx.Response(404, json={"message": "No data found for this memberId"})

    with _client(handler) as c:
        recs = linkedin.fetch_domain(c, "tok", "POSITIONS")
    assert [r["Title"] for r in recs] == ["Dev 0", "Dev 1"]
    assert calls == [0, 1, 2]


def test_fetch_domain_stops_without_next_link():
    def handler(req):
        return httpx.Response(200, json={"paging": {"links": []},
                                         "elements": [{"snapshotData": [{"Name": "Python"}]}]})

    with _client(handler) as c:
        assert linkedin.fetch_domain(c, "tok", "SKILLS") == [{"Name": "Python"}]


def test_expired_token_raises():
    with _client(lambda req: httpx.Response(401, json={})) as c:
        with pytest.raises(linkedin.LinkedInAuthError):
            linkedin.fetch_domain(c, "tok", "PROFILE")


def test_format_profile_skips_empty_fields():
    text_out = linkedin.format_profile({
        "PROFILE": [{"First Name": "Anna", "Headline": "DevOps Engineer", "Twitter Handles": ""}],
        "SKILLS": [{"Name": "Python"}, {"Name": "Docker"}],
        "HONORS": [],
    })
    assert "## Profile" in text_out and "Headline: DevOps Engineer" in text_out
    assert "Twitter" not in text_out and "Honors" not in text_out
    assert "- Name: Docker" in text_out


def test_import_job_applications_dedupes():
    db.init_db()
    recs = [
        {"Application Date": "10/1/26, 9:15 AM", "Company Name": "LiCo", "Job Title": "SRE",
         "Job Url": "https://www.linkedin.com/jobs/view/1", "Contact Phone Number": "+36 1 555 0000"},
        {"Company Name": "LiCo", "Job Title": "SRE"},  # duplicate
        {"Company Name": "", "Job Title": "No company"},  # unusable
    ]
    with db.session_scope() as s:
        assert linkedin.import_job_applications(s, recs) == 1
        s.commit()
        assert linkedin.import_job_applications(s, recs) == 0
        a = s.exec(db.select(Application).where(Application.company == "LiCo")).one()
        assert a.method == "linkedin" and a.status == "applied" and a.contact_phone
        assert a.applied_at.year == 2026 and a.applied_at.month == 10


def test_migration_adds_missing_column(monkeypatch, tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with eng.begin() as conn:  # a profile table from before linkedin_text existed
        conn.execute(text("CREATE TABLE profile (id INTEGER PRIMARY KEY, full_name VARCHAR NOT NULL)"))
    monkeypatch.setattr(db, "engine", eng)
    db._add_missing_columns()
    cols = {c["name"] for c in inspect(eng).get_columns("profile")}
    assert {"linkedin_text", "want_remote", "min_score"} <= cols


def test_settings_page_shows_linkedin_block():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        r = c.get("/settings")
        assert r.status_code == 200 and "r_dma_portability_self_serve" in r.text

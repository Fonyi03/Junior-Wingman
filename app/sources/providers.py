"""Job sources that use public APIs or licensed aggregators.

LinkedIn / Glassdoor are NOT scraped directly (their terms forbid it). Their
listings are reached through JSearch (Google for Jobs aggregator) instead.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from ..config import get_settings
from .base import USER_AGENT, JobIn, JobSource

log = logging.getLogger(__name__)


def _client() -> httpx.Client:
    return httpx.Client(timeout=30, headers={"User-Agent": USER_AGENT}, follow_redirects=True)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _matches(job: JobIn, keywords: list[str]) -> bool:
    if not keywords:
        return True
    hay = f"{job.title} {' '.join(job.tags)}".lower()
    return any(k.lower() in hay for k in keywords)


class Remotive(JobSource):
    name = "remotive"

    def enabled(self) -> bool:
        return get_settings().enable_remotive

    def fetch(self, keywords, *, want_remote, want_hungary):
        if not want_remote:
            return []
        out: dict[str, JobIn] = {}
        with _client() as c:
            for kw in keywords[:6] or [""]:
                r = c.get("https://remotive.com/api/remote-jobs", params={"search": kw, "limit": 50})
                r.raise_for_status()
                for j in r.json().get("jobs", []):
                    out[str(j["id"])] = parse_remotive(j)
        return list(out.values())


def parse_remotive(j: dict) -> JobIn:
    return JobIn(
        source="remotive",
        external_id=str(j["id"]),
        title=j.get("title", ""),
        company=j.get("company_name", ""),
        location=j.get("candidate_required_location", "") or "Remote",
        is_remote=True,
        url=j.get("url", ""),
        description=j.get("description", ""),
        salary_text=j.get("salary", "") or "",
        posted_at=_parse_iso(j.get("publication_date")),
        tags=j.get("tags", []) or [],
    ).finalize()


class Arbeitnow(JobSource):
    name = "arbeitnow"

    def enabled(self) -> bool:
        return get_settings().enable_arbeitnow

    def fetch(self, keywords, *, want_remote, want_hungary):
        jobs: list[JobIn] = []
        with _client() as c:
            for page in (1, 2, 3):
                r = c.get("https://www.arbeitnow.com/api/job-board-api", params={"page": page})
                r.raise_for_status()
                for j in r.json().get("data", []):
                    job = parse_arbeitnow(j)
                    if not _matches(job, keywords):
                        continue
                    if (job.is_remote and want_remote) or (job.country == "HU" and want_hungary):
                        jobs.append(job)
        return jobs


def parse_arbeitnow(j: dict) -> JobIn:
    created = j.get("created_at")
    return JobIn(
        source="arbeitnow",
        external_id=j.get("slug", ""),
        title=j.get("title", ""),
        company=j.get("company_name", ""),
        location=j.get("location", ""),
        is_remote=bool(j.get("remote")),
        url=j.get("url", ""),
        description=j.get("description", ""),
        posted_at=datetime.fromtimestamp(created, timezone.utc) if created else None,
        tags=j.get("tags", []) or [],
    ).finalize()


class RemoteOK(JobSource):
    """RemoteOK API terms require linking back to RemoteOK; the UI shows the source link."""

    name = "remoteok"

    def enabled(self) -> bool:
        return get_settings().enable_remoteok

    def fetch(self, keywords, *, want_remote, want_hungary):
        if not want_remote:
            return []
        with _client() as c:
            r = c.get("https://remoteok.com/api")
            r.raise_for_status()
            data = [j for j in r.json() if isinstance(j, dict) and j.get("id")]
        jobs = [parse_remoteok(j) for j in data]
        return [j for j in jobs if _matches(j, keywords)]


def parse_remoteok(j: dict) -> JobIn:
    salary = ""
    if j.get("salary_min") and j.get("salary_max"):
        salary = f"${j['salary_min']:,} – ${j['salary_max']:,}"
    return JobIn(
        source="remoteok",
        external_id=str(j["id"]),
        title=j.get("position", ""),
        company=j.get("company", ""),
        location=j.get("location", "") or "Remote",
        is_remote=True,
        url=j.get("url", "") or j.get("apply_url", ""),
        description=j.get("description", ""),
        salary_text=salary,
        posted_at=_parse_iso(j.get("date")),
        tags=j.get("tags", []) or [],
    ).finalize()


class JSearch(JobSource):
    """Google for Jobs aggregator on RapidAPI: includes LinkedIn, Indeed, Glassdoor, Profession.hu listings."""

    name = "jsearch"

    def enabled(self) -> bool:
        return bool(get_settings().jsearch_api_key)

    def fetch(self, keywords, *, want_remote, want_hungary):
        key = get_settings().jsearch_api_key
        headers = {"X-RapidAPI-Key": key, "X-RapidAPI-Host": "jsearch.p.rapidapi.com"}
        queries: list[dict] = []
        # Free tier is ~200 requests/month: keep the query count small.
        for kw in keywords[:3]:
            if want_hungary:
                queries.append({"query": f"{kw} in Hungary", "country": "hu", "date_posted": "week"})
            if want_remote:
                queries.append({"query": f"{kw} remote", "remote_jobs_only": "true", "date_posted": "week"})
        out: dict[str, JobIn] = {}
        with _client() as c:
            for q in queries:
                r = c.get("https://jsearch.p.rapidapi.com/search", params=q, headers=headers)
                if r.status_code == 429:
                    log.warning("JSearch quota exhausted")
                    break
                r.raise_for_status()
                for j in r.json().get("data", []):
                    out[j["job_id"]] = parse_jsearch(j)
        return list(out.values())


def parse_jsearch(j: dict) -> JobIn:
    loc = ", ".join(x for x in (j.get("job_city"), j.get("job_country")) if x)
    salary = ""
    if j.get("job_min_salary") and j.get("job_max_salary"):
        salary = f"{j['job_min_salary']:,}–{j['job_max_salary']:,} {j.get('job_salary_currency') or ''} / {j.get('job_salary_period') or ''}"
    return JobIn(
        source="jsearch",
        external_id=j["job_id"],
        title=j.get("job_title", ""),
        company=j.get("employer_name", ""),
        location=loc or ("Remote" if j.get("job_is_remote") else ""),
        is_remote=bool(j.get("job_is_remote")),
        country=(j.get("job_country") or "").upper(),
        url=j.get("job_apply_link", ""),
        description=j.get("job_description", ""),
        salary_text=salary.strip(" /"),
        posted_at=_parse_iso(j.get("job_posted_at_datetime_utc")),
    ).finalize()


ALL_SOURCES: list[JobSource] = [Remotive(), Arbeitnow(), RemoteOK(), JSearch()]

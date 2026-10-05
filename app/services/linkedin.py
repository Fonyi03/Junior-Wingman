"""LinkedIn profile sync through the official Member Data Portability (Member) API.

Available to members in the EEA and Switzerland (EU Digital Markets Act).
The member creates their own developer app and generates a token with the
`r_dma_portability_self_serve` scope; nothing here logs in to LinkedIn.
Docs: https://learn.microsoft.com/en-us/linkedin/dma/member-data-portability/shared/member-snapshot-api
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx
from sqlmodel import Session, select

from ..db import get_profile, kv_get, kv_set, session_scope
from ..models import Application, utcnow

log = logging.getLogger(__name__)

API_URL = "https://api.linkedin.com/rest/memberSnapshotData"
API_VERSION = "202312"  # the only version this endpoint accepts
TOKEN_KEY = "linkedin_token"
TOKEN_SET_KEY = "linkedin_token_set_at"
SYNC_KEY = "linkedin_last_sync"
ERROR_KEY = "linkedin_error"

# Profile domains that matter for job matching and cover letters, in reading order.
PROFILE_DOMAINS = [
    "PROFILE", "PROFILE_SUMMARY", "POSITIONS", "EDUCATION", "SKILLS", "CERTIFICATIONS",
    "LANGUAGES", "PROJECTS", "COURSES", "HONORS", "PUBLICATIONS", "VOLUNTEERING_EXPERIENCES",
    "JOB_SEEKER_PREFERENCES",
]
MAX_PAGES = 20


class LinkedInAuthError(RuntimeError):
    """Token missing, expired or revoked: the user must generate a new one."""


def fetch_domain(client: httpx.Client, token: str, domain: str) -> list[dict]:
    """Return all snapshotData records of one domain, following pagination."""
    headers = {"Authorization": f"Bearer {token}", "Linkedin-Version": API_VERSION}
    records: list[dict] = []
    for start in range(MAX_PAGES):
        r = client.get(API_URL, params={"q": "criteria", "domain": domain, "start": start}, headers=headers)
        if r.status_code in (401, 403):
            raise LinkedInAuthError(f"LinkedIn token rejected ({r.status_code})")
        # LinkedIn signals "no (more) data for this domain" with an error response
        if r.status_code == 404 or (r.status_code >= 400 and "No data found" in r.text):
            break
        r.raise_for_status()
        body = r.json()
        for element in body.get("elements", []):
            data = element.get("snapshotData", [])
            records.extend(d for d in data if isinstance(d, dict))
        links = body.get("paging", {}).get("links", [])
        if not any(link.get("rel") == "next" for link in links):
            break
    return records


def format_profile(domains: dict[str, list[dict]]) -> str:
    """Turn snapshot records into compact readable text for the LLM prompts."""
    parts: list[str] = []
    for domain, records in domains.items():
        lines = []
        for rec in records:
            fields = [f"{k}: {v}" for k, v in rec.items() if v not in ("", None, [], {})]
            if fields:
                lines.append("- " + " | ".join(fields))
        if lines:
            parts.append(f"## {domain.replace('_', ' ').title()}\n" + "\n".join(lines))
    return "\n\n".join(parts)


def _pick(rec: dict, *names: str) -> str:
    lowered = {k.lower(): v for k, v in rec.items()}
    for n in names:
        v = lowered.get(n.lower())
        if v:
            return str(v).strip()
    return ""


def _parse_date(value: str) -> datetime | None:
    for fmt in ("%m/%d/%y, %I:%M %p", "%m/%d/%Y, %I:%M %p", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d %b %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(value.strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def import_job_applications(s: Session, records: list[dict]) -> int:
    """Add applications made on LinkedIn (e.g. Easy Apply) to the tracker; returns the number added."""
    existing = s.exec(select(Application)).all()
    seen_urls = {a.job_url for a in existing if a.job_url}
    seen_pairs = {(a.company.lower(), a.position.lower()) for a in existing}
    added = 0
    for rec in records:
        company = _pick(rec, "Company Name", "Company", "companyName")
        title = _pick(rec, "Job Title", "Title", "jobTitle")
        if not company or not title:
            continue
        url = _pick(rec, "Job Url", "Job URL", "jobUrl")
        if (url and url in seen_urls) or (company.lower(), title.lower()) in seen_pairs:
            continue
        applied = _parse_date(_pick(rec, "Application Date", "Applied At", "Date"))
        s.add(Application(
            company=company,
            position=title,
            job_url=url,
            status="applied",
            method="linkedin",
            contact_email=_pick(rec, "Contact Email"),
            contact_phone=_pick(rec, "Contact Phone Number"),
            applied_at=applied or utcnow(),
            notes="Imported from LinkedIn",
        ))
        seen_urls.add(url)
        seen_pairs.add((company.lower(), title.lower()))
        added += 1
    return added


# ---------------------------------------------------------------- token storage

def get_token(s: Session) -> str:
    return kv_get(s, TOKEN_KEY)


def set_token(s: Session, token: str) -> None:
    kv_set(s, TOKEN_KEY, token.strip())
    kv_set(s, TOKEN_SET_KEY, utcnow().isoformat(timespec="minutes") if token.strip() else "")
    kv_set(s, ERROR_KEY, "")


def status(s: Session) -> dict:
    set_at = kv_get(s, TOKEN_SET_KEY)
    age_days = None
    if set_at:
        age_days = (utcnow() - datetime.fromisoformat(set_at).replace(tzinfo=timezone.utc)).days
    return {
        "connected": bool(get_token(s)),
        "token_age_days": age_days,
        "last_sync": kv_get(s, SYNC_KEY),
        "error": kv_get(s, ERROR_KEY),
    }


# ---------------------------------------------------------------- sync job

def run_sync() -> dict:
    stats = {"domains": 0, "records": 0, "applications_added": 0, "errors": []}
    with session_scope() as s:
        token = get_token(s)
        if not token:
            return stats
        domains: dict[str, list[dict]] = {}
        try:
            with httpx.Client(timeout=60) as client:
                for domain in PROFILE_DOMAINS:
                    domains[domain] = fetch_domain(client, token, domain)
                applications = fetch_domain(client, token, "JOB_APPLICATIONS")
        except LinkedInAuthError as e:
            kv_set(s, ERROR_KEY, "token_expired")
            stats["errors"].append(str(e))
            return stats
        except httpx.HTTPError as e:
            log.exception("linkedin sync failed")
            kv_set(s, ERROR_KEY, str(e)[:300])
            stats["errors"].append(str(e))
            return stats

        text = format_profile(domains)
        stats["domains"] = sum(1 for v in domains.values() if v)
        stats["records"] = sum(len(v) for v in domains.values())
        if text:
            profile = get_profile(s)
            profile.linkedin_text = text
            s.add(profile)
        stats["applications_added"] = import_job_applications(s, applications)
        s.commit()
        kv_set(s, ERROR_KEY, "")
        kv_set(s, SYNC_KEY, f"{utcnow().isoformat(timespec='minutes')} · {stats}")
    return stats

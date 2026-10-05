from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import datetime

from bs4 import BeautifulSoup

USER_AGENT = "job-hunter-app/0.1 (+https://github.com/; personal job search, open source)"

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?:\+|00)\d[\d\s\-/().]{7,16}\d|\b06[\s\-/]?\d{1,2}[\s\-/]?\d{3}[\s\-]?\d{3,4}\b")
HU_HINTS = ("hungary", "magyarország", "budapest", "debrecen", "szeged", "pécs", "győr", "miskolc", ", hu")


@dataclass
class JobIn:
    source: str
    external_id: str
    title: str
    company: str = ""
    location: str = ""
    is_remote: bool = False
    country: str = ""
    url: str = ""
    description: str = ""
    salary_text: str = ""
    posted_at: datetime | None = None
    tags: list[str] = field(default_factory=list)
    apply_email: str = ""

    def finalize(self) -> "JobIn":
        self.description = html_to_text(self.description)
        if not self.apply_email:
            self.apply_email = find_apply_email(self.description)
        if not self.country and is_hungarian_location(self.location):
            self.country = "HU"
        return self


def html_to_text(raw: str) -> str:
    if not raw:
        return ""
    if "<" in raw and ">" in raw:
        raw = BeautifulSoup(raw, "html.parser").get_text("\n")
    text = html.unescape(raw)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def find_apply_email(text: str) -> str:
    """Return an email from the job text that looks like an application address."""
    emails = EMAIL_RE.findall(text or "")
    if not emails:
        return ""
    preferred = ("job", "career", "karrier", "hr", "allas", "recruit", "apply", "jelentkez", "talent", "hiring")
    for e in emails:
        if any(p in e.lower() for p in preferred):
            return e
    return ""


def find_phone(text: str) -> str:
    m = PHONE_RE.search(text or "")
    return m.group(0).strip() if m else ""


def is_hungarian_location(location: str) -> bool:
    loc = (location or "").lower()
    return any(h in loc for h in HU_HINTS)


def letter_language(country: str, location: str, is_remote: bool) -> str:
    """Hungarian letters for jobs located in Hungary, English for remote/international ones."""
    if country.upper() == "HU" or is_hungarian_location(location):
        return "hu"
    return "en"


class JobSource:
    name = "base"

    def enabled(self) -> bool:
        return True

    def fetch(self, keywords: list[str], *, want_remote: bool, want_hungary: bool) -> list[JobIn]:
        raise NotImplementedError

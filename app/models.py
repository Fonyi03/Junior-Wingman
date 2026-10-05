from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel, UniqueConstraint


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Profile(SQLModel, table=True):
    id: int = Field(default=1, primary_key=True)
    full_name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    cv_text: str = ""
    cv_filename: str = ""
    # Text exported from LinkedIn ("Save to PDF"), Glassdoor, etc.
    profiles_text: str = ""
    # Synced automatically via the LinkedIn Member Data Portability API
    linkedin_text: str = ""
    # Comma separated; generated from the CV, editable
    search_keywords: str = ""
    want_remote: bool = True
    want_hungary: bool = True
    salary_huf: str = ""  # e.g. "900 000 Ft bruttó / hó"
    salary_eur: str = ""  # e.g. "€55,000 / year"
    min_score: int = 65
    notes: str = ""  # extra instructions for the cover letter writer
    ui_lang: str = "hu"

    def keywords(self) -> list[str]:
        return [k.strip() for k in self.search_keywords.split(",") if k.strip()]


class Job(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("source", "external_id"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    source: str
    external_id: str
    title: str
    company: str = ""
    location: str = ""
    is_remote: bool = False
    country: str = ""  # ISO-2 if known
    url: str = ""
    apply_email: str = ""
    description: str = ""
    salary_text: str = ""
    posted_at: Optional[datetime] = None
    fetched_at: datetime = Field(default_factory=utcnow)
    prefilter_score: int = 0
    score: Optional[int] = None
    score_reason: str = ""
    language: str = "en"
    # new -> scored -> suggested | rejected_low; dismissed; applied
    status: str = "new"


class Application(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: Optional[int] = Field(default=None, foreign_key="job.id")
    company: str = ""
    position: str = ""
    job_url: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    # draft -> applied -> received -> interview -> offer | rejected | withdrawn
    status: str = "draft"
    method: str = ""  # email | manual
    language: str = "en"
    cover_letter_subject: str = ""
    cover_letter: str = ""
    salary_expectation: str = ""
    contact_name: str = ""
    contact_email: str = ""
    contact_phone: str = ""
    applied_at: Optional[datetime] = None
    last_email_at: Optional[datetime] = None
    gmail_thread_id: str = ""
    notes: str = ""


class EmailEvent(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    gmail_id: str = Field(index=True, unique=True)
    thread_id: str = ""
    application_id: Optional[int] = Field(default=None, foreign_key="application.id")
    from_addr: str = ""
    subject: str = ""
    snippet: str = ""
    received_at: Optional[datetime] = None
    detected_status: str = ""


class KV(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: str = ""

"""All LLM tasks: profile analysis, job scoring, cover letters, email classification."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ..llm import get_llm
from ..models import Job, Profile

MAX_DOC_CHARS = 12000


class ProfileInsights(BaseModel):
    search_keywords: list[str] = Field(description="5-8 short job-title search terms in English, most relevant first")
    summary: str = Field(description="2-3 sentence professional summary")


class MatchResult(BaseModel):
    score: int = Field(description="0-100 fit between candidate and job")
    reason: str = Field(description="One or two sentences: strongest matches and main gaps")
    contact_name: str = Field(description="Recruiter/contact person named in the posting, or empty")
    contact_phone: str = Field(description="Phone number in the posting, or empty")
    contact_email: str = Field(description="Application email in the posting, or empty")


class CoverLetter(BaseModel):
    subject: str = Field(description="Email subject line for the application")
    body: str = Field(description="Cover letter body, plain text, no placeholders")


class EmailClassification(BaseModel):
    is_job_related: bool
    company: str = Field(description="Company the email is about, or empty")
    status: Literal["received", "interview", "offer", "rejected", "other"]
    contact_name: str = ""
    contact_phone: str = ""


def _profile_block(p: Profile) -> str:
    return (
        f"Name: {p.full_name}\nLocation: {p.location}\n\n"
        f"CV:\n{p.cv_text[:MAX_DOC_CHARS]}\n\n"
        f"LinkedIn profile (synced):\n{p.linkedin_text[:MAX_DOC_CHARS]}\n\n"
        f"Other profiles (uploaded exports):\n{p.profiles_text[:MAX_DOC_CHARS]}"
    )


def _job_block(j: Job) -> str:
    return (
        f"Title: {j.title}\nCompany: {j.company}\nLocation: {j.location} (remote={j.is_remote})\n"
        f"Salary: {j.salary_text}\n\nDescription:\n{j.description[:MAX_DOC_CHARS]}"
    )


def analyze_profile(p: Profile) -> ProfileInsights:
    return get_llm().structured(
        "You are a career advisor. Read the candidate's documents and propose job search terms.",
        _profile_block(p),
        ProfileInsights,
    )


def score_job(p: Profile, j: Job) -> MatchResult:
    system = (
        "You screen job postings for a candidate. Score realistic fit 0-100: required skills, "
        "seniority, language requirements and location/remote eligibility. Be strict: below 50 "
        "means the candidate would likely be rejected. Extract contact details only if they "
        "literally appear in the posting."
    )
    prompt = f"<candidate>\n{_profile_block(p)}\n</candidate>\n\n<job>\n{_job_block(j)}\n</job>"
    result = get_llm().structured(system, prompt, MatchResult, fast=True)
    result.score = max(0, min(100, result.score))
    return result


def write_cover_letter(p: Profile, j: Job, language: str, salary: str) -> CoverLetter:
    lang_name = "Hungarian" if language == "hu" else "English"
    system = (
        f"You write concise, specific job application emails in {lang_name}. "
        "Use only facts from the candidate's documents; never invent experience. "
        "3-4 short paragraphs, warm but professional, no clichés, no placeholders like [Name]. "
        "Sign with the candidate's name and phone number."
    )
    extra = f"\nCandidate's extra instructions: {p.notes}" if p.notes else ""
    salary_line = f"\nMention this salary expectation only if the posting asks for one: {salary}" if salary else ""
    prompt = (
        f"<candidate>\n{_profile_block(p)}\nPhone: {p.phone}\nEmail: {p.email}\n</candidate>\n\n"
        f"<job>\n{_job_block(j)}\n</job>{salary_line}{extra}"
    )
    return get_llm().structured(system, prompt, CoverLetter)


def classify_email(sender: str, subject: str, body: str, companies: list[str]) -> EmailClassification:
    system = (
        "Classify an email from a job search inbox. 'received' = application acknowledged, "
        "'interview' = invitation/scheduling of any interview or test, 'offer' = job offer, "
        "'rejected' = rejection, 'other' = anything else. Extract a phone number only if present."
    )
    prompt = (
        f"Known companies the candidate applied to: {', '.join(companies[:200])}\n\n"
        f"From: {sender}\nSubject: {subject}\n\n{body[:6000]}"
    )
    return get_llm().structured(system, prompt, EmailClassification, fast=True)

"""Gmail access through the official Gmail API (OAuth, no password stored)."""
from __future__ import annotations

import base64
import json
import mimetypes
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

from ..config import get_settings
from ..db import kv_get, kv_set, session_scope

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]
TOKEN_KEY = "gmail_token"
VERIFIER_KEY = "gmail_code_verifier"


def configured() -> bool:
    s = get_settings()
    return bool(s.google_client_id and s.google_client_secret)


def _flow() -> Flow:
    s = get_settings()
    cfg = {
        "web": {
            "client_id": s.google_client_id,
            "client_secret": s.google_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    return Flow.from_client_config(cfg, scopes=SCOPES, redirect_uri=f"{s.base_url.rstrip('/')}/gmail/callback")


def auth_url() -> str:
    flow = _flow()
    url, _ = flow.authorization_url(access_type="offline", prompt="consent", include_granted_scopes="true")
    with session_scope() as s:
        kv_set(s, VERIFIER_KEY, flow.code_verifier or "")
    return url


def finish_auth(full_callback_url: str) -> None:
    flow = _flow()
    with session_scope() as s:
        flow.code_verifier = kv_get(s, VERIFIER_KEY) or None
        # oauthlib refuses http:// callbacks unless told it is a local setup
        import os

        if full_callback_url.startswith("http://"):
            os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
        flow.fetch_token(authorization_response=full_callback_url)
        kv_set(s, TOKEN_KEY, flow.credentials.to_json())


def disconnect() -> None:
    with session_scope() as s:
        kv_set(s, TOKEN_KEY, "")


def _credentials() -> Credentials | None:
    with session_scope() as s:
        raw = kv_get(s, TOKEN_KEY)
        if not raw:
            return None
        creds = Credentials.from_authorized_user_info(json.loads(raw), SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            kv_set(s, TOKEN_KEY, creds.to_json())
        return creds


def connected() -> bool:
    try:
        return _credentials() is not None
    except Exception:
        return False


def _service():
    creds = _credentials()
    if not creds:
        raise RuntimeError("Gmail is not connected")
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def account_email() -> str:
    return _service().users().getProfile(userId="me").execute().get("emailAddress", "")


def send(to: str, subject: str, body: str, attachment: Path | None = None) -> str:
    """Send an email and return the Gmail thread id."""
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    if attachment and attachment.exists():
        ctype, _ = mimetypes.guess_type(attachment.name)
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        msg.add_attachment(attachment.read_bytes(), maintype=maintype, subtype=subtype, filename=attachment.name)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    sent = _service().users().messages().send(userId="me", body={"raw": raw}).execute()
    return sent.get("threadId", "")


@dataclass
class Mail:
    id: str
    thread_id: str
    sender: str
    sender_email: str
    subject: str
    snippet: str
    body: str
    received_at: datetime | None


def _decode_body(payload: dict) -> str:
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
    for part in payload.get("parts", []) or []:
        text = _decode_body(part)
        if text:
            return text
    if payload.get("mimeType") == "text/html" and payload.get("body", {}).get("data"):
        from ..sources.base import html_to_text

        return html_to_text(base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace"))
    return ""


def list_recent(days: int, skip_ids: set[str]) -> list[Mail]:
    svc = _service()
    q = f"newer_than:{days}d -in:sent -in:chats -category:promotions -category:social -category:forums"
    ids: list[dict] = []
    req = svc.users().messages().list(userId="me", q=q, maxResults=100)
    while req is not None and len(ids) < 500:
        resp = req.execute()
        ids.extend(resp.get("messages", []))
        req = svc.users().messages().list_next(req, resp)
    out: list[Mail] = []
    for m in ids:
        if m["id"] in skip_ids:
            continue
        full = svc.users().messages().get(userId="me", id=m["id"], format="full").execute()
        headers = {h["name"].lower(): h["value"] for h in full.get("payload", {}).get("headers", [])}
        name, addr = parseaddr(headers.get("from", ""))
        ts = int(full.get("internalDate", "0")) / 1000
        out.append(
            Mail(
                id=m["id"],
                thread_id=full.get("threadId", ""),
                sender=name or addr,
                sender_email=addr.lower(),
                subject=headers.get("subject", ""),
                snippet=full.get("snippet", ""),
                body=_decode_body(full.get("payload", {})),
                received_at=datetime.fromtimestamp(ts, timezone.utc) if ts else None,
            )
        )
    return out

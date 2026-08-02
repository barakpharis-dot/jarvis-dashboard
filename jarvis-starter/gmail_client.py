"""
Read-only Gmail access. Scope is gmail.readonly on purpose -- this app
can never send or delete anything, matching the safety-net principle
from the original design.
"""
import os
import base64
import email.utils
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.events",
]
def build_flow():
    return Flow.from_client_config(
        {
            "web": {
                "client_id": os.environ["GOOGLE_CLIENT_ID"],
                "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [os.environ["GOOGLE_REDIRECT_URI"]],
            }
        },
        scopes=SCOPES,
        redirect_uri=os.environ["GOOGLE_REDIRECT_URI"],
    )

def get_gmail_service(credentials_dict: dict):
    creds = Credentials(**credentials_dict)
    return build("gmail", "v1", credentials=creds)

def get_calendar_service(credentials_dict: dict):
    creds = Credentials(**credentials_dict)
    return build("calendar", "v3", credentials=creds)

def fetch_recent_messages(service, max_results: int = 25):
    """Returns a list of {id, sender, subject, snippet, body, received_at, thread_id, message_id_header}."""
    results = service.users().messages().list(
        userId="me", maxResults=max_results, labelIds=["INBOX"]
    ).execute()
    messages = results.get("messages", [])

    parsed = []
    for m in messages:
        msg = service.users().messages().get(
            userId="me", id=m["id"], format="full"
        ).execute()
        headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
        parsed.append({
            "id": msg["id"],
            "sender": headers.get("From", ""),
            "subject": headers.get("Subject", ""),
            "snippet": msg.get("snippet", ""),
            "body": _extract_body(msg["payload"]),
            "received_at": _parse_date(headers.get("Date", "")),
            "thread_id": msg.get("threadId"),
            "message_id_header": headers.get("Message-ID"),
        })
    return parsed

def _extract_body(payload) -> str:
    """Pulls plain-text body out of a Gmail message payload, best effort."""
    if payload.get("mimeType") == "text/plain" and "data" in payload.get("body", {}):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="ignore")
    for part in payload.get("parts", []):
        text = _extract_body(part)
        if text:
            return text
    return ""
def _parse_date(date_str: str) -> str | None:
    """Converts Gmail's date header into a format Postgres accepts."""
    if not date_str:
        return None
    try:
        return email.utils.parsedate_to_datetime(date_str).isoformat()
    except (TypeError, ValueError):
        return None

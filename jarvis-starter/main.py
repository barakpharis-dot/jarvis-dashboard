import os
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client
from apscheduler.schedulers.background import BackgroundScheduler

from gmail_client import build_flow, get_gmail_service, get_calendar_service, fetch_recent_messages
from classifier import classify
from datetime import datetime, timedelta
import base64
import email.utils
from email.mime.text import MIMEText

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


@app.get("/")
def serve_dashboard():
    return FileResponse("dashboard.html")


def save_credentials(creds: dict):
    row = {
        "id": "me",
        "token": creds["token"],
        "refresh_token": creds["refresh_token"],
        "token_uri": creds["token_uri"],
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "scopes": ",".join(creds["scopes"]) if creds.get("scopes") else "",
    }
    supabase.table("app_credentials").upsert(row).execute()


def load_credentials():
    result = supabase.table("app_credentials").select("*").eq("id", "me").execute()
    if not result.data:
        return None
    row = result.data[0]
    return {
        "token": row["token"],
        "refresh_token": row["refresh_token"],
        "token_uri": row["token_uri"],
        "client_id": row["client_id"],
        "client_secret": row["client_secret"],
        "scopes": row["scopes"].split(",") if row["scopes"] else [],
    }


@app.get("/auth/start")
def auth_start():
    flow = build_flow()
    auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent")
    return RedirectResponse(auth_url)


@app.get("/auth/callback")
def auth_callback(request: Request):
    flow = build_flow()
    flow.fetch_token(authorization_response=str(request.url))
    creds = flow.credentials
    save_credentials({
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes,
    })
    return JSONResponse({"status": "connected"})


def _create_calendar_event(creds, summary, date, time=None):
    """Creates a real Google Calendar event. Returns both the id (needed to delete it later) and the link."""
    service = get_calendar_service(creds)
    if time:
        start_dt = datetime.fromisoformat(f"{date}T{time}:00")
        end_dt = start_dt + timedelta(hours=1)
        event = {
            "summary": summary,
            "start": {"dateTime": start_dt.isoformat(), "timeZone": "America/Chicago"},
            "end":

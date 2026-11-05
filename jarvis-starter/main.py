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


def _create_calendar_event(creds, summary, date, time=None, description="Added from JARVIS", end_time=None):
    """Creates a real Google Calendar event. Returns both the id (needed to delete it later) and the link."""
    service = get_calendar_service(creds)
    time = time[:5] if time else time
    end_time = end_time[:5] if end_time else end_time
    if time:
        start_dt = datetime.fromisoformat(f"{date}T{time}:00")
        end_dt = start_dt + timedelta(hours=1)
        if end_time:
            candidate = datetime.fromisoformat(f"{date}T{end_time}:00")
            if candidate > start_dt:
                end_dt = candidate
        event = {
            "summary": summary,
            "start": {"dateTime": start_dt.isoformat(), "timeZone": "America/Chicago"},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": "America/Chicago"},
            "description": description,
        }
    else:
        end_date = (datetime.fromisoformat(date) + timedelta(days=1)).date().isoformat()
        event = {
            "summary": summary,
            "start": {"date": date},
            "end": {"date": end_date},
            "description": description,
        }
    created = service.events().insert(calendarId="primary", body=event).execute()
    return {"id": created.get("id"), "link": created.get("htmlLink")}


def _fetch_calendar_events(creds, days_ahead=30):
    """Pulls upcoming events straight from Google Calendar, so things added there (not through JARVIS) still show up here."""
    service = get_calendar_service(creds)
    now = datetime.utcnow()
    time_min = now.isoformat() + "Z"
    time_max = (now + timedelta(days=days_ahead)).isoformat() + "Z"
    events_result = service.events().list(
        calendarId="primary", timeMin=time_min, timeMax=time_max,
        singleEvents=True, orderBy="startTime", maxResults=100,
    ).execute()

    parsed = []
    for e in events_result.get("items", []):
        start = e.get("start", {})
        if "date" in start:
            due_date, due_time = start["date"], None
        elif "dateTime" in start:
            dt = datetime.fromisoformat(start["dateTime"])
            due_date, due_time = dt.date().isoformat(), dt.strftime("%H:%M")
        else:
            continue
        parsed.append({
            "id": f"gcal-{e['id']}",
            "text": e.get("summary", "(no title)"),
            "due_date": due_date,
            "due_time": due_time,
            "source": "calendar",
            "done": False,
            "calendar_event_link": e.get("htmlLink"),
        })
    return parsed


def _social_ids():
    """Calendar event ids you've marked as social."""
    try:
        return {r["event_id"] for r in supabase.table("social_events").select("event_id").execute().data}
    except Exception as e:
        print(f"social_events table unavailable: {e}")
        return set()


def _calendar_done_ids():
    """Calendar events you've checked off in JARVIS (the real event stays on Google Calendar)."""
    try:
        return {r["event_id"] for r in supabase.table("calendar_done").select("event_id").execute().data}
    except Exception as e:
        print(f"calendar_done table unavailable: {e}")
        return set()


@app.post("/sync")
def sync():
    """Pulls recent emails, classifies each, and upserts into Supabase.
    Action items found in emails are saved as SUGGESTED tasks (approved = false).
    They only become real tasks (and Calendar events) when you approve them on the Tasks page."""
    creds = load_credentials()
    if not creds:
        return JSONResponse({"error": "not authenticated, visit /auth/start first"}, status_code=401)

    service = get_gmail_service(creds)
    messages = fetch_recent_messages(service)

    # emails you've cleared from the dashboard are skipped entirely
    archived_ids = {r["id"] for r in supabase.table("emails").select("id").eq("archived", True).execute().data}

    results = []
    for email in messages:
        if email["id"] in archived_ids:
            continue

        try:
            result = classify(email)
        except Exception as e:
            print(f"Skipping email {email['id']} ({email['subject']}) — classification error: {e}")
            continue

        row = {
            "id": email["id"],
            "sender": email["sender"],
            "subject": email["subject"],
            "category": result["category"],
            "summary": result["summary"],
            "action_needed": result.get("action_needed"),
            "body": email["body"],
            "full_summary": result.get("full_summary"),
            "needs_reply": result.get("needs_reply", False),
            "source": result["source"],
            "confidence": result.get("confidence"),
            "received_at": email["received_at"],
            "due_date": result.get("due_date"),
            "due_time": result.get("due_time"),
            "thread_id": email["thread_id"],
            "message_id_header": email.get("message_id_header"),
        }
        supabase.table("emails").upsert(row).execute()

        existing_email = supabase.table("emails").select("task_dismissed").eq("id", email["id"]).execute().data
        was_dismissed = existing_email[0].get("task_dismissed") if existing_email else False

        if result.get("action_needed") and not was_dismissed:
            existing = supabase.table("tasks").select("id").eq("source_email_id", email["id"]).execute()
            if not existing.data:
                supabase.table("tasks").insert({
                    "text": result["action_needed"],
                    "due_date": result.get("due_date"),
                    "due_time": result.get("due_time"),
                    "source": "ai",
                    "source_email_id": email["id"],
                    "done": False,
                    "approved": False,
                }).execute()

        results.append(row)

    return {"synced": len(results), "emails": results}


@app.post("/calendar/event")
def create_calendar_event(payload: dict):
    """payload = {"summary": "...", "date": "YYYY-MM-DD", "time": "HH:MM" (optional)}"""
    creds = load_credentials()
    if not creds:
        return JSONResponse({"error": "not authenticated, visit /auth/start first"}, status_code=401)
    event = _create_calendar_event(creds, payload["summary"], payload["date"], payload.get("time"))
    return {"status": "created", "link": event["link"]}


@app.get("/calendar/week")
def calendar_week(start: str):
    """start = first day of the week, like 2026-11-08. Returns that week's Google Calendar events."""
    creds = load_credentials()
    if not creds:
        return JSONResponse({"error": "not authenticated, visit /auth/start first"}, status_code=401)
    try:
        start_date = datetime.fromisoformat(start).date()
    except ValueError:
        return JSONResponse({"error": "start must look like 2026-11-08"}, status_code=400)
    end_date = start_date + timedelta(days=7)  # exclusive

    service = get_calendar_service(creds)
    # ask for a slightly wider window, then keep only events whose date falls inside the week
    time_min = (datetime.combine(start_date, datetime.min.time()) - timedelta(days=1)).isoformat() + "Z"
    time_max = (datetime.combine(end_date, datetime.min.time()) + timedelta(days=1)).isoformat() + "Z"
    try:
        result = service.events().list(
            calendarId="primary", timeMin=time_min, timeMax=time_max,
            singleEvents=True, orderBy="startTime", maxResults=250, timeZone="America/Chicago",
        ).execute()
    except Exception as e:
        print(f"Could not read calendar week: {e}")
        return JSONResponse({"error": "could not read calendar"}, status_code=502)

    social_ids = _social_ids()
    events = []
    for e in result.get("items", []):
        # events JARVIS made for tasks are tasks, not social plans
        if (e.get("description") or "").strip() == "Added from JARVIS":
            continue
        if e.get("status") == "cancelled":
            continue
        s, en = e.get("start", {}), e.get("end", {})
        base = {
            "id": e["id"],
            "title": e.get("summary", "(no title)"),
            "location": e.get("location"),
            "link": e.get("htmlLink"),
            "social": e["id"] in social_ids,
        }
        if "date" in s:
            day = datetime.fromisoformat(s["date"]).date()
            last = datetime.fromisoformat(en["date"]).date() if "date" in en else day + timedelta(days=1)
            day, last = max(day, start_date), min(last, end_date)
            while day < last:
                events.append({**base, "date": day.isoformat(), "all_day": True, "start_time": None, "end_time": None})
                day += timedelta(days=1)
        elif "dateTime" in s:
            sdt = datetime.fromisoformat(s["dateTime"])
            if not (start_date <= sdt.date() < end_date):
                continue
            edt = datetime.fromisoformat(en["dateTime"]) if "dateTime" in en else None
            events.append({
                **base, "date": sdt.date().isoformat(), "all_day": False,
                "start_time": sdt.strftime("%H:%M"), "end_time": edt.strftime("%H:%M") if edt else None,
            })
    return events


@app.post("/social/toggle")
def social_toggle(payload: dict):
    """payload = {"event_id": "...", "social": true or false}"""
    event_id = payload["event_id"]
    if payload.get("social"):
        supabase.table("social_events").upsert({"event_id": event_id}).execute()
    else:
        supabase.table("social_events").delete().eq("event_id", event_id).execute()
    return {"status": "ok"}


@app.post("/social/event")
def social_event(payload: dict):
    """payload = {"title": "...", "date": "YYYY-MM-DD", "time": "HH:MM" (optional), "end_time": "HH:MM" (optional)}"""
    creds = load_credentials()
    if not creds:
        return JSONResponse({"error": "not authenticated, visit /auth/start first"}, status_code=401)
    title = (payload.get("title") or "").strip()
    date = payload.get("date")
    if not title or not date:
        return JSONResponse({"error": "title and date are required"}, status_code=400)
    event = _create_calendar_event(
        creds, title, date, payload.get("time") or None,
        description="Social event added from JARVIS", end_time=payload.get("end_time") or None,
    )
    supabase.table("social_events").upsert({"event_id": event["id"]}).execute()
    return {"status": "created", "id": event["id"], "link": event["link"]}


@app.get("/tasks")
def list_tasks():
    db_tasks = supabase.table("tasks").select("*").order("due_date").execute().data
    creds = load_credentials()
    if not creds:
        return db_tasks

    try:
        cal_events = _fetch_calendar_events(creds)
    except Exception as e:
        print(f"Could not fetch calendar events: {e}")
        return db_tasks

    done_ids = _calendar_done_ids()
    existing_keys = {(t.get("text"), t.get("due_date")) for t in db_tasks}
    merged = list(db_tasks)
    for ev in cal_events:
        if (ev["text"], ev["due_date"]) not in existing_keys:
            ev["done"] = ev["id"][len("gcal-"):] in done_ids
            merged.append(ev)
    return merged


@app.post("/tasks")
def create_task(payload: dict):
    """payload = {"text": "...", "due_date": "YYYY-MM-DD" (optional), "due_time": "HH:MM" (optional)}"""
    row = {
        "text": payload["text"],
        "due_date": payload.get("due_date"),
        "due_time": payload.get("due_time"),
        "source": "manual",
        "done": False,
        "approved": True,
    }
    if row["due_date"]:
        creds = load_credentials()
        if creds:
            try:
                event = _create_calendar_event(creds, row["text"], row["due_date"], row["due_time"])
                row["calendar_event_id"] = event["id"]
                row["calendar_event_link"] = event["link"]
            except Exception as e:
                print(f"Could not auto-create calendar event: {e}")
    created = supabase.table("tasks").insert(row).execute()
    return created.data[0]


@app.post("/tasks/{task_id}/approve")
def approve_task(task_id: str):
    """Turns a suggested task into a real one, and adds it to Google Calendar if it has a due date."""
    found = supabase.table("tasks").select("*").eq("id", task_id).execute().data
    if not found:
        return JSONResponse({"error": "task not found"}, status_code=404)
    t = found[0]

    update = {"approved": True}
    if t.get("due_date") and not t.get("calendar_event_id"):
        creds = load_credentials()
        if creds:
            try:
                event = _create_calendar_event(creds, t["text"], t["due_date"], t.get("due_time"))
                update["calendar_event_id"] = event["id"]
                update["calendar_event_link"] = event["link"]
            except Exception as e:
                print(f"Could not create calendar event for approved task {task_id}: {e}")

    updated = supabase.table("tasks").update(update).eq("id", task_id).execute()
    return updated.data[0]


@app.patch("/tasks/{task_id}")
def update_task(task_id: str, payload: dict):
    """payload = {"done": true or false}"""
    if task_id.startswith("gcal-"):
        event_id = task_id[len("gcal-"):]
        try:
            if payload["done"]:
                supabase.table("calendar_done").upsert({"event_id": event_id}).execute()
            else:
                supabase.table("calendar_done").delete().eq("event_id", event_id).execute()
        except Exception as e:
            print(f"Could not save done state for calendar event {event_id}: {e}")
            return JSONResponse({"error": "could not save"}, status_code=500)
        return {"status": "updated"}
    supabase.table("tasks").update({"done": payload["done"]}).eq("id", task_id).execute()
    return {"status": "updated"}


@app.delete("/tasks/{task_id}")
def delete_task(task_id: str):
    """Deletes the task (or dismisses a suggestion), removes its Calendar event if it has one,
    and marks the originating email so a future sync doesn't just recreate the same task."""
    if task_id.startswith("gcal-"):
        creds = load_credentials()
        if not creds:
            return JSONResponse({"error": "not authenticated"}, status_code=401)
        real_event_id = task_id[len("gcal-"):]
        try:
            service = get_calendar_service(creds)
            service.events().delete(calendarId="primary", eventId=real_event_id).execute()
        except Exception as e:
            print(f"Could not delete calendar event {real_event_id}: {e}")
        return {"status": "deleted"}

    task = supabase.table("tasks").select("*").eq("id", task_id).execute().data
    if task:
        t = task[0]
        if t.get("calendar_event_id"):
            creds = load_credentials()
            if creds:
                try:
                    service = get_calendar_service(creds)
                    service.events().delete(calendarId="primary", eventId=t["calendar_event_id"]).execute()
                except Exception as e:
                    print(f"Could not delete calendar event for task {task_id}: {e}")
        if t.get("source_email_id"):
            supabase.table("emails").update({"task_dismissed": True}).eq("id", t["source_email_id"]).execute()
    supabase.table("tasks").delete().eq("id", task_id).execute()
    return {"status": "deleted"}


@app.get("/emails")
def list_emails(category: str | None = None):
    # archived emails are hidden from the dashboard (they are still in Gmail)
    query = supabase.table("emails").select("*").eq("archived", False).order("received_at", desc=True)
    if category:
        query = query.eq("category", category)
    return query.execute().data


@app.delete("/emails/{email_id}")
def delete_email(email_id: str):
    """Moves the email to Trash in Gmail (recoverable for 30 days) and removes it from the dashboard's view."""
    creds = load_credentials()
    if not creds:
        return JSONResponse({"error": "not authenticated, visit /auth/start first"}, status_code=401)

    service = get_gmail_service(creds)
    service.users().messages().trash(userId="me", id=email_id).execute()

    # unlink any task tied to this email so it isn't lost, but no longer blocks deletion
    supabase.table("tasks").update({"source_email_id": None}).eq("source_email_id", email_id).execute()

    supabase.table("emails").delete().eq("id", email_id).execute()
    return {"status": "trashed"}


@app.post("/emails/{email_id}/reply")
def send_reply(email_id: str, payload: dict):
    """payload = {"body": "reply text"} -- sends immediately once you click Send on the dashboard."""
    creds = load_credentials()
    if not creds:
        return JSONResponse({"error": "not authenticated, visit /auth/start first"}, status_code=401)

    record = supabase.table("emails").select("*").eq("id", email_id).single().execute().data
    if not record:
        return JSONResponse({"error": "email not found"}, status_code=404)

    service = get_gmail_service(creds)
    _, to_addr = email.utils.parseaddr(record["sender"])

    msg = MIMEText(payload["body"])
    msg["to"] = to_addr
    subject = record["subject"] or ""
    msg["subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    if record.get("message_id_header"):
        msg["In-Reply-To"] = record["message_id_header"]
        msg["References"] = record["message_id_header"]

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    message_body = {"raw": raw}
    if record.get("thread_id"):
        message_body["threadId"] = record["thread_id"]

    sent = service.users().messages().send(userId="me", body=message_body).execute()
    return {"status": "sent", "id": sent.get("id")}


# Re-syncs every 2 hours automatically once the app is running.
scheduler = BackgroundScheduler()
scheduler.add_job(sync, "interval", hours=2)
scheduler.start()

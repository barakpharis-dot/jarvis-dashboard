# JARVIS starter kit

This is a working skeleton for the dashboard from the demo site: it connects to
Gmail read-only, sorts emails with cheap rules first and Claude for anything
ambiguous, and stores the result in Supabase. It's intentionally minimal so
Claude Code can extend it with you rather than you starting from a blank file.

## What's already done for you
- Gmail OAuth flow (`gmail_client.py`)
- Rule + AI classification, including the one-line summary (`classifier.py`)
- FastAPI routes: `/auth/start`, `/auth/callback`, `/sync`, `/emails` (`main.py`)
- Auto re-sync every 2 hours (built in, no extra setup)
- Supabase schema for emails, tasks, and rules (`schema.sql`)
- Railway deploy config (`Procfile`)

## What you still need to do

### 1. Google Cloud (the fiddliest part)
1. Go to console.cloud.google.com → create a new project.
2. APIs & Services → Library → enable the **Gmail API**.
3. APIs & Services → OAuth consent screen → External → fill in app name and
   your email → add scope `.../auth/gmail.readonly` → add yourself as a
   test user.
4. APIs & Services → Credentials → Create Credentials → OAuth client ID →
   type "Web application" → add `http://localhost:8000/auth/callback` as
   an authorized redirect URI.
5. Copy the client ID and secret into `.env`.

### 2. Supabase
1. Create a free project at supabase.com.
2. SQL Editor → paste in `schema.sql` → run it.
3. Settings → API → copy the project URL and anon/service key into `.env`.

### 3. Anthropic
Copy your API key from console.anthropic.com into `.env`.

### 4. Run it locally
```
cp .env.example .env    # then fill in the values above
pip install -r requirements.txt
uvicorn main:app --reload
```
Visit `http://localhost:8000/auth/start`, log in with your Google account,
then `POST http://localhost:8000/sync` to pull and classify your real inbox.

### 5. Deploy
Push this folder to a GitHub repo, connect it to Railway, add the same `.env`
values as Railway environment variables, and update
`GOOGLE_REDIRECT_URI` (both in `.env` and in the Google Cloud Console) to your
Railway URL instead of localhost.

## Handing this off to Claude Code
Once you have accounts set up, open Claude Code in this folder and try:

> I have a FastAPI backend here that connects to Gmail and classifies emails
> with Claude. Help me get it running locally, then help me connect the demo
> dashboard's React frontend to the real `/emails` endpoint instead of mock data.

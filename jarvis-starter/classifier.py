"""
Cheap rules first (receipts, retail — always skip AI).
Named contacts get a forced category, but still go through AI for a real summary,
since a message from a person or platform always has content worth reading.
"""
import os
import json
from datetime import datetime
from anthropic import Anthropic

client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

RULE_SENDERS = [s.strip() for s in os.environ.get("KNOWN_RULE_SENDERS", "").split(",") if s.strip()]

def _parse_named_contacts() -> dict:
    raw = os.environ.get("NAMED_CONTACTS", "")
    pairs = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if ":" in entry:
            name, category = entry.split(":", 1)
            pairs[name.strip().lower()] = category.strip()
    return pairs

NAMED_CONTACTS = _parse_named_contacts()
CATEGORIES = "parents|principals|colleagues|school-ops|personal|finance|promotions|other"


def ai_classify(email: dict) -> dict:
    """Sends the email to Claude for category + summary + action + reply flag."""
    today_str = datetime.now().strftime("%Y-%m-%d (%A)")
    prompt = f"""Classify this email. Respond ONLY with JSON, no other text.
Today's date is {today_str}, use it to resolve relative dates like "Friday" or "next week".

From: {email['sender']}
Subject: {email['subject']}
Body: {email['body'][:2000]}

Categories, and what belongs in each:
- parents: a parent writing about their own child/student
- principals: school leadership or administration
- colleagues: fellow teachers or grade-level team members
- school-ops: schoolwide or district-wide platform announcements (e.g. ParentSquare, front office blasts)
- personal: friends/family unrelated to work
- finance: bills, statements, banking
- promotions: retail, marketing, newsletters
- other: anything that doesn't fit above

Return exactly this shape:
{{"category": "{CATEGORIES}",
  "summary": "one sentence, what this email is actually about",
  "full_summary": "a fuller 3-4 sentence summary covering all the key details, so the person doesn't have to open the original",
  "action_needed": "a short, specific next step the person should take, or null if nothing is required",
  "due_date": "YYYY-MM-DD if the action has a specific date/deadline, else null",
  "due_time": "HH:MM in 24-hour time if a specific time is mentioned, else null",
  "needs_reply": true or false,
  "confidence": 0.0 to 1.0}}
"""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )
    text = response.content[0].text.strip()
    text = text.replace("```json", "").replace("```", "").strip()
    result = json.loads(text)
    result["source"] = "ai"
    return result


def classify(email: dict) -> dict:
    sender = email["sender"].lower()

    # Pure noise -- receipts, retail. Always skip AI, always "promotions".
    if any(domain in sender for domain in RULE_SENDERS):
        return {
            "category": "promotions", "summary": None, "full_summary": None,
            "action_needed": None, "due_date": None, "due_time": None,
            "needs_reply": False, "source": "rule", "confidence": None,
        }

    # Named contacts -- still get a real AI summary, category is just locked in.
    forced_category = None
    for name, category in NAMED_CONTACTS.items():
        if name in sender:
            forced_category = category
            break

    result = ai_classify(email)
    if forced_category:
        result["category"] = forced_category
    return result

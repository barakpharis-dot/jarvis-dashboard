-- Run this in the Supabase SQL editor once, before starting the app.

create table if not exists emails (
  id text primary key,              -- Gmail message id
  sender text not null,
  subject text,
  category text,                    -- finance / networking / family / promotions / other
  summary text,                     -- one-line TL;DR, null for rule-caught emails
  needs_reply boolean default false,
  source text not null,             -- 'rule' or 'ai'
  confidence numeric,               -- null for rule-caught, 0-1 for ai
  received_at timestamptz,
  created_at timestamptz default now()
);

create table if not exists tasks (
  id uuid primary key default gen_random_uuid(),
  text text not null,
  due_date date,
  source text not null,             -- 'manual' or 'ai'
  source_email_id text references emails(id),
  done boolean default false,
  created_at timestamptz default now()
);

-- Corrections you make become permanent rules, so accuracy improves over time.
create table if not exists rules (
  id uuid primary key default gen_random_uuid(),
  pattern text not null,            -- sender domain, address, or subject keyword
  match_type text not null,         -- 'sender' or 'subject_keyword'
  category text not null,
  created_at timestamptz default now()
);

create index if not exists idx_emails_category on emails(category);
create index if not exists idx_tasks_done on tasks(done);

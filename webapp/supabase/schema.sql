-- Run this once in the Supabase SQL Editor (Project > SQL Editor > New query).
-- Creates the Journal and Training Data tables, with row-level security
-- scoped so only the logged-in user can read/write their own rows.

create table if not exists journal_entries (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  date date not null,
  ticker text not null default 'SPY',
  direction text not null check (direction in ('Long', 'Short')),
  setup text default '',
  result text default '',
  notes text default '',
  created_at timestamptz not null default now()
);

alter table journal_entries enable row level security;

create policy "journal_entries_owner_all"
  on journal_entries
  for all
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

create table if not exists training_examples (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  occurred_at timestamptz not null,
  type text not null check (type in ('Entry', 'Exit')),
  direction text not null check (direction in ('Long', 'Short')),
  quality text not null check (quality in ('Good', 'Bad')),
  notes text default '',
  created_at timestamptz not null default now()
);

alter table training_examples enable row level security;

create policy "training_examples_owner_all"
  on training_examples
  for all
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

-- Each row is one AI-synthesized "entry model" — a snapshot in time, not
-- overwritten in place, so the Model tab can show how the rules evolve as
-- more Training Data examples accumulate.
create table if not exists entry_models (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  generated_at timestamptz not null default now(),
  bars_analyzed int not null default 0,
  examples_used int not null default 0,
  date_range jsonb,
  rules jsonb not null,
  summary text default '',
  confidence text not null check (confidence in ('low', 'medium', 'high')),
  created_at timestamptz not null default now()
);

alter table entry_models enable row level security;

create policy "entry_models_owner_all"
  on entry_models
  for all
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

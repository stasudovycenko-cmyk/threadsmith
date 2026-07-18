-- Модуль 0 + заготовки под 1-3. Гнать в Supabase SQL Editor целиком.

create table users (
  id bigserial primary key,
  telegram_id bigint unique not null,
  referred_by bigint references users(id),
  credits_balance int not null default 0,
  created_at timestamptz default now()
);

create table oauth_states (
  state uuid primary key,
  user_id bigint references users(id),
  created_at timestamptz default now()
);

create table threads_accounts (
  id bigserial primary key,
  user_id bigint references users(id),
  threads_user_id text unique not null,
  username text,
  access_token_enc bytea not null,
  expires_at timestamptz not null,
  created_at timestamptz default now()
);

create table voice_profiles (
  user_id bigint primary key references users(id),
  profile_json jsonb not null,
  sample_posts jsonb,
  updated_at timestamptz default now()
);

create table authors (
  threads_author_id text primary key,
  username text,
  avg_views numeric,
  posts_tracked int default 0,
  updated_at timestamptz
);

create table posts_library (
  threads_post_id text primary key,
  niche text,
  author_id text references authors(threads_author_id),
  text text,
  metrics_json jsonb,
  virality_score numeric,
  hook_type text,
  fetched_at timestamptz default now()
);
create index on posts_library (niche, virality_score desc);

create table search_quota (
  threads_account_id bigint references threads_accounts(id),
  window_start date,
  used int default 0,
  primary key (threads_account_id, window_start)
);

create table generations (
  id bigserial primary key,
  user_id bigint references users(id),
  type text,
  input jsonb,
  output jsonb,
  credits_spent int,
  created_at timestamptz default now()
);

create table scheduled_posts (
  id bigserial primary key,
  user_id bigint references users(id),
  threads_account_id bigint references threads_accounts(id),
  text text not null,
  media_url text,
  link text,
  utm text,
  run_at timestamptz not null,
  status text default 'pending',
  threads_post_id text,
  error text
);
create index on scheduled_posts (status, run_at);

create table reply_rules (
  id bigserial primary key,
  user_id bigint references users(id),
  keyword text not null,
  response_text text not null,
  active bool default true
);

create table poll_state (
  threads_post_id text primary key,
  threads_account_id bigint,
  last_polled_at timestamptz,
  tier smallint default 0
);

create table replies_log (
  comment_id text primary key,
  threads_post_id text,
  matched_keyword text,
  replied_at timestamptz default now()
);

create table insights_snapshots (
  threads_post_id text,
  snapshot_date date,
  metrics_json jsonb,
  primary key (threads_post_id, snapshot_date)
);

create table subscriptions (
  user_id bigint primary key references users(id),
  plan text default 'free',
  status text default 'active',
  renews_at timestamptz
);

create table credits_ledger (
  id bigserial primary key,
  user_id bigint references users(id),
  delta int not null,
  reason text,
  created_at timestamptz default now()
);

-- Робокассе нужен уникальный int inv_id -> bigserial как PK
create table payments (
  inv_id bigserial primary key,
  user_id bigint references users(id),
  plan text not null,
  amount numeric not null,
  provider text not null default 'robokassa',
  status text not null default 'pending',   -- pending / paid / failed
  created_at timestamptz default now(),
  paid_at timestamptz
);

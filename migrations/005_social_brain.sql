-- Social Brain v1: durable user facts, strategy state, and future decisions.
-- This migration only adds new tables and indexes.

create table if not exists social_facts (
  id bigserial primary key,
  user_id bigint not null references users(id) on delete cascade,
  fact_type text not null,
  fact_key text not null,
  fact_value_json jsonb not null,
  confidence numeric(4, 3) not null default 1.000,
  source text not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint social_facts_confidence_range
    check (confidence >= 0 and confidence <= 1),
  constraint social_facts_type_not_empty
    check (btrim(fact_type) <> ''),
  constraint social_facts_key_not_empty
    check (btrim(fact_key) <> ''),
  constraint social_facts_source_not_empty
    check (btrim(source) <> ''),
  constraint social_facts_user_type_key_unique
    unique (user_id, fact_type, fact_key)
);

create index if not exists idx_social_facts_user_type
  on social_facts (user_id, fact_type);

create index if not exists idx_social_facts_user_updated
  on social_facts (user_id, updated_at desc);

create table if not exists user_strategy_state (
  user_id bigint primary key references users(id) on delete cascade,
  primary_goal text,
  secondary_goal text,
  strategy_json jsonb not null default '{}'::jsonb,
  autonomy_level text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists decision_log (
  id bigserial primary key,
  user_id bigint not null references users(id) on delete cascade,
  decision_type text not null,
  input_context_json jsonb not null default '{}'::jsonb,
  decision_json jsonb not null default '{}'::jsonb,
  reason_json jsonb,
  result_json jsonb,
  created_at timestamptz not null default now(),
  constraint decision_log_type_not_empty
    check (btrim(decision_type) <> '')
);

create index if not exists idx_decision_log_user_created
  on decision_log (user_id, created_at desc);

create index if not exists idx_decision_log_user_type_created
  on decision_log (user_id, decision_type, created_at desc);

-- Supporting indexes for deterministic 30-day context aggregation.
create index if not exists idx_social_brain_accounts_user_created
  on threads_accounts (user_id, created_at desc);

create index if not exists idx_social_brain_generations_user_created
  on generations (user_id, created_at desc);

create index if not exists idx_social_brain_posts_user_run_at
  on scheduled_posts (user_id, run_at desc);

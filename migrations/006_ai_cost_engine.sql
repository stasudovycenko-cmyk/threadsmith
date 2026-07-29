-- AI Cost Engine v1: one row per physical Anthropic call.

create table if not exists ai_usage_events (
  id bigserial primary key,
  user_id bigint references users(id) on delete set null,
  threads_account_id bigint references threads_accounts(id) on delete set null,
  feature text not null,
  model text not null,
  input_tokens integer not null default 0 check (input_tokens >= 0),
  output_tokens integer not null default 0 check (output_tokens >= 0),
  cache_read_tokens integer not null default 0
    check (cache_read_tokens >= 0),
  cache_creation_tokens integer not null default 0
    check (cache_creation_tokens >= 0),
  estimated_cost_usd numeric(16, 10) not null default 0
    check (estimated_cost_usd >= 0),
  reserved_cost_usd numeric(16, 10) not null default 0
    check (reserved_cost_usd >= 0),
  pricing_version text not null,
  attempt smallint not null check (attempt >= 1),
  status text not null
    check (status in ('reserved', 'success', 'failure')),
  request_id uuid not null,
  event_key text not null,
  run_id text,
  latency_ms integer check (latency_ms is null or latency_ms >= 0),
  failure_type text,
  created_at timestamptz not null default now(),
  completed_at timestamptz,
  constraint ai_usage_events_event_key_unique unique (event_key),
  constraint ai_usage_events_feature_not_empty check (btrim(feature) <> ''),
  constraint ai_usage_events_model_not_empty check (btrim(model) <> '')
);

create index if not exists idx_ai_usage_events_created_at
  on ai_usage_events (created_at);
create index if not exists idx_ai_usage_events_user_created_at
  on ai_usage_events (user_id, created_at);
create index if not exists idx_ai_usage_events_account_created_at
  on ai_usage_events (threads_account_id, created_at);
create index if not exists idx_ai_usage_events_feature_created_at
  on ai_usage_events (feature, created_at);
create index if not exists idx_ai_usage_events_run_created_at
  on ai_usage_events (run_id, created_at)
  where run_id is not null;

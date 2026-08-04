-- Deterministic, recommendation-only Autopilot Intelligence V1.
-- Run this whole file inside one PostgreSQL transaction.

do $migration_guard$
begin
  if to_regclass('autopilot_intelligence_migration_014') is not null then
    raise exception 'migration 014 is already applied';
  end if;
  if to_regclass('decision_runs') is not null then
    raise exception 'migration 014 found a partial schema; resolve it manually';
  end if;
  if to_regclass('ux_v2_migration_013') is null
     or to_regclass('threads_accounts') is null then
    raise exception 'migration 014 requires migrations 010 through 013';
  end if;
end
$migration_guard$;

create table decision_runs (
  id bigserial primary key,
  user_id bigint not null references users(id) on delete cascade,
  threads_account_id bigint not null,
  context_hash text not null,
  decision_hash text not null,
  status text not null,
  health_score smallint not null,
  priority smallint not null,
  reason_codes text[] not null default '{}'::text[],
  result_json jsonb not null,
  next_check timestamptz not null,
  bucket_start timestamptz not null,
  created_at timestamptz not null default now(),
  constraint decision_runs_account_owner_fk
    foreign key (threads_account_id, user_id)
    references threads_accounts (id, user_id)
    on delete cascade,
  constraint decision_runs_context_hash_check
    check (context_hash ~ '^[0-9a-f]{64}$'),
  constraint decision_runs_decision_hash_check
    check (decision_hash ~ '^[0-9a-f]{64}$'),
  constraint decision_runs_status_check check (
    status in (
      'healthy', 'attention', 'blocked', 'waiting', 'insufficient_data'
    )
  ),
  constraint decision_runs_scores_check check (
    health_score between 0 and 100 and priority between 0 and 100
  ),
  constraint decision_runs_result_object
    check (jsonb_typeof(result_json) = 'object'),
  constraint decision_runs_result_summary_consistent check (
    result_json ? 'status'
    and result_json ? 'health_score'
    and result_json ->> 'status' = status
    and (result_json ->> 'health_score')::integer = health_score
  ),
  constraint decision_runs_next_check_order
    check (next_check >= bucket_start),
  constraint decision_runs_context_bucket_unique
    unique (threads_account_id, context_hash, bucket_start)
);

create index decision_runs_account_history_idx
  on decision_runs (threads_account_id, created_at desc, id desc);

create index decision_runs_retention_idx
  on decision_runs (created_at);

create table autopilot_intelligence_migration_014 (
  singleton boolean primary key default true check (singleton),
  applied_at timestamptz not null default now()
);

insert into autopilot_intelligence_migration_014 (singleton) values (true);

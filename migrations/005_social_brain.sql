-- Social Brain v1: account-scoped intelligence state and event history.
-- This migration has not been deployed yet, so it defines the final v1
-- storage model directly rather than layering a replacement migration.

-- The account id remains the primary key. This pair lets every Brain row
-- prove that the referenced Threads account belongs to the same user.
create unique index if not exists
  idx_social_brain_account_owner_unique
  on threads_accounts (id, user_id);

create table if not exists brains (
  id bigserial primary key,
  user_id bigint not null references users(id) on delete cascade,
  threads_account_id bigint not null,
  dna jsonb not null default '{}'::jsonb,
  audience jsonb not null default '{}'::jsonb,
  goals jsonb not null default '{}'::jsonb,
  constraints jsonb not null default '{}'::jsonb,
  performance jsonb not null default '{}'::jsonb,
  version integer not null default 1,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint brains_account_owner_fk
    foreign key (threads_account_id, user_id)
    references threads_accounts (id, user_id)
    on delete cascade,
  constraint brains_owner_account_unique
    unique (user_id, threads_account_id),
  constraint brains_version_positive
    check (version >= 1),
  constraint brains_dna_object
    check (jsonb_typeof(dna) = 'object'),
  constraint brains_audience_object
    check (jsonb_typeof(audience) = 'object'),
  constraint brains_goals_object
    check (jsonb_typeof(goals) = 'object'),
  constraint brains_constraints_object
    check (jsonb_typeof(constraints) = 'object'),
  constraint brains_performance_object
    check (jsonb_typeof(performance) = 'object')
);

create table if not exists brain_patterns (
  id bigserial primary key,
  brain_id bigint not null references brains(id) on delete cascade,
  kind text not null,
  key text not null,
  metric text not null,
  lift double precision not null default 0,
  samples integer not null default 0,
  confidence double precision not null default 0,
  updated_at timestamptz not null default now(),
  constraint brain_patterns_identity_unique
    unique (brain_id, kind, key, metric),
  constraint brain_patterns_kind_not_empty
    check (btrim(kind) <> ''),
  constraint brain_patterns_key_not_empty
    check (btrim(key) <> ''),
  constraint brain_patterns_metric_not_empty
    check (btrim(metric) <> ''),
  constraint brain_patterns_samples_nonnegative
    check (samples >= 0),
  constraint brain_patterns_confidence_range
    check (confidence >= 0 and confidence <= 1)
);

create index if not exists idx_brain_patterns_brain_confidence
  on brain_patterns (brain_id, confidence desc, samples desc);

create table if not exists brain_events (
  id bigserial primary key,
  brain_id bigint not null references brains(id) on delete cascade,
  type text not null,
  payload jsonb not null default '{}'::jsonb,
  source_type text,
  source_id text,
  event_key text,
  occurred_at timestamptz not null,
  created_at timestamptz not null default now(),
  constraint brain_events_type_not_empty
    check (btrim(type) <> ''),
  constraint brain_events_payload_object
    check (jsonb_typeof(payload) = 'object'),
  constraint brain_events_event_key_not_empty
    check (event_key is null or btrim(event_key) <> '')
);

create unique index if not exists uq_brain_events_natural_identity
  on brain_events (brain_id, event_key)
  where event_key is not null;

create index if not exists idx_brain_events_brain_type_occurred
  on brain_events (brain_id, type, occurred_at desc);

-- Supporting index for account-specific rolling performance backfill.
create index if not exists idx_social_brain_posts_user_run_at
  on scheduled_posts (
    user_id,
    threads_account_id,
    run_at desc
  );

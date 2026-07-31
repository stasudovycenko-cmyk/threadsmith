-- Account-scoped auto-post run history and schedule timezone.

alter table autocontent_settings
  add column if not exists timezone text not null
  default 'Europe/Moscow';

alter table scheduled_posts
  add column if not exists publish_started_at timestamptz;

create table if not exists autopost_runs (
  id bigserial primary key,
  user_id bigint not null references users(id) on delete cascade,
  threads_account_id bigint not null
    references threads_accounts(id) on delete cascade,
  scheduled_post_id bigint unique
    references scheduled_posts(id) on delete set null,
  scheduled_at timestamptz not null,
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  status text not null default 'pending',
  threads_post_id text,
  error_code text,
  safe_error_message text,
  created_at timestamptz not null default now(),
  constraint autopost_runs_status_check check (
    status in ('success', 'failed', 'skipped', 'pending')
  ),
  constraint autopost_runs_error_code_check check (
    error_code is null or error_code in (
      'AUTH_EXPIRED',
      'PERMISSION_DENIED',
      'THREADS_TEMPORARY_ERROR',
      'INSUFFICIENT_CREDITS',
      'GENERATION_FAILED',
      'QUALITY_FAILED',
      'UNKNOWN_ERROR'
    )
  ),
  constraint autopost_runs_slot_unique unique (
    threads_account_id,
    scheduled_at
  )
);

create index if not exists autopost_runs_account_history_idx
  on autopost_runs (user_id, threads_account_id, started_at desc);

create index if not exists autopost_runs_pending_idx
  on autopost_runs (scheduled_at)
  where status = 'pending';

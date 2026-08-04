-- Account-scoped Telegram UX preferences and resumable onboarding.
-- Run this whole file inside one PostgreSQL transaction.

do $migration_guard$
begin
  if to_regclass('ux_v2_migration_013') is not null then
    raise exception 'migration 013 is already applied';
  end if;
  if to_regclass('user_preferences') is null
     or to_regclass('threads_accounts') is null then
    raise exception 'migration 013 requires migration 010';
  end if;
end
$migration_guard$;

alter table user_preferences
  add column interface_mode text;

-- Existing users keep the complete interface. New preference rows start in
-- simple mode; this changes visibility only and never enables automation.
update user_preferences
set interface_mode = 'advanced'
where interface_mode is null;

alter table user_preferences
  alter column interface_mode set default 'simple',
  alter column interface_mode set not null,
  add constraint user_preferences_interface_mode_check
    check (interface_mode in ('simple', 'advanced'));

create table ux_onboarding (
  user_id bigint not null references users(id) on delete cascade,
  threads_account_id bigint not null,
  status text not null default 'not_started',
  current_step smallint not null default 0,
  data jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (user_id, threads_account_id),
  constraint ux_onboarding_account_owner_fk
    foreign key (threads_account_id, user_id)
    references threads_accounts (id, user_id)
    on delete cascade,
  constraint ux_onboarding_status_check check (
    status in ('not_started', 'in_progress', 'completed', 'skipped')
  ),
  constraint ux_onboarding_step_check
    check (current_step between 0 and 9),
  constraint ux_onboarding_data_object
    check (jsonb_typeof(data) = 'object')
);

create index ux_onboarding_account_status_idx
  on ux_onboarding (threads_account_id, status, updated_at desc);

create table ux_v2_migration_013 (
  singleton boolean primary key default true check (singleton),
  applied_at timestamptz not null default now(),
  existing_preferences_preserved bigint not null default 0
);

insert into ux_v2_migration_013 (
  singleton, existing_preferences_preserved
)
select true, count(*) from user_preferences;

-- Threads account cabinet migration 010.
-- Run this whole file inside one PostgreSQL transaction. Do not split it.
-- The *_user_backup_010 tables are intentionally retained for rollback.

do $migration_guard$
declare
  marker_exists boolean :=
    to_regclass('threads_account_cabinet_migration_010') is not null;
  preferences_exist boolean := to_regclass('user_preferences') is not null;
  autocontent_backup_exists boolean :=
    to_regclass('autocontent_settings_user_backup_010') is not null;
  neuro_backup_exists boolean :=
    to_regclass('neuro_settings_user_backup_010') is not null;
  deletion_table_exists boolean :=
    to_regclass('threads_data_deletion_requests') is not null;
  connection_status_exists boolean := exists (
    select 1
    from information_schema.columns
    where table_schema = current_schema()
      and table_name = 'threads_accounts'
      and column_name = 'connection_status'
  );
  autocontent_account_scoped boolean := exists (
    select 1
    from information_schema.columns
    where table_schema = current_schema()
      and table_name = 'autocontent_settings'
      and column_name = 'threads_account_id'
  );
  neuro_account_scoped boolean := exists (
    select 1
    from information_schema.columns
    where table_schema = current_schema()
      and table_name = 'neuro_settings'
      and column_name = 'threads_account_id'
  );
  oauth_action_exists boolean := exists (
    select 1
    from information_schema.columns
    where table_schema = current_schema()
      and table_name = 'oauth_states'
      and column_name = 'action'
  );
  oauth_expected_account_exists boolean := exists (
    select 1
    from information_schema.columns
    where table_schema = current_schema()
      and table_name = 'oauth_states'
      and column_name = 'expected_threads_account_id'
  );
  neuro_comment_account_scoped boolean := exists (
    select 1
    from information_schema.columns
    where table_schema = current_schema()
      and table_name = 'neuro_comments'
      and column_name = 'threads_account_id'
  );
begin
  if marker_exists
     and preferences_exist
     and connection_status_exists
     and autocontent_account_scoped
     and neuro_account_scoped
     and autocontent_backup_exists
     and neuro_backup_exists
     and deletion_table_exists
     and oauth_action_exists
     and oauth_expected_account_exists
     and neuro_comment_account_scoped then
    raise exception using
      errcode = '55000',
      message = 'migration 010 is already applied; refusing a repeat run';
  end if;

  if marker_exists
     or preferences_exist
     or connection_status_exists
     or autocontent_account_scoped
     or neuro_account_scoped
     or autocontent_backup_exists
     or neuro_backup_exists
     or deletion_table_exists
     or oauth_action_exists
     or oauth_expected_account_exists
     or neuro_comment_account_scoped then
    raise exception using
      errcode = '55000',
      message = 'migration 010 found an inconsistent partial/applied shape; run preflight and resolve manually';
  end if;

  if to_regclass('autocontent_settings') is null
     or to_regclass('neuro_settings') is null then
    raise exception 'migration 010 requires the legacy settings tables';
  end if;

  if exists (select 1 from threads_accounts where user_id is null) then
    raise exception 'migration 010 blocked: threads account with NULL owner';
  end if;

  if exists (
    select threads_user_id
    from threads_accounts
    group by threads_user_id
    having count(*) > 1
  ) then
    raise exception 'migration 010 blocked: duplicate Threads ownership';
  end if;

  if exists (
    select 1
    from autocontent_settings setting
    left join users owner on owner.id = setting.user_id
    where owner.id is null
  ) or exists (
    select 1
    from neuro_settings setting
    left join users owner on owner.id = setting.user_id
    where owner.id is null
  ) then
    raise exception 'migration 010 blocked: orphan legacy settings';
  end if;

  -- Only comments for single-account users will be attributed. Detect any
  -- conflict before dropping the old uniqueness constraint; never dedupe.
  if exists (
    select single_account.account_id, comment.target_post_id
    from neuro_comments comment
    join (
      select user_id, min(id) as account_id
      from threads_accounts
      group by user_id
      having count(*) = 1
    ) single_account on single_account.user_id = comment.user_id
    group by single_account.account_id, comment.target_post_id
    having count(*) > 1
  ) then
    raise exception
      'migration 010 blocked: duplicate neuro target for a single account';
  end if;
end
$migration_guard$;

alter table threads_accounts
  alter column access_token_enc drop not null;

alter table threads_accounts
  add column if not exists connection_status text not null
    default 'connected',
  add column if not exists disconnected_at timestamptz;

do $constraint$
begin
  if not exists (
    select 1 from pg_constraint
    where conrelid = 'threads_accounts'::regclass
      and conname = 'threads_accounts_connection_status_check'
  ) then
    alter table threads_accounts
      add constraint threads_accounts_connection_status_check check (
        connection_status in ('connected', 'disconnected', 'error')
      );
  end if;

  if not exists (
    select 1 from pg_constraint
    where conrelid = 'threads_accounts'::regclass
      and conname = 'threads_accounts_id_user_id_key'
  ) then
    alter table threads_accounts
      add constraint threads_accounts_id_user_id_key
      unique (id, user_id);
  end if;
end
$constraint$;

create table user_preferences (
  user_id bigint primary key references users(id) on delete cascade,
  selected_threads_account_id bigint,
  updated_at timestamptz not null default now()
);

do $constraint$
begin
  if not exists (
    select 1 from pg_constraint
    where conrelid = 'user_preferences'::regclass
      and conname = 'user_preferences_selected_owner_fk'
  ) then
    -- Application code selects a replacement or clears the value before an
    -- account delete. NO ACTION preserves the preferences row if it does not.
    alter table user_preferences
      add constraint user_preferences_selected_owner_fk
      foreign key (selected_threads_account_id, user_id)
      references threads_accounts (id, user_id)
      on delete no action;
  end if;
end
$constraint$;

insert into user_preferences (user_id, selected_threads_account_id)
select distinct on (account.user_id)
  account.user_id,
  account.id
from threads_accounts account
order by
  account.user_id,
  account.created_at desc nulls last,
  account.id desc;

alter table oauth_states
  add column if not exists action text not null default 'connect',
  add column if not exists expected_threads_account_id bigint;

-- Existing states predate reconnect semantics and must remain connect states.
update oauth_states
set action = 'connect', expected_threads_account_id = null;

do $oauth_validation$
begin
  if exists (
    select 1 from oauth_states
    where (action = 'connect' and expected_threads_account_id is not null)
       or (action = 'reconnect' and expected_threads_account_id is null)
       or action not in ('connect', 'reconnect')
  ) then
    raise exception 'migration 010 blocked: invalid OAuth action state';
  end if;

  if exists (
    select 1
    from oauth_states state
    left join threads_accounts account
      on account.id = state.expected_threads_account_id
     and account.user_id = state.user_id
    where state.action = 'reconnect'
      and account.id is null
  ) then
    raise exception 'migration 010 blocked: reconnect state ownership mismatch';
  end if;
end
$oauth_validation$;

do $constraint$
begin
  if not exists (
    select 1 from pg_constraint
    where conrelid = 'oauth_states'::regclass
      and conname = 'oauth_states_action_check'
  ) then
    alter table oauth_states
      add constraint oauth_states_action_check check (
        (action = 'connect' and expected_threads_account_id is null)
        or
        (action = 'reconnect' and expected_threads_account_id is not null)
      );
  end if;

  if not exists (
    select 1 from pg_constraint
    where conrelid = 'oauth_states'::regclass
      and conname = 'oauth_states_expected_owner_fk'
  ) then
    alter table oauth_states
      add constraint oauth_states_expected_owner_fk
      foreign key (expected_threads_account_id, user_id)
      references threads_accounts (id, user_id)
      on delete cascade;
  end if;
end
$constraint$;

-- Some production installations received these columns operationally.
alter table autocontent_settings
  add column if not exists topics text not null default '',
  add column if not exists slots text not null default '',
  add column if not exists days text not null default 'all',
  add column if not exists goal text not null default '',
  add column if not exists timezone text not null default 'Europe/Moscow';

alter table autocontent_settings
  rename to autocontent_settings_user_backup_010;

comment on table autocontent_settings_user_backup_010 is
  'Temporary migration 010 backup of user-scoped settings; runtime code must not use this table.';

create table autocontent_settings (
  threads_account_id bigint not null,
  user_id bigint not null references users(id) on delete cascade,
  active boolean not null default false,
  posts_per_day integer not null default 1
    check (posts_per_day between 0 and 5),
  topics text not null default '',
  slots text not null default '',
  days text not null default 'all'
    check (days in ('all', 'weekdays')),
  goal text not null default '',
  timezone text not null default 'Europe/Moscow',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint autocontent_settings_account_pkey
    primary key (threads_account_id)
);

do $constraint$
begin
  if not exists (
    select 1 from pg_constraint
    where conrelid = 'autocontent_settings'::regclass
      and conname = 'autocontent_settings_account_owner_fk'
  ) then
    alter table autocontent_settings
      add constraint autocontent_settings_account_owner_fk
      foreign key (threads_account_id, user_id)
      references threads_accounts (id, user_id)
      on delete cascade;
  end if;
end
$constraint$;

insert into autocontent_settings (
  threads_account_id, user_id, active, posts_per_day,
  topics, slots, days, goal, timezone, created_at
)
select
  account.id,
  account.user_id,
  coalesce(backup.active, false),
  greatest(0, least(5, coalesce(backup.posts_per_day, 1))),
  coalesce(backup.topics, ''),
  coalesce(backup.slots, ''),
  case when backup.days = 'weekdays' then 'weekdays' else 'all' end,
  coalesce(backup.goal, ''),
  coalesce(backup.timezone, 'Europe/Moscow'),
  coalesce(backup.created_at, now())
from threads_accounts account
left join autocontent_settings_user_backup_010 backup
  on backup.user_id = account.user_id;

create index autocontent_settings_account_owner_idx
  on autocontent_settings (user_id, threads_account_id);

do $autocontent_validation$
declare
  expected_count bigint;
  actual_count bigint;
begin
  select count(*) into expected_count
  from threads_accounts where user_id is not null;
  select count(*) into actual_count from autocontent_settings;

  if actual_count <> expected_count then
    raise exception
      'migration 010 autocontent count mismatch: expected %, got %',
      expected_count, actual_count;
  end if;
  if exists (
    select 1
    from threads_accounts account
    left join autocontent_settings setting
      on setting.threads_account_id = account.id
    where account.user_id is not null and setting.threads_account_id is null
  ) then
    raise exception 'migration 010 autocontent has missing account rows';
  end if;
  if exists (
    select threads_account_id from autocontent_settings
    group by threads_account_id having count(*) <> 1
  ) then
    raise exception 'migration 010 autocontent has duplicate account rows';
  end if;
  if exists (
    select 1
    from autocontent_settings setting
    join threads_accounts account on account.id = setting.threads_account_id
    where setting.user_id <> account.user_id
  ) then
    raise exception 'migration 010 autocontent ownership mismatch';
  end if;
  if exists (
    select 1 from autocontent_settings
    where posts_per_day not between 0 and 5
       or timezone is null
       or slots is null
       or topics is null
       or days is null
       or goal is null
  ) then
    raise exception 'migration 010 autocontent contains invalid values';
  end if;
end
$autocontent_validation$;

alter table neuro_settings
  rename to neuro_settings_user_backup_010;

comment on table neuro_settings_user_backup_010 is
  'Temporary migration 010 backup of user-scoped settings; runtime code must not use this table.';

create table neuro_settings (
  threads_account_id bigint not null,
  user_id bigint not null references users(id) on delete cascade,
  active boolean not null default false,
  mode text not null default 'approve'
    check (mode in ('approve', 'auto')),
  daily_cap integer not null default 10
    check (daily_cap between 1 and 30),
  created_at timestamptz not null default now(),
  constraint neuro_settings_account_pkey
    primary key (threads_account_id)
);

do $constraint$
begin
  if not exists (
    select 1 from pg_constraint
    where conrelid = 'neuro_settings'::regclass
      and conname = 'neuro_settings_account_owner_fk'
  ) then
    alter table neuro_settings
      add constraint neuro_settings_account_owner_fk
      foreign key (threads_account_id, user_id)
      references threads_accounts (id, user_id)
      on delete cascade;
  end if;
end
$constraint$;

insert into neuro_settings (
  threads_account_id, user_id, active, mode, daily_cap, created_at
)
select
  account.id,
  account.user_id,
  coalesce(backup.active, false),
  case when backup.mode = 'auto' then 'auto' else 'approve' end,
  greatest(1, least(30, coalesce(backup.daily_cap, 10))),
  coalesce(backup.created_at, now())
from threads_accounts account
left join neuro_settings_user_backup_010 backup
  on backup.user_id = account.user_id;

create index neuro_settings_account_owner_idx
  on neuro_settings (user_id, threads_account_id);

do $neuro_validation$
declare
  expected_count bigint;
  actual_count bigint;
begin
  select count(*) into expected_count
  from threads_accounts where user_id is not null;
  select count(*) into actual_count from neuro_settings;

  if actual_count <> expected_count then
    raise exception
      'migration 010 neuro count mismatch: expected %, got %',
      expected_count, actual_count;
  end if;
  if exists (
    select 1
    from threads_accounts account
    left join neuro_settings setting
      on setting.threads_account_id = account.id
    where account.user_id is not null and setting.threads_account_id is null
  ) then
    raise exception 'migration 010 neuro has missing account rows';
  end if;
  if exists (
    select threads_account_id from neuro_settings
    group by threads_account_id having count(*) <> 1
  ) then
    raise exception 'migration 010 neuro has duplicate account rows';
  end if;
  if exists (
    select 1
    from neuro_settings setting
    join threads_accounts account on account.id = setting.threads_account_id
    where setting.user_id <> account.user_id
  ) then
    raise exception 'migration 010 neuro ownership mismatch';
  end if;
  if exists (
    select 1 from neuro_settings
    where mode not in ('approve', 'auto')
       or daily_cap not between 1 and 30
  ) then
    raise exception 'migration 010 neuro contains invalid values';
  end if;
end
$neuro_validation$;

alter table neuro_comments
  add column if not exists threads_account_id bigint
    references threads_accounts(id) on delete set null;

update neuro_comments comment
set threads_account_id = single_account.account_id
from (
  select user_id, min(id) as account_id
  from threads_accounts
  group by user_id
  having count(*) = 1
) single_account
where single_account.user_id = comment.user_id
  and comment.threads_account_id is null;

do $neuro_comment_validation$
begin
  if exists (
    select threads_account_id, target_post_id
    from neuro_comments
    where threads_account_id is not null
    group by threads_account_id, target_post_id
    having count(*) > 1
  ) then
    raise exception
      'migration 010 blocked: duplicate account-scoped neuro target';
  end if;
end
$neuro_comment_validation$;

alter table neuro_comments
  drop constraint if exists neuro_comments_user_id_target_post_id_key;

create unique index if not exists neuro_comments_account_target_unique
  on neuro_comments (threads_account_id, target_post_id)
  where threads_account_id is not null;

create table if not exists threads_data_deletion_requests (
  confirmation_code text primary key,
  threads_user_id_hash text not null,
  status text not null check (status in ('received', 'completed', 'failed')),
  requested_at timestamptz not null default now(),
  completed_at timestamptz
);

-- No secondary indexes are added: runtime reads this table only by the
-- confirmation_code primary key. Status, timestamp, and hash are not queried.

create table threads_account_cabinet_migration_010 (
  singleton boolean primary key default true check (singleton),
  applied_at timestamptz not null default now(),
  user_ids bigint[] not null,
  account_ids bigint[] not null,
  autocontent_backup_fingerprint text not null,
  neuro_backup_fingerprint text not null,
  autocontent_fingerprint text not null,
  neuro_fingerprint text not null
);

comment on table threads_account_cabinet_migration_010 is
  'Migration 010 marker and conservative rollback fingerprints; keep until cleanup migration.';

insert into threads_account_cabinet_migration_010 (
  singleton, user_ids, account_ids,
  autocontent_backup_fingerprint, neuro_backup_fingerprint,
  autocontent_fingerprint, neuro_fingerprint
)
select
  true,
  coalesce(
    (select array_agg(id order by id) from users),
    '{}'::bigint[]
  ),
  coalesce(
    (select array_agg(id order by id) from threads_accounts),
    '{}'::bigint[]
  ),
  md5(coalesce(
    (select jsonb_agg(to_jsonb(setting) order by user_id)::text
     from autocontent_settings_user_backup_010 setting),
    '[]'
  )),
  md5(coalesce(
    (select jsonb_agg(to_jsonb(setting) order by user_id)::text
     from neuro_settings_user_backup_010 setting),
    '[]'
  )),
  md5(coalesce(
    (select jsonb_agg(to_jsonb(setting) order by threads_account_id)::text
     from autocontent_settings setting),
    '[]'
  )),
  md5(coalesce(
    (select jsonb_agg(to_jsonb(setting) order by threads_account_id)::text
     from neuro_settings setting),
    '[]'
  ));

do $final_validation$
begin
  if (select count(*) from threads_account_cabinet_migration_010) <> 1 then
    raise exception 'migration 010 marker validation failed';
  end if;
  if exists (
    select 1 from threads_accounts
    where connection_status = 'connected' and access_token_enc is null
  ) then
    raise exception 'migration 010 connected account has no token';
  end if;
  if exists (
    select 1
    from user_preferences preference
    left join threads_accounts account
      on account.id = preference.selected_threads_account_id
     and account.user_id = preference.user_id
    where preference.selected_threads_account_id is not null
      and account.id is null
  ) then
    raise exception 'migration 010 selected account ownership mismatch';
  end if;
end
$final_validation$;

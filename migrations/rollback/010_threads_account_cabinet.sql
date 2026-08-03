-- Conservative rollback for Threads account cabinet migration 010.
-- Run this whole file inside one PostgreSQL transaction. Do not split it.
-- It restores the exact user-scoped backup tables and refuses data loss.

do $rollback_guard$
declare
  marker_user_ids bigint[];
  marker_account_ids bigint[];
  marker_autocontent_backup_fingerprint text;
  marker_neuro_backup_fingerprint text;
  marker_autocontent_fingerprint text;
  marker_neuro_fingerprint text;
  current_user_ids bigint[];
  current_account_ids bigint[];
  current_autocontent_backup_fingerprint text;
  current_neuro_backup_fingerprint text;
  current_autocontent_fingerprint text;
  current_neuro_fingerprint text;
begin
  if to_regclass('threads_account_cabinet_migration_010') is null then
    raise exception
      'rollback 010 blocked: migration marker is missing';
  end if;
  if to_regclass('autocontent_settings_user_backup_010') is null
     or to_regclass('neuro_settings_user_backup_010') is null then
    raise exception
      'rollback 010 blocked: required backup tables are missing';
  end if;
  if not exists (
    select 1 from information_schema.columns
    where table_schema = current_schema()
      and table_name = 'autocontent_settings'
      and column_name = 'threads_account_id'
  ) or not exists (
    select 1 from information_schema.columns
    where table_schema = current_schema()
      and table_name = 'neuro_settings'
      and column_name = 'threads_account_id'
  ) then
    raise exception
      'rollback 010 blocked: canonical settings tables are not account-scoped';
  end if;

  if (select count(*) from threads_account_cabinet_migration_010) <> 1 then
    raise exception
      'rollback 010 blocked: migration marker is missing or malformed';
  end if;

  select
    user_ids,
    account_ids,
    autocontent_backup_fingerprint,
    neuro_backup_fingerprint,
    autocontent_fingerprint,
    neuro_fingerprint
  into
    marker_user_ids,
    marker_account_ids,
    marker_autocontent_backup_fingerprint,
    marker_neuro_backup_fingerprint,
    marker_autocontent_fingerprint,
    marker_neuro_fingerprint
  from threads_account_cabinet_migration_010
  where singleton;

  if exists (
    select 1 from scheduled_posts where status = 'publishing'
  ) then
    raise exception
      'rollback 010 blocked: a publication is in progress';
  end if;

  select coalesce(array_agg(id order by id), '{}'::bigint[])
  into current_user_ids from users;
  select coalesce(array_agg(id order by id), '{}'::bigint[])
  into current_account_ids from threads_accounts;

  if current_user_ids is distinct from marker_user_ids then
    raise exception
      'rollback 010 blocked: users changed after migration';
  end if;
  if current_account_ids is distinct from marker_account_ids then
    raise exception
      'rollback 010 blocked: Threads accounts changed after migration';
  end if;

  select md5(coalesce(
    jsonb_agg(to_jsonb(setting) order by user_id)::text,
    '[]'
  )) into current_autocontent_backup_fingerprint
  from autocontent_settings_user_backup_010 setting;
  select md5(coalesce(
    jsonb_agg(to_jsonb(setting) order by user_id)::text,
    '[]'
  )) into current_neuro_backup_fingerprint
  from neuro_settings_user_backup_010 setting;
  select md5(coalesce(
    jsonb_agg(to_jsonb(setting) order by threads_account_id)::text,
    '[]'
  )) into current_autocontent_fingerprint
  from autocontent_settings setting;
  select md5(coalesce(
    jsonb_agg(to_jsonb(setting) order by threads_account_id)::text,
    '[]'
  )) into current_neuro_fingerprint
  from neuro_settings setting;

  if current_autocontent_backup_fingerprint is distinct from
       marker_autocontent_backup_fingerprint
     or current_neuro_backup_fingerprint is distinct from
       marker_neuro_backup_fingerprint then
    raise exception
      'rollback 010 blocked: migration backup tables were modified';
  end if;
  if current_autocontent_fingerprint is distinct from
       marker_autocontent_fingerprint
     or current_neuro_fingerprint is distinct from
       marker_neuro_fingerprint then
    raise exception
      'rollback 010 blocked: account settings changed after migration';
  end if;

  -- Pre-010 workers decrypt every selected token and do not understand
  -- disconnected/error states. Never manufacture a replacement token.
  if exists (
    select 1 from threads_accounts
    where access_token_enc is null or connection_status <> 'connected'
  ) then
    raise exception
      'rollback 010 blocked: reconnect disconnected/error accounts first';
  end if;

  if exists (select 1 from threads_data_deletion_requests) then
    raise exception
      'rollback 010 blocked: data deletion requests exist';
  end if;
  if exists (
    select 1 from oauth_states
    where action <> 'connect' or expected_threads_account_id is not null
  ) then
    raise exception
      'rollback 010 blocked: reconnect OAuth states exist';
  end if;
  if exists (
    select user_id, target_post_id
    from neuro_comments
    group by user_id, target_post_id
    having count(*) > 1
  ) then
    raise exception
      'rollback 010 blocked: account-scoped neuro comments cannot be collapsed';
  end if;
end
$rollback_guard$;

drop table if exists autocontent_settings;
alter table autocontent_settings_user_backup_010
  rename to autocontent_settings;
comment on table autocontent_settings is null;

drop table if exists neuro_settings;
alter table neuro_settings_user_backup_010
  rename to neuro_settings;
comment on table neuro_settings is null;

drop index if exists neuro_comments_account_target_unique;
alter table neuro_comments
  drop column if exists threads_account_id;

do $constraint$
begin
  if not exists (
    select 1 from pg_constraint
    where conrelid = 'neuro_comments'::regclass
      and conname = 'neuro_comments_user_id_target_post_id_key'
  ) then
    alter table neuro_comments
      add constraint neuro_comments_user_id_target_post_id_key
      unique (user_id, target_post_id);
  end if;
end
$constraint$;

drop table if exists threads_data_deletion_requests;

alter table oauth_states
  drop constraint if exists oauth_states_expected_owner_fk,
  drop constraint if exists oauth_states_action_check,
  drop column if exists expected_threads_account_id,
  drop column if exists action;

drop table if exists user_preferences;

alter table threads_accounts
  alter column access_token_enc set not null,
  drop constraint if exists threads_accounts_connection_status_check,
  drop constraint if exists threads_accounts_id_user_id_key,
  drop column if exists disconnected_at,
  drop column if exists connection_status;

drop table if exists threads_account_cabinet_migration_010;

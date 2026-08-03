-- Lossy rollback: one account's settings are retained per user.
-- It refuses to continue while soft-disconnected accounts have null tokens.

do $$
begin
  if exists (
    select 1 from threads_accounts where access_token_enc is null
  ) then
    raise exception
      'Reconnect or permanently delete disconnected accounts before rollback';
  end if;
  if exists (
    select 1
    from neuro_comments
    group by user_id, target_post_id
    having count(*) > 1
  ) then
    raise exception
      'Cannot collapse account-scoped neuro comments without data loss';
  end if;
end $$;

alter table autocontent_settings
  rename to autocontent_settings_account_legacy;

create table autocontent_settings (
  user_id bigint primary key references users(id),
  active boolean default false,
  posts_per_day integer default 1,
  topics text not null default '',
  slots text not null default '',
  days text not null default 'all',
  goal text not null default '',
  timezone text not null default 'Europe/Moscow',
  created_at timestamptz default now()
);

insert into autocontent_settings (
  user_id, active, posts_per_day, topics, slots,
  days, goal, timezone, created_at
)
select distinct on (settings.user_id)
  settings.user_id,
  settings.active,
  settings.posts_per_day,
  settings.topics,
  settings.slots,
  settings.days,
  settings.goal,
  settings.timezone,
  settings.created_at
from autocontent_settings_account_legacy settings
left join user_preferences preference
  on preference.user_id = settings.user_id
order by
  settings.user_id,
  (settings.threads_account_id =
    preference.selected_threads_account_id) desc,
  settings.threads_account_id desc;

drop table autocontent_settings_account_legacy;

alter table neuro_settings
  rename to neuro_settings_account_legacy;

create table neuro_settings (
  user_id bigint primary key references users(id),
  active boolean default false,
  mode text default 'approve',
  daily_cap integer default 10,
  created_at timestamptz default now()
);

insert into neuro_settings (
  user_id, active, mode, daily_cap, created_at
)
select distinct on (settings.user_id)
  settings.user_id,
  settings.active,
  settings.mode,
  settings.daily_cap,
  settings.created_at
from neuro_settings_account_legacy settings
left join user_preferences preference
  on preference.user_id = settings.user_id
order by
  settings.user_id,
  (settings.threads_account_id =
    preference.selected_threads_account_id) desc,
  settings.threads_account_id desc;

drop table neuro_settings_account_legacy;

drop index neuro_comments_account_target_unique;
alter table neuro_comments drop column threads_account_id;
alter table neuro_comments
  add constraint neuro_comments_user_id_target_post_id_key
  unique (user_id, target_post_id);

drop table threads_data_deletion_requests;

alter table oauth_states
  drop constraint oauth_states_expected_owner_fk,
  drop constraint oauth_states_action_check,
  drop column expected_threads_account_id,
  drop column action;

drop table user_preferences;

alter table threads_accounts
  alter column access_token_enc set not null,
  drop constraint threads_accounts_connection_status_check,
  drop column disconnected_at,
  drop column connection_status;

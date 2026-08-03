-- Threads account cabinet, safe OAuth reconnect, and account-scoped settings.

alter table threads_accounts
  alter column access_token_enc drop not null;

alter table threads_accounts
  add column if not exists connection_status text not null
    default 'connected',
  add column if not exists disconnected_at timestamptz;

alter table threads_accounts
  add constraint threads_accounts_connection_status_check check (
    connection_status in ('connected', 'disconnected', 'error')
  );

create unique index if not exists
  idx_social_brain_account_owner_unique
  on threads_accounts (id, user_id);

create table user_preferences (
  user_id bigint primary key references users(id) on delete cascade,
  selected_threads_account_id bigint,
  updated_at timestamptz not null default now(),
  constraint user_preferences_selected_owner_fk
    foreign key (selected_threads_account_id, user_id)
    references threads_accounts (id, user_id)
    on delete cascade
);

insert into user_preferences (user_id, selected_threads_account_id)
select distinct on (account.user_id)
  account.user_id,
  account.id
from threads_accounts account
where account.user_id is not null
order by account.user_id, account.created_at desc, account.id desc
on conflict (user_id) do nothing;

alter table oauth_states
  add column if not exists action text not null default 'connect',
  add column if not exists expected_threads_account_id bigint;

alter table oauth_states
  add constraint oauth_states_action_check check (
    (action = 'connect' and expected_threads_account_id is null)
    or
    (action = 'reconnect' and expected_threads_account_id is not null)
  ),
  add constraint oauth_states_expected_owner_fk
    foreign key (expected_threads_account_id, user_id)
    references threads_accounts (id, user_id)
    on delete cascade;

-- Some production installations received these columns operationally.
-- Defining them here makes the account-scoped replacement reproducible.
alter table autocontent_settings
  add column if not exists topics text not null default '',
  add column if not exists slots text not null default '',
  add column if not exists days text not null default 'all',
  add column if not exists goal text not null default '',
  add column if not exists timezone text not null default 'Europe/Moscow';

alter table autocontent_settings
  rename to autocontent_settings_user_legacy;

create table autocontent_settings (
  threads_account_id bigint primary key,
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
  constraint autocontent_settings_account_owner_fk
    foreign key (threads_account_id, user_id)
    references threads_accounts (id, user_id)
    on delete cascade
);

insert into autocontent_settings (
  threads_account_id, user_id, active, posts_per_day,
  topics, slots, days, goal, timezone, created_at
)
select
  account.id,
  account.user_id,
  coalesce(legacy.active, false),
  greatest(0, least(5, coalesce(legacy.posts_per_day, 1))),
  coalesce(legacy.topics, ''),
  coalesce(legacy.slots, ''),
  case when legacy.days = 'weekdays' then 'weekdays' else 'all' end,
  coalesce(legacy.goal, ''),
  coalesce(legacy.timezone, 'Europe/Moscow'),
  coalesce(legacy.created_at, now())
from threads_accounts account
left join autocontent_settings_user_legacy legacy
  on legacy.user_id = account.user_id
where account.user_id is not null;

drop table autocontent_settings_user_legacy;

create index autocontent_settings_user_idx
  on autocontent_settings (user_id, threads_account_id);

alter table neuro_settings
  rename to neuro_settings_user_legacy;

create table neuro_settings (
  threads_account_id bigint primary key,
  user_id bigint not null references users(id) on delete cascade,
  active boolean not null default false,
  mode text not null default 'approve'
    check (mode in ('approve', 'auto')),
  daily_cap integer not null default 10,
  created_at timestamptz not null default now(),
  constraint neuro_settings_account_owner_fk
    foreign key (threads_account_id, user_id)
    references threads_accounts (id, user_id)
    on delete cascade
);

insert into neuro_settings (
  threads_account_id, user_id, active, mode, daily_cap, created_at
)
select
  account.id,
  account.user_id,
  coalesce(legacy.active, false),
  case when legacy.mode = 'auto' then 'auto' else 'approve' end,
  coalesce(legacy.daily_cap, 10),
  coalesce(legacy.created_at, now())
from threads_accounts account
left join neuro_settings_user_legacy legacy
  on legacy.user_id = account.user_id
where account.user_id is not null;

drop table neuro_settings_user_legacy;

alter table neuro_comments
  add column if not exists threads_account_id bigint
    references threads_accounts(id) on delete set null;

update neuro_comments comment
set threads_account_id = single_account.account_id
from (
  select user_id, min(id) as account_id
  from threads_accounts
  where user_id is not null
  group by user_id
  having count(*) = 1
) single_account
where single_account.user_id = comment.user_id
  and comment.threads_account_id is null;

alter table neuro_comments
  drop constraint if exists neuro_comments_user_id_target_post_id_key;

create unique index neuro_comments_account_target_unique
  on neuro_comments (threads_account_id, target_post_id)
  where threads_account_id is not null;

create table threads_data_deletion_requests (
  confirmation_code text primary key,
  threads_user_id_hash text not null,
  status text not null check (status in ('received', 'completed', 'failed')),
  requested_at timestamptz not null default now(),
  completed_at timestamptz
);

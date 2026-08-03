-- Account-scoped Radar and Neurocommenting v2.
-- Run this whole file inside one PostgreSQL transaction. Do not split it.

do $migration_guard$
begin
  if to_regclass('radar_neuro_v2_migration_011') is not null then
    raise exception 'migration 011 is already applied';
  end if;
  if to_regclass('radar_settings') is not null
     or to_regclass('radar_search_runs') is not null
     or to_regclass('radar_candidates') is not null
     or to_regclass('neuro_author_memory') is not null
     or to_regclass('ai_credit_events') is not null then
    raise exception
      'migration 011 found a partial schema; resolve it manually';
  end if;
  if to_regclass('neuro_settings') is null
     or to_regclass('neuro_comments') is null
     or to_regclass('threads_accounts') is null then
    raise exception 'migration 011 requires migration 010';
  end if;
end
$migration_guard$;

alter table neuro_settings
  alter column daily_cap set default 5,
  add column minimum_score integer not null default 75,
  add column excluded_authors text[] not null default '{}'::text[],
  add column minimum_interval_minutes integer not null default 30,
  add column auto_follow_up boolean not null default false;

alter table neuro_settings
  add constraint neuro_settings_minimum_score_check
    check (minimum_score between 0 and 100),
  add constraint neuro_settings_minimum_interval_check
    check (minimum_interval_minutes between 5 and 1440);

create table radar_settings (
  threads_account_id bigint primary key,
  user_id bigint not null references users(id) on delete cascade,
  niche text not null default '',
  keywords text[] not null default '{}'::text[],
  language text not null default 'ru',
  max_age_hours integer not null default 72,
  updated_at timestamptz not null default now(),
  constraint radar_settings_account_owner_fk
    foreign key (threads_account_id, user_id)
    references threads_accounts (id, user_id)
    on delete cascade,
  constraint radar_settings_language_check
    check (language in ('ru', 'en', 'any')),
  constraint radar_settings_max_age_check
    check (max_age_hours between 1 and 168)
);

insert into radar_settings (
  threads_account_id, user_id, niche, keywords
)
select
  account.id,
  account.user_id,
  coalesce(niche.niche, ''),
  coalesce(niche.keywords, '{}'::text[])
from threads_accounts account
left join user_niches niche on niche.user_id = account.user_id;

create table radar_search_runs (
  id bigserial primary key,
  user_id bigint not null references users(id) on delete cascade,
  threads_account_id bigint not null,
  keywords text[] not null default '{}'::text[],
  status text not null default 'running',
  results_seen integer not null default 0,
  candidates_saved integer not null default 0,
  filtered_count integer not null default 0,
  duplicate_count integer not null default 0,
  error_code text,
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  constraint radar_search_runs_account_owner_fk
    foreign key (threads_account_id, user_id)
    references threads_accounts (id, user_id)
    on delete cascade,
  constraint radar_search_runs_status_check
    check (status in ('running', 'success', 'permission_denied', 'failed')),
  constraint radar_search_runs_counts_check check (
    results_seen >= 0 and candidates_saved >= 0
    and filtered_count >= 0 and duplicate_count >= 0
  )
);

create index radar_search_runs_account_started_idx
  on radar_search_runs (threads_account_id, started_at desc);

create table radar_candidates (
  id bigserial primary key,
  user_id bigint not null references users(id) on delete cascade,
  threads_account_id bigint not null,
  threads_post_id text not null,
  author_key text not null,
  author_threads_id text,
  author_username text,
  post_text text not null,
  permalink text,
  published_at timestamptz,
  found_keywords text[] not null default '{}'::text[],
  discovered_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now(),
  duplicate_hits integer not null default 0,
  metrics_json jsonb not null default '{}'::jsonb,
  deterministic_score integer not null default 0,
  semantic_score integer,
  final_score integer,
  score_reason text,
  status text not null default 'discovered',
  filtered_reason text,
  semantic_claimed_at timestamptz,
  semantic_scored_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint radar_candidates_account_owner_fk
    foreign key (threads_account_id, user_id)
    references threads_accounts (id, user_id)
    on delete cascade,
  constraint radar_candidates_account_post_unique
    unique (threads_account_id, threads_post_id),
  constraint radar_candidates_identity_unique
    unique (id, threads_account_id, user_id),
  constraint radar_candidates_author_key_not_empty
    check (btrim(author_key) <> ''),
  constraint radar_candidates_post_not_empty
    check (btrim(threads_post_id) <> ''),
  constraint radar_candidates_scores_check check (
    deterministic_score between 0 and 100
    and (semantic_score is null or semantic_score between 0 and 100)
    and (final_score is null or final_score between 0 and 100)
  ),
  constraint radar_candidates_duplicate_hits_check
    check (duplicate_hits >= 0),
  constraint radar_candidates_metrics_object
    check (jsonb_typeof(metrics_json) = 'object'),
  constraint radar_candidates_status_check check (status in (
    'discovered', 'scoring', 'ready', 'generating', 'pending',
    'commented', 'rejected', 'filtered', 'score_failed', 'score_blocked'
  ))
);

create index radar_candidates_account_status_score_idx
  on radar_candidates (
    threads_account_id, status, final_score desc, deterministic_score desc
  );
create index radar_candidates_account_author_idx
  on radar_candidates (threads_account_id, author_key, discovered_at desc);

create table neuro_author_memory (
  id bigserial primary key,
  user_id bigint not null references users(id) on delete cascade,
  threads_account_id bigint not null,
  author_key text not null,
  author_threads_id text,
  author_username text,
  discovered_count integer not null default 0,
  comments_posted integer not null default 0,
  last_neuro_comment_id bigint references neuro_comments(id) on delete set null,
  last_strategy text,
  author_replied boolean not null default false,
  last_interaction_at timestamptz,
  cooldown_until timestamptz,
  updated_at timestamptz not null default now(),
  constraint neuro_author_memory_account_owner_fk
    foreign key (threads_account_id, user_id)
    references threads_accounts (id, user_id)
    on delete cascade,
  constraint neuro_author_memory_identity_unique
    unique (threads_account_id, author_key),
  constraint neuro_author_memory_counts_check
    check (discovered_count >= 0 and comments_posted >= 0),
  constraint neuro_author_memory_author_key_not_empty
    check (btrim(author_key) <> '')
);

create index neuro_author_memory_account_recent_idx
  on neuro_author_memory (threads_account_id, last_interaction_at desc);

create table ai_credit_events (
  operation_key text primary key,
  user_id bigint not null references users(id) on delete cascade,
  threads_account_id bigint not null,
  feature text not null,
  credits integer not null,
  created_at timestamptz not null default now(),
  constraint ai_credit_events_account_owner_fk
    foreign key (threads_account_id, user_id)
    references threads_accounts (id, user_id)
    on delete cascade,
  constraint ai_credit_events_key_not_empty
    check (btrim(operation_key) <> ''),
  constraint ai_credit_events_feature_not_empty
    check (btrim(feature) <> ''),
  constraint ai_credit_events_credits_positive check (credits > 0)
);

create index ai_credit_events_account_created_idx
  on ai_credit_events (threads_account_id, created_at desc);

alter table neuro_comments
  add column radar_candidate_id bigint,
  add column target_author_id text,
  add column author_key text,
  add column strategy text,
  add column score integer,
  add column score_reason text,
  add column generation_variant integer not null default 0,
  add column generation_claimed_at timestamptz,
  add column publish_claim_token uuid,
  add column publish_claimed_at timestamptz,
  add column publish_attempts integer not null default 0,
  add column provider_container_id text,
  add column published_threads_id text,
  add column publish_error_code text,
  add column reply_poll_status text not null default 'pending',
  add column reply_checked_at timestamptz,
  add column author_replied boolean not null default false,
  add column reply_threads_id text,
  add column reply_text text,
  add column replied_at timestamptz,
  add column follow_up_text text,
  add column follow_up_status text,
  add column follow_up_claimed_at timestamptz,
  add column follow_up_container_id text,
  add column follow_up_threads_id text,
  add column follow_up_error_code text,
  add column follow_up_count integer not null default 0;

update neuro_comments
set author_key = coalesce(nullif(lower(target_author), ''), 'legacy:' || id);

alter table neuro_comments
  alter column author_key set not null,
  add constraint neuro_comments_radar_candidate_owner_fk
    foreign key (radar_candidate_id, threads_account_id, user_id)
    references radar_candidates (id, threads_account_id, user_id)
    on delete no action,
  add constraint neuro_comments_strategy_check check (
    strategy is null or strategy in (
      'useful_addition', 'personal_observation', 'clarifying_question',
      'gentle_disagreement', 'short_insight', 'specific_support',
      'mini_story', 'professional_opinion'
    )
  ),
  add constraint neuro_comments_score_check
    check (score is null or score between 0 and 100),
  add constraint neuro_comments_v2_counts_check
    check (generation_variant >= 0 and publish_attempts >= 0
           and follow_up_count between 0 and 1),
  add constraint neuro_comments_reply_poll_status_check
    check (reply_poll_status in ('pending', 'checked', 'replied',
                                 'permission_denied')),
  add constraint neuro_comments_v2_status_check check (status in (
    'generating', 'pending', 'publishing', 'posted', 'rejected',
    'skipped', 'failed', 'unknown', 'permission_denied'
  ));

create index neuro_comments_account_status_created_idx
  on neuro_comments (threads_account_id, status, created_at desc);
create index neuro_comments_reply_poll_idx
  on neuro_comments (threads_account_id, reply_checked_at)
  where status = 'posted' and author_replied = false;

create table radar_neuro_v2_migration_011 (
  singleton boolean primary key default true check (singleton),
  applied_at timestamptz not null default now(),
  radar_settings_fingerprint text not null,
  neuro_settings_fingerprint text not null
);

insert into radar_neuro_v2_migration_011 (
  singleton, radar_settings_fingerprint, neuro_settings_fingerprint
)
select
  true,
  md5(coalesce(
    (select jsonb_agg(to_jsonb(setting) order by threads_account_id)::text
     from radar_settings setting),
    '[]'
  )),
  md5(coalesce(
    (select jsonb_agg(jsonb_build_object(
       'threads_account_id', threads_account_id,
       'minimum_score', minimum_score,
       'excluded_authors', excluded_authors,
       'minimum_interval_minutes', minimum_interval_minutes,
       'auto_follow_up', auto_follow_up
     ) order by threads_account_id)::text
     from neuro_settings),
    '[]'
  ));

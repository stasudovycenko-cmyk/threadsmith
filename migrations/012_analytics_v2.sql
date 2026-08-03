-- Analytics V2: provider-neutral, account-scoped performance intelligence.
-- Run this whole file inside one PostgreSQL transaction. Do not split it.

do $migration_guard$
begin
  if to_regclass('analytics_v2_migration_012') is not null then
    raise exception 'migration 012 is already applied';
  end if;
  if to_regclass('analytics_snapshots') is not null
     or to_regclass('analytics_post_summary') is not null
     or to_regclass('analytics_account_summary') is not null
     or to_regclass('analytics_aggregates') is not null
     or to_regclass('analytics_scheduled_post_owner_unique') is not null then
    raise exception
      'migration 012 found a partial schema; resolve it manually';
  end if;
  if to_regclass('radar_neuro_v2_migration_011') is null
     or to_regclass('scheduled_posts') is null
     or to_regclass('insights_snapshots') is null
     or to_regclass('brains') is null then
    raise exception 'migration 012 requires migrations 005 through 011';
  end if;
end
$migration_guard$;

-- This identity lets analytics rows prove that a scheduled post belongs to
-- the same user/account, not merely that the post id exists.
create unique index analytics_scheduled_post_owner_unique
  on scheduled_posts (id, user_id, threads_account_id);

create table analytics_snapshots (
  id bigserial primary key,
  user_id bigint not null references users(id) on delete cascade,
  threads_account_id bigint not null,
  scheduled_post_id bigint,
  provider text not null,
  threads_post_id text not null,
  snapshot_at timestamptz not null,
  snapshot_bucket timestamptz not null,
  views bigint,
  likes bigint,
  replies bigint,
  quotes bigint,
  reposts bigint,
  shares bigint,
  profile_visits bigint,
  followers bigint,
  engagement_rate numeric,
  performance_score numeric,
  virality_score numeric,
  brain_score numeric,
  raw_metrics jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  constraint analytics_snapshots_account_owner_fk
    foreign key (threads_account_id, user_id)
    references threads_accounts (id, user_id)
    on delete cascade,
  constraint analytics_snapshots_scheduled_owner_fk
    foreign key (scheduled_post_id, user_id, threads_account_id)
    references scheduled_posts (id, user_id, threads_account_id)
    on delete cascade,
  constraint analytics_snapshots_identity_unique
    unique (threads_account_id, provider, threads_post_id, snapshot_bucket),
  constraint analytics_snapshots_provider_not_empty
    check (btrim(provider) <> ''),
  constraint analytics_snapshots_post_not_empty
    check (btrim(threads_post_id) <> ''),
  constraint analytics_snapshots_counts_nonnegative check (
    (views is null or views >= 0)
    and (likes is null or likes >= 0)
    and (replies is null or replies >= 0)
    and (quotes is null or quotes >= 0)
    and (reposts is null or reposts >= 0)
    and (shares is null or shares >= 0)
    and (profile_visits is null or profile_visits >= 0)
    and (followers is null or followers >= 0)
    and (engagement_rate is null or engagement_rate >= 0)
  ),
  constraint analytics_snapshots_scores_range check (
    (performance_score is null or performance_score between 0 and 100)
    and (virality_score is null or virality_score between 0 and 100)
    and (brain_score is null or brain_score between 0 and 100)
  ),
  constraint analytics_snapshots_raw_object
    check (jsonb_typeof(raw_metrics) = 'object')
);

create index analytics_snapshots_account_post_time_idx
  on analytics_snapshots (
    threads_account_id, threads_post_id, snapshot_at desc
  );
create index analytics_snapshots_account_time_idx
  on analytics_snapshots (threads_account_id, snapshot_at desc);

create table analytics_post_summary (
  id bigserial primary key,
  user_id bigint not null references users(id) on delete cascade,
  threads_account_id bigint not null,
  scheduled_post_id bigint,
  provider text not null,
  threads_post_id text not null,
  published_at timestamptz not null,
  first_seen timestamptz not null,
  last_updated timestamptz not null,
  peak_views bigint,
  current_views bigint,
  likes bigint,
  replies bigint,
  quotes bigint,
  reposts bigint,
  shares bigint,
  profile_visits bigint,
  followers bigint,
  engagement_rate numeric,
  performance_score numeric,
  virality_score numeric,
  brain_score numeric,
  performance_percentile numeric,
  hook_type text,
  cta_type text,
  topic text,
  publish_hour smallint,
  weekday smallint,
  constraint analytics_post_summary_account_owner_fk
    foreign key (threads_account_id, user_id)
    references threads_accounts (id, user_id)
    on delete cascade,
  constraint analytics_post_summary_scheduled_owner_fk
    foreign key (scheduled_post_id, user_id, threads_account_id)
    references scheduled_posts (id, user_id, threads_account_id)
    on delete cascade,
  constraint analytics_post_summary_identity_unique
    unique (threads_account_id, provider, threads_post_id),
  constraint analytics_post_summary_counts_nonnegative check (
    (peak_views is null or peak_views >= 0)
    and (current_views is null or current_views >= 0)
    and (likes is null or likes >= 0)
    and (replies is null or replies >= 0)
    and (quotes is null or quotes >= 0)
    and (reposts is null or reposts >= 0)
    and (shares is null or shares >= 0)
    and (profile_visits is null or profile_visits >= 0)
    and (followers is null or followers >= 0)
    and (engagement_rate is null or engagement_rate >= 0)
  ),
  constraint analytics_post_summary_scores_range check (
    (performance_score is null or performance_score between 0 and 100)
    and (virality_score is null or virality_score between 0 and 100)
    and (brain_score is null or brain_score between 0 and 100)
    and (performance_percentile is null
         or performance_percentile between 0 and 100)
  ),
  constraint analytics_post_summary_time_range check (
    (publish_hour is null or publish_hour between 0 and 23)
    and (weekday is null or weekday between 0 and 6)
  )
);

create index analytics_post_summary_account_views_idx
  on analytics_post_summary (
    threads_account_id, current_views desc nulls last
  );
create index analytics_post_summary_account_score_idx
  on analytics_post_summary (
    threads_account_id, brain_score desc nulls last
  );
create unique index analytics_post_summary_scheduled_provider_unique
  on analytics_post_summary (scheduled_post_id, provider)
  where scheduled_post_id is not null;

create table analytics_aggregates (
  id bigserial primary key,
  user_id bigint not null references users(id) on delete cascade,
  threads_account_id bigint not null,
  dimension text not null,
  dimension_key text not null,
  posts_count integer not null,
  views_total bigint,
  avg_views numeric,
  avg_er numeric,
  avg_replies numeric,
  avg_brain_score numeric,
  avg_virality_score numeric,
  avg_ctr numeric,
  updated_at timestamptz not null default now(),
  constraint analytics_aggregates_account_owner_fk
    foreign key (threads_account_id, user_id)
    references threads_accounts (id, user_id)
    on delete cascade,
  constraint analytics_aggregates_identity_unique
    unique (threads_account_id, dimension, dimension_key),
  constraint analytics_aggregates_dimension_check check (
    dimension in (
      'topic', 'hook_type', 'cta_type', 'publish_hour', 'weekday'
    )
  ),
  constraint analytics_aggregates_key_not_empty
    check (btrim(dimension_key) <> ''),
  constraint analytics_aggregates_values_check check (
    posts_count > 0
    and (views_total is null or views_total >= 0)
    and (avg_views is null or avg_views >= 0)
    and (avg_er is null or avg_er >= 0)
    and (avg_replies is null or avg_replies >= 0)
    and (avg_brain_score is null or avg_brain_score between 0 and 100)
    and (avg_virality_score is null
         or avg_virality_score between 0 and 100)
    and (avg_ctr is null or avg_ctr >= 0)
  )
);

create index analytics_aggregates_account_dimension_score_idx
  on analytics_aggregates (
    threads_account_id, dimension, avg_brain_score desc nulls last
  );

create table analytics_account_summary (
  threads_account_id bigint primary key,
  user_id bigint not null references users(id) on delete cascade,
  posts_total integer not null default 0,
  views_total bigint,
  likes_total bigint,
  comments_total bigint,
  shares_total bigint,
  avg_er numeric,
  avg_views numeric,
  best_post_id text,
  worst_post_id text,
  best_hour smallint,
  best_weekday smallint,
  best_topic text,
  best_hook text,
  best_cta text,
  brain_score numeric,
  metric_coverage jsonb not null default '{}'::jsonb,
  updated_at timestamptz not null default now(),
  constraint analytics_account_summary_account_owner_fk
    foreign key (threads_account_id, user_id)
    references threads_accounts (id, user_id)
    on delete cascade,
  constraint analytics_account_summary_values_check check (
    posts_total >= 0
    and (views_total is null or views_total >= 0)
    and (likes_total is null or likes_total >= 0)
    and (comments_total is null or comments_total >= 0)
    and (shares_total is null or shares_total >= 0)
    and (avg_er is null or avg_er >= 0)
    and (avg_views is null or avg_views >= 0)
    and (brain_score is null or brain_score between 0 and 100)
    and (best_hour is null or best_hour between 0 and 23)
    and (best_weekday is null or best_weekday between 0 and 6)
  ),
  constraint analytics_account_summary_coverage_object
    check (jsonb_typeof(metric_coverage) = 'object')
);

-- Preserve existing own-post history as raw snapshots. Derived scores are
-- intentionally left NULL and will be calculated by the first collector run.
insert into analytics_snapshots (
  user_id, threads_account_id, scheduled_post_id,
  provider, threads_post_id, snapshot_at,
  snapshot_bucket,
  views, likes, replies, quotes, reposts, shares,
  engagement_rate, raw_metrics
)
select
  post.user_id,
  post.threads_account_id,
  post.id,
  'threads',
  post.threads_post_id,
  snapshot.snapshot_date::timestamptz,
  snapshot.snapshot_date::timestamptz,
  case when coalesce(snapshot.metrics_json->>'views', '') ~ '^[0-9]+$'
       then (snapshot.metrics_json->>'views')::bigint end,
  case when coalesce(snapshot.metrics_json->>'likes', '') ~ '^[0-9]+$'
       then (snapshot.metrics_json->>'likes')::bigint end,
  case when coalesce(snapshot.metrics_json->>'replies', '') ~ '^[0-9]+$'
       then (snapshot.metrics_json->>'replies')::bigint end,
  case when coalesce(snapshot.metrics_json->>'quotes', '') ~ '^[0-9]+$'
       then (snapshot.metrics_json->>'quotes')::bigint end,
  case when coalesce(snapshot.metrics_json->>'reposts', '') ~ '^[0-9]+$'
       then (snapshot.metrics_json->>'reposts')::bigint end,
  case when coalesce(snapshot.metrics_json->>'shares', '') ~ '^[0-9]+$'
       then (snapshot.metrics_json->>'shares')::bigint end,
  case
    when coalesce(snapshot.metrics_json->>'views', '') ~ '^[1-9][0-9]*$'
     and coalesce(snapshot.metrics_json->>'likes', '') ~ '^[0-9]+$'
     and coalesce(snapshot.metrics_json->>'replies', '') ~ '^[0-9]+$'
     and coalesce(snapshot.metrics_json->>'reposts', '') ~ '^[0-9]+$'
     and coalesce(snapshot.metrics_json->>'quotes', '') ~ '^[0-9]+$'
    then (
      (snapshot.metrics_json->>'likes')::numeric
      + (snapshot.metrics_json->>'replies')::numeric
      + (snapshot.metrics_json->>'reposts')::numeric
      + (snapshot.metrics_json->>'quotes')::numeric
    ) / (snapshot.metrics_json->>'views')::numeric
  end,
  snapshot.metrics_json
from insights_snapshots snapshot
join scheduled_posts post
  on post.threads_post_id = snapshot.threads_post_id
where post.user_id is not null
  and post.threads_account_id is not null
  and post.threads_post_id is not null
on conflict (
  threads_account_id, provider, threads_post_id, snapshot_bucket
) do nothing;

create table analytics_v2_migration_012 (
  singleton boolean primary key default true check (singleton),
  applied_at timestamptz not null default now(),
  backfilled_snapshots bigint not null default 0
);

insert into analytics_v2_migration_012 (
  singleton, backfilled_snapshots
)
select true, count(*) from analytics_snapshots;

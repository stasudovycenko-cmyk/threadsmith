-- Conservative rollback for migration 011.
-- It refuses to discard Radar/Neuro v2 activity or changed settings.

do $rollback_guard$
declare
  expected_radar text;
  expected_neuro text;
  current_radar text;
  current_neuro text;
begin
  if to_regclass('radar_neuro_v2_migration_011') is null then
    raise exception 'rollback 011 blocked: migration marker is missing';
  end if;
  select radar_settings_fingerprint, neuro_settings_fingerprint
  into strict expected_radar, expected_neuro
  from radar_neuro_v2_migration_011 where singleton;

  select md5(coalesce(
    jsonb_agg(to_jsonb(setting) order by threads_account_id)::text,
    '[]'
  )) into current_radar from radar_settings setting;
  select md5(coalesce(
    jsonb_agg(jsonb_build_object(
      'threads_account_id', threads_account_id,
      'minimum_score', minimum_score,
      'excluded_authors', excluded_authors,
      'minimum_interval_minutes', minimum_interval_minutes,
      'auto_follow_up', auto_follow_up
    ) order by threads_account_id)::text,
    '[]'
  )) into current_neuro from neuro_settings;

  if current_radar is distinct from expected_radar
     or current_neuro is distinct from expected_neuro then
    raise exception 'rollback 011 blocked: v2 settings changed';
  end if;
  if exists (select 1 from radar_search_runs)
     or exists (select 1 from radar_candidates)
     or exists (select 1 from neuro_author_memory)
     or exists (select 1 from ai_credit_events) then
    raise exception 'rollback 011 blocked: v2 activity data exists';
  end if;
  if exists (
    select 1 from neuro_comments
    where radar_candidate_id is not null
       or strategy is not null
       or publish_claim_token is not null
       or published_threads_id is not null
       or author_replied
       or follow_up_text is not null
       or follow_up_status is not null
       or follow_up_count > 0
  ) then
    raise exception 'rollback 011 blocked: v2 comments exist';
  end if;
end
$rollback_guard$;

alter table neuro_comments
  drop constraint if exists neuro_comments_radar_candidate_owner_fk,
  drop constraint if exists neuro_comments_strategy_check,
  drop constraint if exists neuro_comments_score_check,
  drop constraint if exists neuro_comments_v2_counts_check,
  drop constraint if exists neuro_comments_reply_poll_status_check,
  drop constraint if exists neuro_comments_v2_status_check,
  drop column if exists radar_candidate_id,
  drop column if exists target_author_id,
  drop column if exists author_key,
  drop column if exists strategy,
  drop column if exists score,
  drop column if exists score_reason,
  drop column if exists generation_variant,
  drop column if exists generation_claimed_at,
  drop column if exists publish_claim_token,
  drop column if exists publish_claimed_at,
  drop column if exists publish_attempts,
  drop column if exists provider_container_id,
  drop column if exists published_threads_id,
  drop column if exists publish_error_code,
  drop column if exists reply_poll_status,
  drop column if exists reply_checked_at,
  drop column if exists author_replied,
  drop column if exists reply_threads_id,
  drop column if exists reply_text,
  drop column if exists replied_at,
  drop column if exists follow_up_text,
  drop column if exists follow_up_status,
  drop column if exists follow_up_claimed_at,
  drop column if exists follow_up_container_id,
  drop column if exists follow_up_threads_id,
  drop column if exists follow_up_error_code,
  drop column if exists follow_up_count;

drop table if exists ai_credit_events;
drop table if exists neuro_author_memory;
drop table if exists radar_candidates;
drop table if exists radar_search_runs;
drop table if exists radar_settings;

alter table neuro_settings
  drop constraint if exists neuro_settings_minimum_score_check,
  drop constraint if exists neuro_settings_minimum_interval_check,
  alter column daily_cap set default 10,
  drop column if exists minimum_score,
  drop column if exists excluded_authors,
  drop column if exists minimum_interval_minutes,
  drop column if exists auto_follow_up;

drop table if exists radar_neuro_v2_migration_011;

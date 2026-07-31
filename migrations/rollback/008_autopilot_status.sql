-- Roll back Autopilot Status. This removes run history.

drop table if exists autopost_runs;

alter table scheduled_posts
  drop column if exists publish_started_at;

alter table autocontent_settings
  drop column if exists timezone;

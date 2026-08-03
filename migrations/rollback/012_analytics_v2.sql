-- Analytics V2 stores only rebuildable derivative data.
-- This rollback does not touch publication or legacy insight tables.

do $rollback_guard$
begin
  if to_regclass('analytics_v2_migration_012') is null then
    raise exception 'rollback 012 blocked: migration marker is missing';
  end if;
end
$rollback_guard$;

drop table if exists analytics_account_summary;
drop table if exists analytics_aggregates;
drop table if exists analytics_post_summary;
drop table if exists analytics_snapshots;
drop index if exists analytics_scheduled_post_owner_unique;
drop table if exists analytics_v2_migration_012;

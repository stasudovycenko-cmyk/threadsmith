-- Decision history is derivative and can be rebuilt after rollback.

do $rollback_guard$
begin
  if to_regclass('autopilot_intelligence_migration_014') is null then
    raise exception 'rollback 014 blocked: migration marker is missing';
  end if;
end
$rollback_guard$;

drop table if exists decision_runs;
drop table if exists autopilot_intelligence_migration_014;

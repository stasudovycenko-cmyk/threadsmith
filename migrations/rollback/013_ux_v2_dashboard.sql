-- Remove only UX V2 persistence. Publication and account data are untouched.

do $rollback_guard$
begin
  if to_regclass('ux_v2_migration_013') is null then
    raise exception 'rollback 013 blocked: migration marker is missing';
  end if;
end
$rollback_guard$;

drop table if exists ux_onboarding;

alter table user_preferences
  drop constraint if exists user_preferences_interface_mode_check,
  drop column if exists interface_mode;

drop table if exists ux_v2_migration_013;

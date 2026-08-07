-- Durable account-scoped state for the temporary Autocontent cost circuit.

alter table autocontent_settings
  add column if not exists cost_guard_until timestamptz,
  add column if not exists cost_guard_reason text,
  add column if not exists cost_guard_observed_at timestamptz;

do $constraint$
begin
  if not exists (
    select 1 from pg_constraint
    where conrelid = 'autocontent_settings'::regclass
      and conname = 'autocontent_settings_cost_guard_reason_check'
  ) then
    alter table autocontent_settings
      add constraint autocontent_settings_cost_guard_reason_check
      check (
        cost_guard_reason is null
        or cost_guard_reason in ('REPAIR_RATE_HIGH')
      );
  end if;
end
$constraint$;

create index if not exists ai_usage_events_account_feature_created_idx
  on ai_usage_events (threads_account_id, feature, created_at desc);

drop index if exists ai_usage_events_account_feature_created_idx;

alter table autocontent_settings
  drop constraint if exists autocontent_settings_cost_guard_reason_check,
  drop column if exists cost_guard_observed_at,
  drop column if exists cost_guard_reason,
  drop column if exists cost_guard_until;

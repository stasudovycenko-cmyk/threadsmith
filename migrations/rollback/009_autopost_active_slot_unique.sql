-- Safe before a slot has multiple terminal attempts. Preserve history on failure.

drop index if exists autopost_runs_pending_slot_unique;

alter table autopost_runs
  add constraint autopost_runs_slot_unique unique (
    threads_account_id,
    scheduled_at
  );

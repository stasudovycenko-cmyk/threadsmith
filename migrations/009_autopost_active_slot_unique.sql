-- Allow a failed/skipped slot to be retried while keeping run history.

alter table autopost_runs
  drop constraint if exists autopost_runs_slot_unique;

create unique index if not exists autopost_runs_pending_slot_unique
  on autopost_runs (threads_account_id, scheduled_at)
  where status = 'pending';

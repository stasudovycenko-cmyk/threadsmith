-- Account-scoped publication notifications for Telegram UX V3.

alter table autocontent_settings
  add column if not exists publish_notifications_enabled boolean
    not null default true;

alter table scheduled_posts
  add column if not exists publication_notification_claimed_at timestamptz;

-- Do not emit recovery notifications for incidents that predate UX V3.
update scheduled_posts
set publication_notification_claimed_at = now()
where status = 'failed'
  and error = 'UNKNOWN_ERROR: interrupted worker'
  and publication_notification_claimed_at is null;

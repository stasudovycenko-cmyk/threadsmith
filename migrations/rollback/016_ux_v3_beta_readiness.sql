-- Roll back only UX V3 notification state.

alter table scheduled_posts
  drop column if exists publication_notification_claimed_at;

alter table autocontent_settings
  drop column if exists publish_notifications_enabled;

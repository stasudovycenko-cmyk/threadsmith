-- Roll back Content Engine 2.0 post metadata only.

alter table scheduled_posts
  drop column if exists content_metadata;

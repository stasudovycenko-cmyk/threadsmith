-- Content Engine 2.0: preserve generation features on published posts.

alter table scheduled_posts
  add column if not exists content_metadata jsonb;

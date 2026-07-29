-- Roll back Social Brain v1 tables and its supporting indexes only.

drop table if exists brain_events;
drop table if exists brain_patterns;
drop table if exists brains;
drop index if exists idx_social_brain_posts_user_run_at;
drop index if exists idx_social_brain_account_owner_unique;

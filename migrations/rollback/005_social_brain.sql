-- Roll back Social Brain v1 tables and its supporting indexes only.

drop index if exists idx_social_brain_posts_user_run_at;
drop index if exists idx_social_brain_generations_user_created;
drop index if exists idx_social_brain_accounts_user_created;
drop table if exists decision_log;
drop table if exists user_strategy_state;
drop table if exists social_facts;

-- Новая миграция: настройки авто-контента. Прогнать в Supabase SQL Editor.
create table if not exists autocontent_settings (
  user_id bigint primary key references users(id),
  active bool default false,
  posts_per_day int default 1,
  created_at timestamptz default now()
);

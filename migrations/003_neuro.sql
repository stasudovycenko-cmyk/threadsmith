-- Модуль 4: нейрокомментинг
create table neuro_settings (
  user_id bigint primary key references users(id),
  active bool default false,
  mode text default 'approve',        -- approve / auto
  daily_cap int default 10,
  created_at timestamptz default now()
);

create table neuro_comments (
  id bigserial primary key,
  user_id bigint references users(id),
  target_post_id text not null,
  target_author text,
  target_text text,
  comment_text text not null,
  status text default 'pending',      -- pending/posted/rejected/failed
  created_at timestamptz default now(),
  posted_at timestamptz,
  unique (user_id, target_post_id)    -- один коммент на пост
);
create index on neuro_comments (user_id, status);
create index on neuro_comments (user_id, created_at);

-- Модуль 1: ниши юзеров. Одна ниша на юзера в MVP.
create table user_niches (
  user_id bigint primary key references users(id),
  niche text not null,
  keywords text[] not null default '{}',
  created_at timestamptz default now()
);

create table if not exists public.notifications (
  id uuid primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  type text not null check (type in ('new_request', 'provider_response', 'provider_selected', 'request_resolved')),
  title text not null,
  message text not null,
  query_id uuid null references public.queries(id) on delete cascade,
  response_id uuid null references public.responses(id) on delete cascade,
  is_read boolean not null default false,
  created_at timestamptz not null default now()
);

create index if not exists notifications_user_idx
  on public.notifications(user_id);

create index if not exists notifications_user_read_idx
  on public.notifications(user_id, is_read);

create index if not exists notifications_created_at_idx
  on public.notifications(created_at desc);

create index if not exists notifications_query_idx
  on public.notifications(query_id);

alter table public.notifications enable row level security;

drop policy if exists "Users can read their own notifications" on public.notifications;
create policy "Users can read their own notifications"
  on public.notifications for select
  using (auth.uid() = user_id);

drop policy if exists "Users can update their own notifications" on public.notifications;
create policy "Users can update their own notifications"
  on public.notifications for update
  using (auth.uid() = user_id);

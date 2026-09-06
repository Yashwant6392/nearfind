alter table public.notifications
drop constraint if exists notifications_type_check;

alter table public.notifications
add constraint notifications_type_check
check (type in ('new_request', 'provider_response', 'provider_selected', 'request_resolved', 'chat_message'));

create table if not exists public.conversations (
  id uuid primary key,
  query_id uuid not null references public.queries(id) on delete cascade,
  response_id uuid not null unique references public.responses(id) on delete cascade,
  seeker_id uuid not null references auth.users(id) on delete cascade,
  provider_id uuid not null references auth.users(id) on delete cascade,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.messages (
  id uuid primary key,
  conversation_id uuid not null references public.conversations(id) on delete cascade,
  sender_id uuid not null references auth.users(id) on delete cascade,
  message text not null check (length(trim(message)) > 0 and length(message) <= 2000),
  is_read boolean not null default false,
  created_at timestamptz not null default now()
);

create index if not exists conversations_query_idx
  on public.conversations(query_id);

create index if not exists conversations_seeker_idx
  on public.conversations(seeker_id);

create index if not exists conversations_provider_idx
  on public.conversations(provider_id);

create index if not exists messages_conversation_created_idx
  on public.messages(conversation_id, created_at);

create index if not exists messages_sender_idx
  on public.messages(sender_id);

create index if not exists messages_unread_idx
  on public.messages(conversation_id, is_read);

alter table public.conversations enable row level security;
alter table public.messages enable row level security;

drop policy if exists "Conversation members can read conversations" on public.conversations;
create policy "Conversation members can read conversations"
  on public.conversations for select
  using (auth.uid() = seeker_id or auth.uid() = provider_id);

drop policy if exists "Conversation members can read messages" on public.messages;
create policy "Conversation members can read messages"
  on public.messages for select
  using (exists (
    select 1 from public.conversations c
    where c.id = conversation_id
      and (auth.uid() = c.seeker_id or auth.uid() = c.provider_id)
  ));

drop policy if exists "Conversation members can send messages" on public.messages;
create policy "Conversation members can send messages"
  on public.messages for insert
  with check (
    auth.uid() = sender_id
    and exists (
      select 1 from public.conversations c
      where c.id = conversation_id
        and (auth.uid() = c.seeker_id or auth.uid() = c.provider_id)
    )
  );

drop policy if exists "Conversation members can update message reads" on public.messages;
create policy "Conversation members can update message reads"
  on public.messages for update
  using (exists (
    select 1 from public.conversations c
    where c.id = conversation_id
      and (auth.uid() = c.seeker_id or auth.uid() = c.provider_id)
  ))
  with check (exists (
    select 1 from public.conversations c
    where c.id = conversation_id
      and (auth.uid() = c.seeker_id or auth.uid() = c.provider_id)
  ));

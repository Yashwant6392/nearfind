create extension if not exists "pgcrypto";

create table if not exists public.users (
  id uuid primary key references auth.users(id) on delete cascade,
  email text not null,
  role text not null check (role in ('seeker', 'provider')),
  provider_type text null check (provider_type in ('shop', 'individual')),
  name text not null,
  business_name text null,
  phone text not null,
  address text not null,
  lat float8 null,
  lng float8 null,
  is_active boolean not null default true,
  last_location_update timestamptz null,
  created_at timestamptz not null default now()
);

create table if not exists public.queries (
  id uuid primary key default gen_random_uuid(),
  seeker_id uuid not null references public.users(id) on delete cascade,
  item_name text not null,
  category text not null,
  description text not null,
  location text not null,
  lat float8 not null,
  lng float8 not null,
  radius_km float8 not null default 5,
  urgency text not null check (urgency in ('normal', 'urgent', 'very_urgent')),
  status text not null check (status in ('open', 'matched', 'resolved', 'closed', 'expired')),
  image_url text null,
  expires_at timestamptz null,
  created_at timestamptz not null default now()
);

create table if not exists public.responses (
  id uuid primary key default gen_random_uuid(),
  query_id uuid not null references public.queries(id) on delete cascade,
  provider_id uuid not null references public.users(id) on delete cascade,
  message text not null,
  price text not null,
  status text not null check (status in ('available', 'selected', 'rejected', 'sold')),
  image_url text null,
  created_at timestamptz not null default now()
);

create unique index if not exists responses_one_active_per_provider_query
  on public.responses(query_id, provider_id)
  where status in ('available', 'selected');

create index if not exists queries_status_expires_idx
  on public.queries(status, expires_at);

create index if not exists queries_location_idx
  on public.queries(lat, lng);

create index if not exists responses_query_idx
  on public.responses(query_id);

alter table public.users enable row level security;
alter table public.queries enable row level security;
alter table public.responses enable row level security;

drop policy if exists "Users can read their own profile" on public.users;
create policy "Users can read their own profile"
  on public.users for select
  using (auth.uid() = id);

drop policy if exists "Users can update their own profile" on public.users;
create policy "Users can update their own profile"
  on public.users for update
  using (auth.uid() = id);

drop policy if exists "Seekers can read own queries" on public.queries;
create policy "Seekers can read own queries"
  on public.queries for select
  using (auth.uid() = seeker_id);

drop policy if exists "Seekers can insert own queries" on public.queries;
create policy "Seekers can insert own queries"
  on public.queries for insert
  with check (auth.uid() = seeker_id);

drop policy if exists "Seekers can update own queries" on public.queries;
create policy "Seekers can update own queries"
  on public.queries for update
  using (auth.uid() = seeker_id);

drop policy if exists "Providers can read open queries" on public.queries;
create policy "Providers can read open queries"
  on public.queries for select
  using (status = 'open');

drop policy if exists "Seekers can read responses to own queries" on public.responses;
create policy "Seekers can read responses to own queries"
  on public.responses for select
  using (exists (
    select 1 from public.queries q
    where q.id = query_id and q.seeker_id = auth.uid()
  ));

drop policy if exists "Providers can read own responses" on public.responses;
create policy "Providers can read own responses"
  on public.responses for select
  using (auth.uid() = provider_id);

drop policy if exists "Providers can insert own responses" on public.responses;
create policy "Providers can insert own responses"
  on public.responses for insert
  with check (auth.uid() = provider_id);

drop policy if exists "Providers can update own available responses" on public.responses;
create policy "Providers can update own available responses"
  on public.responses for update
  using (auth.uid() = provider_id);

create or replace function public.select_provider_for_query(
  p_query_id uuid,
  p_response_id uuid,
  p_seeker_id uuid
)
returns jsonb
language plpgsql
set search_path = public
as $$
declare
  selected_query public.queries%rowtype;
  selected_response public.responses%rowtype;
begin
  select *
  into selected_query
  from public.queries
  where id = p_query_id
  for update;

  if not found or selected_query.seeker_id <> p_seeker_id then
    raise exception 'Query is not selectable';
  end if;

  if selected_query.status <> 'open' then
    raise exception 'Query is not selectable';
  end if;

  if selected_query.expires_at is not null and selected_query.expires_at <= now() then
    update public.queries
    set status = 'expired'
    where id = p_query_id
      and status = 'open';

    return jsonb_build_object(
      'query_id', p_query_id,
      'status', 'expired',
      'error', 'Query has expired'
    );
  end if;

  select *
  into selected_response
  from public.responses
  where id = p_response_id
    and query_id = p_query_id
    and status = 'available'
  for update;

  if not found then
    raise exception 'Provider response is not available';
  end if;

  update public.responses
  set status = 'rejected'
  where query_id = p_query_id
    and status = 'available'
    and id <> p_response_id;

  update public.responses
  set status = 'selected'
  where id = p_response_id
    and query_id = p_query_id
    and status = 'available';

  update public.queries
  set status = 'matched'
  where id = p_query_id
    and seeker_id = p_seeker_id
    and status = 'open';

  return jsonb_build_object(
    'query_id', p_query_id,
    'response_id', p_response_id,
    'status', 'matched'
  );
end;
$$;
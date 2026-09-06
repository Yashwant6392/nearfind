create table if not exists public.provider_locations (
  provider_id uuid primary key references auth.users(id) on delete cascade,
  lat double precision not null check (lat between -90 and 90),
  lng double precision not null check (lng between -180 and 180),
  accuracy_m double precision null check (accuracy_m is null or (accuracy_m >= 0 and accuracy_m <= 100000)),
  updated_at timestamptz not null default now(),
  sharing_enabled boolean not null default true
);

create index if not exists provider_locations_updated_idx
  on public.provider_locations(updated_at desc);

alter table public.provider_locations enable row level security;

drop policy if exists "Providers can manage own location" on public.provider_locations;
create policy "Providers can manage own location"
  on public.provider_locations for all
  using (auth.uid() = provider_id)
  with check (auth.uid() = provider_id);

drop policy if exists "Seekers can read selected provider location" on public.provider_locations;
create policy "Seekers can read selected provider location"
  on public.provider_locations for select
  using (sharing_enabled and exists (
    select 1
    from public.queries q
    join public.responses r on r.query_id = q.id
    where q.seeker_id = auth.uid()
      and q.status = 'matched'
      and r.provider_id = provider_id
      and r.status = 'selected'
  ));

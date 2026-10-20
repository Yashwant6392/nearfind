create table if not exists public.driver_availability (
  id uuid primary key default gen_random_uuid(),
  driver_id uuid not null references public.driver_profiles(provider_id) on delete cascade,
  starts_at timestamptz not null,
  ends_at timestamptz not null,
  is_active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check (ends_at > starts_at)
);

create index if not exists driver_availability_driver_time_idx
  on public.driver_availability(driver_id, starts_at, ends_at);

create index if not exists driver_availability_active_idx
  on public.driver_availability(driver_id, is_active, starts_at);

create table if not exists public.driver_locations (
  driver_id uuid primary key references public.driver_profiles(provider_id) on delete cascade,
  lat double precision not null check (lat between -90 and 90),
  lng double precision not null check (lng between -180 and 180),
  accuracy_m double precision null check (accuracy_m is null or (accuracy_m >= 0 and accuracy_m <= 100000)),
  updated_at timestamptz not null default now(),
  sharing_enabled boolean not null default false
);

create index if not exists driver_locations_updated_idx
  on public.driver_locations(updated_at desc);

alter table public.driver_availability enable row level security;
alter table public.driver_locations enable row level security;

drop policy if exists "Drivers can manage own availability" on public.driver_availability;
create policy "Drivers can manage own availability"
  on public.driver_availability for all
  using (auth.uid() = driver_id)
  with check (auth.uid() = driver_id);

drop policy if exists "Driver can read own location" on public.driver_locations;
create policy "Driver can read own location"
  on public.driver_locations for select
  using (auth.uid() = driver_id);

drop policy if exists "Driver can manage own location" on public.driver_locations;
create policy "Driver can manage own location"
  on public.driver_locations for all
  using (auth.uid() = driver_id)
  with check (auth.uid() = driver_id);

-- This table is intentionally separate from provider_locations because driver-specific
-- approval, consent, freshness checks, and privacy rules differ from ordinary provider
-- location publishing.

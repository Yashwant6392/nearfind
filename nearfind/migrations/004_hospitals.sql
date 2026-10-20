create table if not exists public.hospitals (
  id uuid primary key default gen_random_uuid(),
  manager_id uuid not null unique references public.users(id) on delete restrict,
  name text not null,
  address text not null,
  phone text null,
  lat double precision not null check (lat between -90 and 90),
  lng double precision not null check (lng between -180 and 180),
  is_verified boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.users
  add column if not exists is_hospital_manager boolean not null default false;

create table if not exists public.hospital_bed_availability (
  hospital_id uuid primary key references public.hospitals(id) on delete cascade,
  general_available integer not null default 0 check (general_available >= 0),
  icu_available integer not null default 0 check (icu_available >= 0),
  oxygen_available integer not null default 0 check (oxygen_available >= 0),
  emergency_available integer not null default 0 check (emergency_available >= 0),
  isolation_available integer not null default 0 check (isolation_available >= 0),
  updated_at timestamptz not null default now(),
  verified_at timestamptz null
);

create index if not exists hospitals_location_idx
  on public.hospitals(lat, lng);

create index if not exists hospital_bed_availability_updated_idx
  on public.hospital_bed_availability(updated_at desc);

alter table public.hospitals enable row level security;
alter table public.hospital_bed_availability enable row level security;

drop policy if exists "Anyone can read verified hospitals" on public.hospitals;
create policy "Anyone can read verified hospitals"
  on public.hospitals for select
  using (is_verified);

drop policy if exists "Hospital managers can read own hospital" on public.hospitals;
create policy "Hospital managers can read own hospital"
  on public.hospitals for select
  using (auth.uid() = manager_id);

drop policy if exists "Anyone can read verified availability" on public.hospital_bed_availability;
create policy "Anyone can read verified availability"
  on public.hospital_bed_availability for select
  using (exists (
    select 1 from public.hospitals h
    where h.id = hospital_id and h.is_verified
  ));

drop policy if exists "Hospital managers can manage own availability" on public.hospital_bed_availability;
create policy "Hospital managers can manage own availability"
  on public.hospital_bed_availability for all
  using (exists (
    select 1 from public.hospitals h
    where h.id = hospital_id and h.manager_id = auth.uid()
  ))
  with check (exists (
    select 1 from public.hospitals h
    where h.id = hospital_id and h.manager_id = auth.uid()
  ));

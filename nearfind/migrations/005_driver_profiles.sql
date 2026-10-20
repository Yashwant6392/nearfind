create table if not exists public.driver_profiles (
  provider_id uuid primary key references public.users(id) on delete cascade,
  experience_years integer not null check (experience_years >= 0),
  licence_category text not null,
  languages text[] not null default '{}'::text[],
  bio text null check (bio is null or length(trim(bio)) <= 2000),
  service_radius_km double precision not null default 25 check (service_radius_km > 0 and service_radius_km <= 200),
  is_active boolean not null default true,
  verification_status text not null default 'pending' check (verification_status in ('pending', 'under_review', 'approved', 'rejected', 'suspended')),
  reviewed_by uuid null references public.users(id) on delete set null,
  reviewed_at timestamptz null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists driver_profiles_verification_status_idx
  on public.driver_profiles(verification_status, is_active);

create index if not exists driver_profiles_radius_idx
  on public.driver_profiles(service_radius_km);

alter table public.driver_profiles enable row level security;

-- Protected review/approval columns are not writable by the driver or by the public role.
-- RLS alone cannot restrict which columns are updated, so explicit column-level revokes are required.
revoke update (verification_status, reviewed_by, reviewed_at) on public.driver_profiles from authenticated;
revoke insert (verification_status, reviewed_by, reviewed_at) on public.driver_profiles from authenticated;
revoke update (verification_status, reviewed_by, reviewed_at) on public.driver_profiles from anon;
revoke insert (verification_status, reviewed_by, reviewed_at) on public.driver_profiles from anon;

drop policy if exists "Drivers can create own driver profile" on public.driver_profiles;
create policy "Drivers can create own driver profile"
  on public.driver_profiles for insert
  with check (
    auth.uid() = provider_id
    and verification_status = 'pending'
    and reviewed_by is null
    and reviewed_at is null
  );

drop policy if exists "Drivers can read own driver profile" on public.driver_profiles;
create policy "Drivers can read own driver profile"
  on public.driver_profiles for select
  using (auth.uid() = provider_id);

drop policy if exists "Drivers can update own driver profile" on public.driver_profiles;
create policy "Drivers can update own driver profile"
  on public.driver_profiles for update
  using (auth.uid() = provider_id)
  with check (
    auth.uid() = provider_id
    and verification_status = (select verification_status from public.driver_profiles where provider_id = auth.uid())
    and reviewed_by is not distinct from (select reviewed_by from public.driver_profiles where provider_id = auth.uid())
    and reviewed_at is not distinct from (select reviewed_at from public.driver_profiles where provider_id = auth.uid())
  );
  

-- A driver profile is intentionally separate from the general provider role and is never
-- created by a public self-service path. Review and approval happen through a trusted out-of-band process.

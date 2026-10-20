BEGIN;
-- ============================================================================
-- NearFind Consolidated Migrations: 003 through 013
-- Run this in the Supabase SQL Editor to bring the database schema up to date.
-- Target: Live Supabase Project (https://vgfeslhpqulrtslnqepp.supabase.co)
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 003: Provider Live Location Tracking
-- ----------------------------------------------------------------------------
create table if not exists public.provider_locations (
  provider_id uuid primary key references public.users(id) on delete cascade,
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

-- ----------------------------------------------------------------------------
-- 004: Hospitals and Hospital Bed Availability
-- ----------------------------------------------------------------------------
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

-- ----------------------------------------------------------------------------
-- 005: Driver Profiles
-- ----------------------------------------------------------------------------
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
  review_notes text null check (review_notes is null or length(trim(review_notes)) <= 2000),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.driver_profiles
  add column if not exists review_notes text
  check (review_notes is null or length(trim(review_notes)) <= 2000);

create index if not exists driver_profiles_verification_status_idx
  on public.driver_profiles(verification_status, is_active);

create index if not exists driver_profiles_radius_idx
  on public.driver_profiles(service_radius_km);

alter table public.driver_profiles enable row level security;

revoke update (verification_status, reviewed_by, reviewed_at, review_notes) on public.driver_profiles from authenticated;
revoke insert (verification_status, reviewed_by, reviewed_at, review_notes) on public.driver_profiles from authenticated;
revoke update (verification_status, reviewed_by, reviewed_at, review_notes) on public.driver_profiles from anon;
revoke insert (verification_status, reviewed_by, reviewed_at, review_notes) on public.driver_profiles from anon;

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

drop policy if exists "Drivers can update own profile content" on public.driver_profiles;
create policy "Drivers can update own profile content"
  on public.driver_profiles for update
  using (auth.uid() = provider_id)
  with check (auth.uid() = provider_id);

drop policy if exists "Anyone can read approved active driver profiles" on public.driver_profiles;
create policy "Anyone can read approved active driver profiles"
  on public.driver_profiles for select
  using (verification_status = 'approved' and is_active);

-- ----------------------------------------------------------------------------
-- 006: Driver Reviewers and Verification Requests
-- ----------------------------------------------------------------------------
create table if not exists public.driver_reviewers (
  user_id uuid primary key references public.users(id) on delete cascade,
  reviewer_type text not null default 'driver_verifier' check (reviewer_type = 'driver_verifier'),
  granted_by uuid references public.users(id) on delete set null,
  granted_at timestamptz not null default now(),
  notes text null check (notes is null or length(trim(notes)) <= 2000)
);

create table if not exists public.driver_verification_requests (
  id uuid primary key default gen_random_uuid(),
  driver_id uuid not null references public.driver_profiles(provider_id) on delete cascade,
  verification_type text not null check (verification_type in ('identity', 'driving_license')),
  status text not null default 'submitted' check (status in ('submitted', 'under_review', 'approved', 'rejected', 'needs_resubmission')),
  submitted_at timestamptz not null default now(),
  reviewed_at timestamptz null,
  reviewed_by uuid null references public.driver_reviewers(user_id) on delete set null,
  review_notes text null check (review_notes is null or length(trim(review_notes)) <= 2000),
  document_storage_path text null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists driver_verification_requests_driver_status_idx
  on public.driver_verification_requests(driver_id, status, submitted_at desc);

create index if not exists driver_verification_requests_reviewer_idx
  on public.driver_verification_requests(reviewed_by, status);

alter table public.driver_reviewers enable row level security;
alter table public.driver_verification_requests enable row level security;

revoke update (status, reviewed_by, reviewed_at, review_notes) on public.driver_verification_requests from authenticated;
revoke update (status, reviewed_by, reviewed_at, review_notes) on public.driver_verification_requests from anon;
revoke insert (status, reviewed_by, reviewed_at, review_notes) on public.driver_verification_requests from authenticated;
revoke insert (status, reviewed_by, reviewed_at, review_notes) on public.driver_verification_requests from anon;

drop policy if exists "Driver reviewers are provisioned out of band" on public.driver_reviewers;
create policy "Driver reviewers are provisioned out of band"
  on public.driver_reviewers for insert
  with check (false);

drop policy if exists "Drivers can read own reviewer grant" on public.driver_reviewers;
create policy "Drivers can read own reviewer grant"
  on public.driver_reviewers for select
  using (auth.uid() = user_id);

drop policy if exists "Reviewers can read own grant" on public.driver_reviewers;
create policy "Reviewers can read own grant"
  on public.driver_reviewers for select
  using (auth.uid() = user_id);

drop policy if exists "Drivers can create verification requests" on public.driver_verification_requests;
create policy "Drivers can create verification requests"
  on public.driver_verification_requests for insert
  with check (
    auth.uid() = driver_id
    and status = 'submitted'
    and reviewed_by is null
    and reviewed_at is null
    and review_notes is null
  );

drop policy if exists "Drivers can read own verification requests" on public.driver_verification_requests;
create policy "Drivers can read own verification requests"
  on public.driver_verification_requests for select
  using (auth.uid() = driver_id);

drop policy if exists "Reviewers can read assigned verification requests" on public.driver_verification_requests;
create policy "Reviewers can read assigned verification requests"
  on public.driver_verification_requests for select
  using (exists (select 1 from public.driver_reviewers r where r.user_id = auth.uid()));

-- ----------------------------------------------------------------------------
-- 007: Driver Availability and Locations
-- ----------------------------------------------------------------------------
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
  lat double precision null check (lat between -90 and 90),
  lng double precision null check (lng between -180 and 180),
  accuracy_m double precision null check (accuracy_m is null or (accuracy_m >= 0 and accuracy_m <= 100000)),
  updated_at timestamptz not null default now(),
  sharing_enabled boolean not null default false,
  check ((lat is null and lng is null) or (lat is not null and lng is not null))
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

-- ----------------------------------------------------------------------------
-- 008: Driver Requests and Candidates
-- ----------------------------------------------------------------------------
create table if not exists public.driver_requests (
  id uuid primary key default gen_random_uuid(),
  seeker_id uuid not null references public.users(id) on delete cascade,
  pickup_address text not null,
  pickup_lat double precision not null check (pickup_lat between -90 and 90),
  pickup_lng double precision not null check (pickup_lng between -180 and 180),
  destination_address text not null,
  destination_lat double precision not null check (destination_lat between -90 and 90),
  destination_lng double precision not null check (destination_lng between -180 and 180),
  requested_start timestamptz not null,
  requested_end timestamptz not null,
  duration_minutes integer not null check (duration_minutes > 0 and duration_minutes <= 10080),
  return_journey boolean not null default false,
  amenities text[] not null default '{}'::text[] check (
    amenities <@ ARRAY['food', 'rest_breaks', 'sleeping_space', 'overnight_accommodation']::text[]
  ),
  optional_preferences text null check (optional_preferences is null or length(trim(optional_preferences)) <= 1000),
  requested_radius_km double precision not null default 25 check (requested_radius_km > 0 and requested_radius_km <= 200),
  status text not null default 'open' check (status in ('open', 'matched', 'expired', 'cancelled', 'completed')),
  created_at timestamptz not null default now(),
  expires_at timestamptz null,
  updated_at timestamptz not null default now(),
  check (requested_end > requested_start)
);

create index if not exists driver_requests_status_expires_idx
  on public.driver_requests(status, expires_at);

create index if not exists driver_requests_seeker_time_idx
  on public.driver_requests(seeker_id, requested_start, status);

create table if not exists public.driver_request_candidates (
  id uuid primary key default gen_random_uuid(),
  request_id uuid not null references public.driver_requests(id) on delete cascade,
  driver_id uuid not null references public.driver_profiles(provider_id) on delete cascade,
  candidate_status text not null default 'notified' check (candidate_status in ('notified', 'viewed', 'rejected', 'conflict', 'expired', 'accepted')),
  notified_at timestamptz not null default now(),
  viewed_at timestamptz null,
  responded_at timestamptz null,
  metadata jsonb null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint driver_request_candidates_unique_pair unique (request_id, driver_id)
);

create index if not exists driver_request_candidates_driver_idx
  on public.driver_request_candidates(driver_id, candidate_status);

alter table public.driver_requests enable row level security;
alter table public.driver_request_candidates enable row level security;

drop policy if exists "Seekers manage own driver requests" on public.driver_requests;
create policy "Seekers manage own driver requests"
  on public.driver_requests for all
  using (auth.uid() = seeker_id)
  with check (auth.uid() = seeker_id);

drop policy if exists "Drivers can read candidate request assignments" on public.driver_request_candidates;
create policy "Drivers can read candidate request assignments"
  on public.driver_request_candidates for select
  using (auth.uid() = driver_id);

-- ----------------------------------------------------------------------------
-- 009: Driver Bookings and Audit Events
-- ----------------------------------------------------------------------------
create extension if not exists "btree_gist";

create table if not exists public.driver_bookings (
  id uuid primary key default gen_random_uuid(),
  driver_id uuid not null references public.driver_profiles(provider_id) on delete restrict,
  seeker_id uuid not null references public.users(id) on delete restrict,
  request_id uuid not null unique references public.driver_requests(id) on delete restrict,
  start_at timestamptz not null,
  end_at timestamptz not null,
  status text not null default 'accepted' check (status in ('accepted', 'in_progress', 'completed', 'cancelled_by_driver', 'cancelled_by_seeker', 'expired', 'rejected')),
  accepted_at timestamptz null,
  cancelled_at timestamptz null,
  completed_at timestamptz null,
  cancellation_reason text null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check (end_at > start_at)
);

create index if not exists driver_bookings_driver_time_idx
  on public.driver_bookings(driver_id, start_at, end_at);

create index if not exists driver_bookings_seeker_status_idx
  on public.driver_bookings(seeker_id, status, start_at);

alter table public.driver_bookings enable row level security;

alter table public.driver_bookings
  drop constraint if exists driver_bookings_no_overlap;

alter table public.driver_bookings
  add constraint driver_bookings_no_overlap
  exclude using gist (
    driver_id with =,
    tstzrange(start_at, end_at, '[)') with &&
  ) where (status in ('accepted', 'in_progress'));

drop policy if exists "Driver and seeker can read own bookings" on public.driver_bookings;
create policy "Driver and seeker can read own bookings"
  on public.driver_bookings for select
  using (auth.uid() = driver_id or auth.uid() = seeker_id);

drop policy if exists "Drivers can update own bookings" on public.driver_bookings;
create policy "Drivers can update own bookings"
  on public.driver_bookings for update
  using (auth.uid() = driver_id)
  with check (auth.uid() = driver_id);

drop policy if exists "Seekers can update own bookings" on public.driver_bookings;
create policy "Seekers can update own bookings"
  on public.driver_bookings for update
  using (auth.uid() = seeker_id)
  with check (auth.uid() = seeker_id);

create table if not exists public.driver_booking_events (
  id uuid primary key default gen_random_uuid(),
  booking_id uuid not null references public.driver_bookings(id) on delete cascade,
  event_type text not null check (event_type in ('accepted', 'in_progress', 'completed', 'cancelled_by_driver', 'cancelled_by_seeker', 'expired', 'rejected', 'conflict')),
  created_by uuid not null references public.users(id) on delete restrict,
  status_before text null,
  status_after text null,
  details jsonb null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index if not exists driver_booking_events_booking_idx
  on public.driver_booking_events(booking_id, created_at desc);

alter table public.driver_booking_events enable row level security;

drop policy if exists "Participants can read booking events" on public.driver_booking_events;
create policy "Participants can read booking events"
  on public.driver_booking_events for select
  using (
    exists (
      select 1
      from public.driver_bookings b
      where b.id = booking_id
        and (b.driver_id = auth.uid() or b.seeker_id = auth.uid())
    )
  );

-- ----------------------------------------------------------------------------
-- 010: Driver Profile Self-Approval Guard Triggers
-- ----------------------------------------------------------------------------
create or replace function public.driver_profiles_self_approval_guard()
returns trigger
language plpgsql
set search_path = public
as $$
begin
  if TG_OP = 'INSERT' then
    if NEW.verification_status is distinct from 'pending' then
      raise exception 'driver profile approval status is reviewer-controlled';
    end if;
    if NEW.reviewed_by is not null or NEW.reviewed_at is not null then
      raise exception 'driver profile review metadata is reviewer-controlled';
    end if;
    return NEW;
  end if;

  if TG_OP = 'UPDATE' then
    if NEW.provider_id is distinct from OLD.provider_id then
      raise exception 'driver profile owner cannot be reassigned';
    end if;
    if auth.uid() = OLD.provider_id then
      if NEW.verification_status is distinct from OLD.verification_status then
        raise exception 'driver cannot change verification_status';
      end if;
      if NEW.reviewed_by is distinct from OLD.reviewed_by then
        raise exception 'driver cannot change reviewed_by';
      end if;
      if NEW.reviewed_at is distinct from OLD.reviewed_at then
        raise exception 'driver cannot change reviewed_at';
      end if;
    end if;
    if NEW.reviewed_by is not null and NEW.reviewed_by = OLD.provider_id then
      raise exception 'reviewer cannot approve their own driver profile';
    end if;
    return NEW;
  end if;
  return NEW;
end;
$$;

drop trigger if exists driver_profiles_self_approval_guard on public.driver_profiles;
create trigger driver_profiles_self_approval_guard
before insert or update on public.driver_profiles
for each row execute function public.driver_profiles_self_approval_guard();

create or replace function public.driver_verification_requests_self_approval_guard()
returns trigger
language plpgsql
set search_path = public
as $$
begin
  if TG_OP = 'INSERT' then
    if NEW.driver_id = auth.uid() then
      if NEW.status not in ('submitted', 'needs_resubmission') then
        raise exception 'drivers may not set approval or review states on verification requests';
      end if;

      if NEW.reviewed_by is not null or NEW.reviewed_at is not null then
        raise exception 'drivers may not set reviewer metadata on verification requests';
      end if;
    else
      raise exception 'only the owning driver may create their own verification request';
    end if;

    if NEW.reviewed_by is not null and NEW.reviewed_by = NEW.driver_id then
      raise exception 'reviewers may not approve their own verification request';
    end if;

    return NEW;
  end if;

  if TG_OP = 'UPDATE' then
    if NEW.driver_id is distinct from OLD.driver_id then
      raise exception 'verification request owner cannot be reassigned';
    end if;

    if auth.uid() = NEW.driver_id then
      if NEW.status is distinct from OLD.status and NEW.status in ('approved', 'rejected', 'under_review') then
        raise exception 'drivers may not approve or reject verification requests';
      end if;

      if NEW.reviewed_by is distinct from OLD.reviewed_by then
        raise exception 'drivers may not set reviewer metadata on verification requests';
      end if;

      if NEW.reviewed_at is distinct from OLD.reviewed_at then
        raise exception 'drivers may not set review timestamps on verification requests';
      end if;
    end if;

    if NEW.reviewed_by is not null and NEW.reviewed_by = NEW.driver_id then
      raise exception 'reviewers may not approve their own verification request';
    end if;

    if NEW.reviewed_by is not null and not exists (
      select 1
      from public.driver_reviewers dr
      where dr.user_id = NEW.reviewed_by
    ) then
      raise exception 'reviewed_by must reference a provisioned reviewer';
    end if;

    return NEW;
  end if;

  return NEW;
end;
$$;

drop trigger if exists driver_verification_requests_self_approval_guard on public.driver_verification_requests;
create trigger driver_verification_requests_self_approval_guard
before insert or update on public.driver_verification_requests
for each row execute function public.driver_verification_requests_self_approval_guard();

-- ----------------------------------------------------------------------------
-- 011: Driver Workflow Server RPCs
-- ----------------------------------------------------------------------------
-- Server-side workflow RPCs. Application callers must authenticate users first and
-- provide the authenticated session identity; only the server service role can execute
-- these functions. Reviewer grants are provisioned out of band.

alter table public.driver_locations
  alter column lat drop not null,
  alter column lng drop not null;

alter table public.driver_locations
  drop constraint if exists driver_locations_coordinate_pair;

alter table public.driver_locations
  add constraint driver_locations_coordinate_pair
  check ((lat is null and lng is null) or (lat is not null and lng is not null));

alter table public.driver_profiles
  add column if not exists review_notes text
  check (review_notes is null or length(trim(review_notes)) <= 2000);

revoke insert (review_notes) on public.driver_profiles from authenticated, anon;
revoke update (review_notes) on public.driver_profiles from authenticated, anon;

create or replace function public.driver_profiles_review_notes_guard()
returns trigger
language plpgsql
set search_path = public
as $$
begin
  if TG_OP = 'INSERT' and NEW.review_notes is not null then
    raise exception 'driver profile review notes are reviewer-controlled';
  end if;
  if TG_OP = 'UPDATE'
    and auth.uid() = OLD.provider_id
    and NEW.review_notes is distinct from OLD.review_notes then
    raise exception 'driver cannot change review notes';
  end if;
  return NEW;
end;
$$;

drop trigger if exists driver_profiles_review_notes_guard on public.driver_profiles;
create trigger driver_profiles_review_notes_guard
before insert or update on public.driver_profiles
for each row execute function public.driver_profiles_review_notes_guard();

alter table public.driver_availability
  drop constraint if exists driver_availability_no_active_overlap;

alter table public.driver_availability
  add constraint driver_availability_no_active_overlap
  exclude using gist (
    driver_id with =,
    tstzrange(starts_at, ends_at, '[)') with &&
  ) where (is_active);

drop policy if exists "Matched seeker can read active driver location" on public.driver_locations;
create policy "Matched seeker can read active driver location"
  on public.driver_locations for select
  using (
    sharing_enabled
    and lat is not null
    and lng is not null
    and updated_at >= now() - interval '15 minutes'
    and exists (
      select 1
      from public.driver_bookings b
      where b.driver_id = driver_locations.driver_id
        and b.seeker_id = auth.uid()
        and b.status in ('accepted', 'in_progress')
        and now() >= b.start_at
        and now() < b.end_at
    )
  );

revoke insert, update, delete on public.driver_profiles from anon, authenticated;
drop policy if exists "Drivers can create own driver profile" on public.driver_profiles;
drop policy if exists "Drivers can update own driver profile" on public.driver_profiles;
revoke insert, update, delete on public.driver_requests from anon, authenticated;
revoke insert, update, delete on public.driver_bookings from anon, authenticated;
revoke insert, update, delete on public.driver_booking_events from anon, authenticated;
revoke insert, update, delete on public.driver_request_candidates from anon, authenticated;
revoke insert, update, delete on public.driver_availability from anon, authenticated;
revoke insert, update, delete on public.driver_locations from anon, authenticated;

create or replace function public.save_driver_profile(
  p_provider_id uuid,
  p_experience_years integer,
  p_licence_category text,
  p_languages text[],
  p_bio text,
  p_service_radius_km double precision
)
returns public.driver_profiles
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  saved public.driver_profiles;
begin
  if not exists (select 1 from public.users where id = p_provider_id and role = 'provider' and is_active) then
    raise exception 'Provider account not found';
  end if;
  if p_experience_years is null or p_experience_years < 0 or p_experience_years > 60 then
    raise exception 'Experience must be between 0 and 60 years';
  end if;
  if p_licence_category is null or length(trim(p_licence_category)) not between 1 and 40 then
    raise exception 'Licence category is required and must be 40 characters or fewer';
  end if;
  if p_service_radius_km is null or p_service_radius_km <= 0 or p_service_radius_km > 200 then
    raise exception 'Service radius must be greater than 0 and at most 200 km';
  end if;

  insert into public.driver_profiles (
    provider_id, experience_years, licence_category, languages, bio, service_radius_km
  ) values (
    p_provider_id, p_experience_years, trim(p_licence_category),
    coalesce(p_languages, '{}'::text[]), nullif(trim(p_bio), ''), p_service_radius_km
  )
  on conflict (provider_id) do update set
    experience_years = excluded.experience_years,
    licence_category = excluded.licence_category,
    languages = excluded.languages,
    bio = excluded.bio,
    service_radius_km = excluded.service_radius_km,
    updated_at = now()
  returning * into saved;

  return saved;
end;
$$;

create or replace function public.review_driver_profile(
  p_reviewer_id uuid,
  p_driver_id uuid,
  p_decision text,
  p_review_notes text default null
)
returns public.driver_profiles
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  reviewed public.driver_profiles;
begin
  if p_reviewer_id is null or p_driver_id is null or p_reviewer_id = p_driver_id then
    raise exception 'A reviewer cannot review their own driver profile';
  end if;
  if not exists (
    select 1 from public.driver_reviewers dr
    join public.users u on u.id = dr.user_id
    where dr.user_id = p_reviewer_id and u.is_active
  ) then
    raise exception 'Trusted driver reviewer authorization required';
  end if;
  if p_decision is null or p_decision not in ('under_review', 'approved', 'rejected', 'suspended') then
    raise exception 'Invalid driver review decision';
  end if;
  if p_review_notes is not null and length(trim(p_review_notes)) > 2000 then
    raise exception 'Review notes must be 2000 characters or fewer';
  end if;

  update public.driver_profiles
  set verification_status = p_decision,
      reviewed_by = p_reviewer_id,
      reviewed_at = now(),
      review_notes = nullif(trim(p_review_notes), ''),
      updated_at = now()
  where provider_id = p_driver_id
    and verification_status in ('pending', 'under_review', 'approved', 'rejected')
  returning * into reviewed;

  if not found then
    raise exception 'Driver profile is missing or no longer reviewable';
  end if;

  return reviewed;
end;
$$;

create or replace function public.set_driver_presence(
  p_driver_id uuid,
  p_starts_at timestamptz,
  p_ends_at timestamptz,
  p_sharing_enabled boolean,
  p_lat double precision default null,
  p_lng double precision default null
)
returns uuid
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  availability_id uuid;
begin
  if not exists (
    select 1 from public.driver_profiles
    where provider_id = p_driver_id
      and verification_status = 'approved'
      and is_active
  ) then
    raise exception 'Only an approved active driver can publish availability';
  end if;
  if p_starts_at is null or p_ends_at is null or p_starts_at <= now() or p_ends_at <= p_starts_at then
    raise exception 'Availability must be a future time range';
  end if;
  if p_ends_at - p_starts_at > interval '7 days' then
    raise exception 'Availability cannot exceed 7 days';
  end if;
  if p_sharing_enabled and (
    p_lat is null or p_lng is null
    or p_lat < -90 or p_lat > 90
    or p_lng < -180 or p_lng > 180
  ) then
    raise exception 'Valid coordinates are required when location sharing is enabled';
  end if;

  update public.driver_availability set is_active = false, updated_at = now()
  where driver_id = p_driver_id and is_active;

  insert into public.driver_availability (driver_id, starts_at, ends_at)
  values (p_driver_id, p_starts_at, p_ends_at)
  returning id into availability_id;

  insert into public.driver_locations (driver_id, lat, lng, sharing_enabled, updated_at)
  values (
    p_driver_id,
    case when p_sharing_enabled then p_lat else null end,
    case when p_sharing_enabled then p_lng else null end,
    coalesce(p_sharing_enabled, false),
    now()
  )
  on conflict (driver_id) do update set
    lat = excluded.lat,
    lng = excluded.lng,
    sharing_enabled = excluded.sharing_enabled,
    updated_at = now();

  return availability_id;
end;
$$;

create or replace function public.refresh_driver_location(
  p_driver_id uuid,
  p_lat double precision,
  p_lng double precision,
  p_sharing_enabled boolean
)
returns timestamptz
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  refreshed_at timestamptz := now();
begin
  if not exists (
    select 1 from public.driver_profiles
    where provider_id = p_driver_id
      and verification_status = 'approved'
      and is_active
  ) then
    raise exception 'Only an approved active driver can update driver location';
  end if;
  if p_sharing_enabled and (
    p_lat is null or p_lng is null
    or p_lat < -90 or p_lat > 90
    or p_lng < -180 or p_lng > 180
  ) then
    raise exception 'Valid coordinates are required when location sharing is enabled';
  end if;
  if not p_sharing_enabled then
    raise exception 'Location sharing is disabled; do not submit driver coordinates';
  end if;

  update public.driver_locations
  set lat = p_lat, lng = p_lng, updated_at = refreshed_at
  where driver_id = p_driver_id and sharing_enabled;
  if not found then
    raise exception 'Enable and save location sharing before refreshing coordinates';
  end if;

  return refreshed_at;
end;
$$;

create or replace function public.create_driver_request(
  p_seeker_id uuid,
  p_pickup_address text,
  p_pickup_lat double precision,
  p_pickup_lng double precision,
  p_destination_address text,
  p_destination_lat double precision,
  p_destination_lng double precision,
  p_requested_start timestamptz,
  p_duration_minutes integer,
  p_amenities text[] default '{}'::text[],
  p_requested_radius_km double precision default 25
)
returns uuid
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  new_request_id uuid;
begin
  if not exists (select 1 from public.users where id = p_seeker_id and role = 'seeker' and is_active) then
    raise exception 'Seeker account not found';
  end if;
  if p_pickup_lat is null or p_pickup_lng is null
    or p_destination_lat is null or p_destination_lng is null
    or length(trim(coalesce(p_pickup_address, ''))) not between 1 and 250
    or length(trim(coalesce(p_destination_address, ''))) not between 1 and 250 then
    raise exception 'Pickup and destination are required';
  end if;
  if p_pickup_lat not between -90 and 90 or p_pickup_lng not between -180 and 180
    or p_destination_lat not between -90 and 90 or p_destination_lng not between -180 and 180 then
    raise exception 'Pickup and destination coordinates are invalid';
  end if;
  if p_requested_start is null or p_requested_start <= now() then
    raise exception 'Requested start time must be in the future';
  end if;
  if p_duration_minutes is null or p_duration_minutes < 1 or p_duration_minutes > 10080 then
    raise exception 'Duration must be between 1 minute and 7 days';
  end if;
  if p_requested_radius_km is null or p_requested_radius_km <= 0 or p_requested_radius_km > 200 then
    raise exception 'Search radius must be greater than 0 and at most 200 km';
  end if;
  if not (coalesce(p_amenities, '{}'::text[]) <@ array['food', 'rest_breaks']::text[]) then
    raise exception 'Invalid amenity selection';
  end if;

  insert into public.driver_requests (
    seeker_id, pickup_address, pickup_lat, pickup_lng,
    destination_address, destination_lat, destination_lng,
    requested_start, requested_end, duration_minutes, amenities, requested_radius_km
  ) values (
    p_seeker_id, trim(p_pickup_address), p_pickup_lat, p_pickup_lng,
    trim(p_destination_address), p_destination_lat, p_destination_lng,
    p_requested_start, p_requested_start + make_interval(mins => p_duration_minutes),
    p_duration_minutes, coalesce(p_amenities, '{}'::text[]), p_requested_radius_km
  )
  returning id into new_request_id;

  return new_request_id;
end;
$$;

create or replace function public.find_drivers_for_request(p_seeker_id uuid, p_request_id uuid)
returns table (
  driver_id uuid,
  display_name text,
  experience_years integer,
  licence_category text,
  languages text[],
  bio text,
  distance_km double precision
)
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  req public.driver_requests;
begin
  select * into req from public.driver_requests
  where id = p_request_id and seeker_id = p_seeker_id;
  if not found then
    raise exception 'Driver request not found';
  end if;

  return query
  select dp.provider_id, coalesce(u.name, 'Driver'), dp.experience_years,
         dp.licence_category, dp.languages, dp.bio,
         distance.km
  from public.driver_profiles dp
  join public.users u on u.id = dp.provider_id
  join public.driver_locations dl on dl.driver_id = dp.provider_id
  cross join lateral (
    select (6371 * 2 * asin(sqrt(least(1.0,
      power(sin(radians(dl.lat - req.pickup_lat) / 2), 2)
      + cos(radians(req.pickup_lat)) * cos(radians(dl.lat))
      * power(sin(radians(dl.lng - req.pickup_lng) / 2), 2)
    ))))::double precision as km
  ) distance
  where dp.verification_status = 'approved'
    and dp.is_active
    and u.is_active
    and dl.sharing_enabled
    and dl.lat is not null and dl.lng is not null
    and dl.updated_at >= now() - interval '15 minutes'
    and distance.km <= least(dp.service_radius_km, req.requested_radius_km)
    and exists (
      select 1 from public.driver_availability da
      where da.driver_id = dp.provider_id and da.is_active
        and da.starts_at <= req.requested_start and da.ends_at >= req.requested_end
    )
    and not exists (
      select 1 from public.driver_bookings b
      where b.driver_id = dp.provider_id
        and b.status in ('accepted', 'in_progress')
        and tstzrange(b.start_at, b.end_at, '[)') && tstzrange(req.requested_start, req.requested_end, '[)')
    )
  order by distance.km, dp.provider_id;
end;
$$;

create or replace function public.list_driver_requests_for_driver(p_driver_id uuid)
returns table (
  request_id uuid,
  seeker_name text,
  pickup_address text,
  pickup_lat double precision,
  pickup_lng double precision,
  destination_address text,
  destination_lat double precision,
  destination_lng double precision,
  requested_start timestamptz,
  requested_end timestamptz,
  duration_minutes integer,
  amenities text[],
  distance_km double precision
)
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  profile public.driver_profiles;
  location public.driver_locations;
begin
  if not exists (
    select 1 from public.users where id = p_driver_id and role = 'provider' and is_active
  ) then
    return;
  end if;
  select * into profile from public.driver_profiles
  where provider_id = p_driver_id and verification_status = 'approved' and is_active;
  if not found then
    return;
  end if;
  select * into location from public.driver_locations
  where driver_id = p_driver_id and sharing_enabled
    and updated_at >= now() - interval '15 minutes';
  if not found or location.lat is null or location.lng is null then
    return;
  end if;

  return query
  select r.id, coalesce(u.name, 'Seeker'), r.pickup_address, r.pickup_lat, r.pickup_lng,
         r.destination_address, r.destination_lat, r.destination_lng,
         r.requested_start, r.requested_end, r.duration_minutes, r.amenities,
         distance.km
  from public.driver_requests r
  join public.users u on u.id = r.seeker_id
  cross join lateral (
    select (6371 * 2 * asin(sqrt(least(1.0,
      power(sin(radians(r.pickup_lat - location.lat) / 2), 2)
      + cos(radians(location.lat)) * cos(radians(r.pickup_lat))
      * power(sin(radians(r.pickup_lng - location.lng) / 2), 2)
    ))))::double precision as km
  ) distance
  where r.status = 'open' and r.requested_start > now()
    and u.is_active
    and distance.km <= least(profile.service_radius_km, r.requested_radius_km)
    and exists (
      select 1 from public.driver_availability da
      where da.driver_id = p_driver_id and da.is_active
        and da.starts_at <= r.requested_start and da.ends_at >= r.requested_end
    )
    and not exists (
      select 1 from public.driver_bookings b
      where b.driver_id = p_driver_id
        and b.status in ('accepted', 'in_progress')
        and tstzrange(b.start_at, b.end_at, '[)') && tstzrange(r.requested_start, r.requested_end, '[)')
    )
  order by r.requested_start, distance.km;
end;
$$;

create or replace function public.accept_driver_request(p_driver_id uuid, p_request_id uuid)
returns public.driver_bookings
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  req public.driver_requests;
  profile public.driver_profiles;
  location public.driver_locations;
  created_booking public.driver_bookings;
  distance_km double precision;
begin
  if not exists (
    select 1 from public.users where id = p_driver_id and role = 'provider' and is_active
  ) then
    raise exception 'Active provider account required';
  end if;
  select * into req from public.driver_requests where id = p_request_id for update;
  if not found or req.status <> 'open' or req.requested_start <= now() then
    raise exception 'Driver request is no longer available';
  end if;
  if not exists (select 1 from public.users where id = req.seeker_id and role = 'seeker' and is_active) then
    raise exception 'The requesting seeker account is inactive';
  end if;

  select * into profile from public.driver_profiles
  where provider_id = p_driver_id and verification_status = 'approved' and is_active;
  if not found then
    raise exception 'Only an approved active driver can accept requests';
  end if;
  select * into location from public.driver_locations
  where driver_id = p_driver_id and sharing_enabled
    and updated_at >= now() - interval '15 minutes';
  if not found or location.lat is null or location.lng is null then
    raise exception 'Fresh shared driver location is required to accept a request';
  end if;

  select 6371 * 2 * asin(sqrt(least(1.0,
    power(sin(radians(location.lat - req.pickup_lat) / 2), 2)
    + cos(radians(req.pickup_lat)) * cos(radians(location.lat))
    * power(sin(radians(location.lng - req.pickup_lng) / 2), 2)
  ))) into distance_km;
  if distance_km > least(profile.service_radius_km, req.requested_radius_km) then
    raise exception 'Driver is outside the request service radius';
  end if;
  if not exists (
    select 1 from public.driver_availability da
    where da.driver_id = p_driver_id and da.is_active
      and da.starts_at <= req.requested_start and da.ends_at >= req.requested_end
  ) then
    raise exception 'Driver is not available for the requested time';
  end if;

  update public.driver_requests set status = 'matched', updated_at = now()
  where id = req.id and status = 'open';
  if not found then
    raise exception 'Driver request was accepted by another driver';
  end if;

  insert into public.driver_bookings (
    driver_id, seeker_id, request_id, start_at, end_at, status, accepted_at
  ) values (
    p_driver_id, req.seeker_id, req.id, req.requested_start, req.requested_end, 'accepted', now()
  ) returning * into created_booking;

  insert into public.driver_booking_events (
    booking_id, event_type, created_by, status_before, status_after
  ) values (
    created_booking.id, 'accepted', p_driver_id, null, 'accepted'
  );

  return created_booking;
end;
$$;

revoke all on function public.save_driver_profile(uuid, integer, text, text[], text, double precision) from public, anon, authenticated;
revoke all on function public.review_driver_profile(uuid, uuid, text, text) from public, anon, authenticated;
revoke all on function public.set_driver_presence(uuid, timestamptz, timestamptz, boolean, double precision, double precision) from public, anon, authenticated;
revoke all on function public.refresh_driver_location(uuid, double precision, double precision, boolean) from public, anon, authenticated;
revoke all on function public.create_driver_request(uuid, text, double precision, double precision, text, double precision, double precision, timestamptz, integer, text[], double precision) from public, anon, authenticated;
revoke all on function public.find_drivers_for_request(uuid, uuid) from public, anon, authenticated;
revoke all on function public.list_driver_requests_for_driver(uuid) from public, anon, authenticated;
revoke all on function public.accept_driver_request(uuid, uuid) from public, anon, authenticated;

grant execute on function public.save_driver_profile(uuid, integer, text, text[], text, double precision) to service_role;
grant execute on function public.review_driver_profile(uuid, uuid, text, text) to service_role;
grant execute on function public.set_driver_presence(uuid, timestamptz, timestamptz, boolean, double precision, double precision) to service_role;
grant execute on function public.refresh_driver_location(uuid, double precision, double precision, boolean) to service_role;
grant execute on function public.create_driver_request(uuid, text, double precision, double precision, text, double precision, double precision, timestamptz, integer, text[], double precision) to service_role;
grant execute on function public.find_drivers_for_request(uuid, uuid) to service_role;
grant execute on function public.list_driver_requests_for_driver(uuid) to service_role;
grant execute on function public.accept_driver_request(uuid, uuid) to service_role;

-- ----------------------------------------------------------------------------
-- 012: User Business Name
-- ----------------------------------------------------------------------------
alter table public.users
  add column if not exists business_name text null;

-- ----------------------------------------------------------------------------
-- 013: Hospital Bed Holds and Atomic Concurrency RPCs
-- ----------------------------------------------------------------------------
create table if not exists public.hospital_bed_holds (
  id uuid primary key default gen_random_uuid(),
  hospital_id uuid not null references public.hospitals(id) on delete cascade,
  seeker_id uuid not null references public.users(id) on delete cascade,
  bed_type text not null check (bed_type in ('general', 'icu', 'oxygen', 'emergency', 'isolation')),
  status text not null default 'held' check (status in ('held', 'admitted', 'cancelled', 'expired')),
  notes text null,
  created_at timestamptz not null default now(),
  expires_at timestamptz not null default (now() + interval '2 hours')
);

create index if not exists hospital_bed_holds_hospital_idx
  on public.hospital_bed_holds(hospital_id, status);

create index if not exists hospital_bed_holds_seeker_idx
  on public.hospital_bed_holds(seeker_id, status);

create unique index if not exists hospital_bed_holds_active_seeker_idx
  on public.hospital_bed_holds(seeker_id)
  where (status = 'held');

alter table public.hospital_bed_holds enable row level security;

drop policy if exists "Hospital managers can read own facility holds" on public.hospital_bed_holds;
create policy "Hospital managers can read own facility holds"
  on public.hospital_bed_holds for select
  using (exists (
    select 1 from public.hospitals h
    where h.id = hospital_id and h.manager_id = auth.uid()
  ));

drop policy if exists "Seekers can read own bed holds" on public.hospital_bed_holds;
create policy "Seekers can read own bed holds"
  on public.hospital_bed_holds for select
  using (auth.uid() = seeker_id);

drop policy if exists "Seekers can create own bed holds" on public.hospital_bed_holds;
create policy "Seekers can create own bed holds"
  on public.hospital_bed_holds for insert
  with check (auth.uid() = seeker_id);

drop policy if exists "Hospital managers can update hold statuses" on public.hospital_bed_holds;
create policy "Hospital managers can update hold statuses"
  on public.hospital_bed_holds for update
  using (exists (
    select 1 from public.hospitals h
    where h.id = hospital_id and h.manager_id = auth.uid()
  ))
  with check (exists (
    select 1 from public.hospitals h
    where h.id = hospital_id and h.manager_id = auth.uid()
  ));

create or replace function public.hold_hospital_bed(
  p_hospital_id uuid,
  p_seeker_id uuid,
  p_bed_type text,
  p_notes text default null
)
returns public.hospital_bed_holds
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_avail public.hospital_bed_availability;
  v_created_hold public.hospital_bed_holds;
  v_current_count integer;
begin
  if not exists (select 1 from public.users where id = p_seeker_id and role = 'seeker' and is_active) then
    raise exception 'Active seeker account required to hold emergency bed';
  end if;

  if not exists (select 1 from public.hospitals where id = p_hospital_id) then
    raise exception 'Hospital not found';
  end if;

  if exists (
    select 1 from public.hospital_bed_holds
    where seeker_id = p_seeker_id
      and status = 'held'
      and expires_at > now()
  ) then
    raise exception 'Seeker already has an active emergency bed hold';
  end if;

  if p_bed_type not in ('general', 'icu', 'oxygen', 'emergency', 'isolation') then
    raise exception 'Invalid bed category: %', p_bed_type;
  end if;

  select * into v_avail
  from public.hospital_bed_availability
  where hospital_id = p_hospital_id
  for update;

  if not found then
    raise exception 'Hospital bed availability record not found';
  end if;

  if p_bed_type = 'general' then
    v_current_count := v_avail.general_available;
    if v_current_count <= 0 then
      raise exception 'General beds are currently full';
    end if;
    update public.hospital_bed_availability
    set general_available = general_available - 1,
        updated_at = now()
    where hospital_id = p_hospital_id;
  elsif p_bed_type = 'icu' then
    v_current_count := v_avail.icu_available;
    if v_current_count <= 0 then
      raise exception 'ICU beds are currently full';
    end if;
    update public.hospital_bed_availability
    set icu_available = icu_available - 1,
        updated_at = now()
    where hospital_id = p_hospital_id;
  elsif p_bed_type = 'oxygen' then
    v_current_count := v_avail.oxygen_available;
    if v_current_count <= 0 then
      raise exception 'Oxygen-supported beds are currently full';
    end if;
    update public.hospital_bed_availability
    set oxygen_available = oxygen_available - 1,
        updated_at = now()
    where hospital_id = p_hospital_id;
  elsif p_bed_type = 'emergency' then
    v_current_count := v_avail.emergency_available;
    if v_current_count <= 0 then
      raise exception 'Emergency beds are currently full';
    end if;
    update public.hospital_bed_availability
    set emergency_available = emergency_available - 1,
        updated_at = now()
    where hospital_id = p_hospital_id;
  elsif p_bed_type = 'isolation' then
    v_current_count := v_avail.isolation_available;
    if v_current_count <= 0 then
      raise exception 'Isolation beds are currently full';
    end if;
    update public.hospital_bed_availability
    set isolation_available = isolation_available - 1,
        updated_at = now()
    where hospital_id = p_hospital_id;
  end if;

  insert into public.hospital_bed_holds (
    hospital_id,
    seeker_id,
    bed_type,
    status,
    notes,
    created_at,
    expires_at
  ) values (
    p_hospital_id,
    p_seeker_id,
    p_bed_type,
    'held',
    p_notes,
    now(),
    now() + interval '2 hours'
  )
  returning * into v_created_hold;

  return v_created_hold;
end;
$$;

revoke all on function public.hold_hospital_bed(uuid, uuid, text, text) from public, anon;
grant execute on function public.hold_hospital_bed(uuid, uuid, text, text) to authenticated, service_role;

create or replace function public.release_hospital_bed_hold(
  p_hold_id uuid,
  p_actor_id uuid,
  p_new_status text,
  p_notes text default null
)
returns public.hospital_bed_holds
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_hold public.hospital_bed_holds;
begin
  if p_new_status not in ('admitted', 'cancelled', 'expired') then
    raise exception 'Invalid status: %', p_new_status;
  end if;

  select * into v_hold
  from public.hospital_bed_holds
  where id = p_hold_id
  for update;

  if not found or v_hold.status <> 'held' then
    raise exception 'Active bed hold not found';
  end if;

  if v_hold.seeker_id <> p_actor_id and not exists (
    select 1 from public.hospitals where id = v_hold.hospital_id and manager_id = p_actor_id
  ) then
    raise exception 'Unauthorized to release this bed hold';
  end if;

  update public.hospital_bed_holds
  set status = p_new_status,
      notes = coalesce(p_notes, notes)
  where id = p_hold_id
  returning * into v_hold;

  if p_new_status in ('cancelled', 'expired') then
    if v_hold.bed_type = 'general' then
      update public.hospital_bed_availability set general_available = general_available + 1, updated_at = now() where hospital_id = v_hold.hospital_id;
    elsif v_hold.bed_type = 'icu' then
      update public.hospital_bed_availability set icu_available = icu_available + 1, updated_at = now() where hospital_id = v_hold.hospital_id;
    elsif v_hold.bed_type = 'oxygen' then
      update public.hospital_bed_availability set oxygen_available = oxygen_available + 1, updated_at = now() where hospital_id = v_hold.hospital_id;
    elsif v_hold.bed_type = 'emergency' then
      update public.hospital_bed_availability set emergency_available = emergency_available + 1, updated_at = now() where hospital_id = v_hold.hospital_id;
    elsif v_hold.bed_type = 'isolation' then
      update public.hospital_bed_availability set isolation_available = isolation_available + 1, updated_at = now() where hospital_id = v_hold.hospital_id;
    end if;
  end if;

  return v_hold;
end;
$$;

revoke all on function public.release_hospital_bed_hold(uuid, uuid, text, text) from public, anon;
grant execute on function public.release_hospital_bed_hold(uuid, uuid, text, text) to authenticated, service_role;

-- ----------------------------------------------------------------------------
-- Reload PostgREST schema cache
-- ----------------------------------------------------------------------------
notify pgrst, 'reload schema';

COMMIT;
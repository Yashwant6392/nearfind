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

drop policy if exists "Drivers can update own candidate status" on public.driver_request_candidates;
create policy "Drivers can update own candidate status"
  on public.driver_request_candidates for update
  using (auth.uid() = driver_id)
  with check (auth.uid() = driver_id);

drop policy if exists "No client deletes for driver request candidates" on public.driver_request_candidates;
create policy "No client deletes for driver request candidates"
  on public.driver_request_candidates for delete
  using (false);

-- This request model is intentionally separate from the product query lifecycle to keep
-- driver hiring workflows isolated from ordinary provider-response flows.

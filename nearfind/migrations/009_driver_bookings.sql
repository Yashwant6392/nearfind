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

drop policy if exists "No client deletes for bookings" on public.driver_bookings;
create policy "No client deletes for bookings"
  on public.driver_bookings for delete
  using (false);

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

drop policy if exists "No client writes for booking events" on public.driver_booking_events;
create policy "No client writes for booking events"
  on public.driver_booking_events for insert
  with check (false);

drop policy if exists "No client updates for booking events" on public.driver_booking_events;
create policy "No client updates for booking events"
  on public.driver_booking_events for update
  using (false)
  with check (false);

drop policy if exists "No client deletes for booking events" on public.driver_booking_events;
create policy "No client deletes for booking events"
  on public.driver_booking_events for delete
  using (false);

-- The overlap guard uses a half-open [start, end) range so adjacent bookings are allowed while
-- overlapping active bookings are rejected. Acceptance and booking creation remain server-side only.

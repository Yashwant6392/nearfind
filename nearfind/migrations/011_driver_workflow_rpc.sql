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

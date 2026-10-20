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

-- Unique index prevents duplicate active bed holds for the same seeker
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

-- Concurrency-safe atomic bed hold RPC with row-level locking
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

  -- Lock availability row to prevent overselling
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

-- Release/cancel bed hold and restore bed availability
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

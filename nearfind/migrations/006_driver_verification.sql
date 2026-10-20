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

-- Column-level protection for review metadata remains mandatory; the driver and public roles cannot update review fields.
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

drop policy if exists "No client updates for driver reviewers" on public.driver_reviewers;
create policy "No client updates for driver reviewers"
  on public.driver_reviewers for update
  using (false)
  with check (false);

drop policy if exists "No client deletes for driver reviewers" on public.driver_reviewers;
create policy "No client deletes for driver reviewers"
  on public.driver_reviewers for delete
  using (false);

drop policy if exists "Drivers manage own verification submissions" on public.driver_verification_requests;
create policy "Drivers manage own verification submissions"
  on public.driver_verification_requests for insert
  with check (
    auth.uid() = driver_id
    and status in ('submitted', 'needs_resubmission')
    and reviewed_by is null
    and reviewed_at is null
  );

drop policy if exists "Drivers can read own verification submissions" on public.driver_verification_requests;
create policy "Drivers can read own verification submissions"
  on public.driver_verification_requests for select
  using (auth.uid() = driver_id);

drop policy if exists "Drivers can update own verification submissions before review" on public.driver_verification_requests;
create policy "Drivers can update own verification submissions before review"
  on public.driver_verification_requests for update
  using (
    auth.uid() = driver_id
    and status in ('submitted', 'needs_resubmission')
    and reviewed_by is null
    and reviewed_at is null
  )
  with check (
    auth.uid() = driver_id
    and status in ('submitted', 'needs_resubmission')
    and reviewed_by is null
    and reviewed_at is null
  );

drop policy if exists "Reviewers can review verification submissions" on public.driver_verification_requests;
create policy "Reviewers can review verification submissions"
  on public.driver_verification_requests for update
  using (
    exists (
      select 1
      from public.driver_reviewers dr
      where dr.user_id = auth.uid()
    )
    and auth.uid() is distinct from driver_id
  )
  with check (
    exists (
      select 1
      from public.driver_reviewers dr
      where dr.user_id = auth.uid()
    )
    and auth.uid() is distinct from driver_id
    and reviewed_by = auth.uid()
    and reviewed_at is not null
    and status in ('approved', 'rejected', 'under_review')
  );

drop policy if exists "No client deletes for verification submissions" on public.driver_verification_requests;
create policy "No client deletes for verification submissions"
  on public.driver_verification_requests for delete
  using (false);

-- No raw Aadhaar number or driving licence number fields are stored here, and document
-- storage paths stay behind the existing backend/storage authorization layer rather than
-- a public URL.

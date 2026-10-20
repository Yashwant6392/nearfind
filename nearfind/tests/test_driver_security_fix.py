from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_driver_profile_guard_blocks_self_approval_fields():
    sql = (ROOT / "migrations" / "010_driver_profile_self_approval_guard.sql").read_text(encoding="utf-8")

    assert "driver_profiles_self_approval_guard" in sql
    assert "verification_status is distinct from 'pending'" in sql
    assert "driver cannot change verification_status" in sql
    assert "driver cannot change reviewed_by" in sql
    assert "driver cannot change reviewed_at" in sql
    assert "reviewer cannot approve their own driver profile" in sql


def test_verification_request_guard_blocks_approval_and_self_review():
    sql = (ROOT / "migrations" / "010_driver_profile_self_approval_guard.sql").read_text(encoding="utf-8")

    assert "driver_verification_requests_self_approval_guard" in sql
    assert "drivers may not set approval or review states on verification requests" in sql
    assert "drivers may not approve or reject verification requests" in sql
    assert "reviewers may not approve their own verification request" in sql
    assert "reviewed_by must reference a provisioned reviewer" in sql


def test_public_reviewer_self_registration_remains_blocked():
    sql = (ROOT / "migrations" / "006_driver_verification.sql").read_text(encoding="utf-8")
    migration_010 = (ROOT / "migrations" / "010_driver_profile_self_approval_guard.sql").read_text(encoding="utf-8")

    assert 'create policy "Driver reviewers are provisioned out of band"' in sql
    assert 'on public.driver_reviewers for insert' in sql
    assert 'with check (false)' in sql
    assert 'reviewer-controlled' in migration_010
    assert 'out-of-band' in migration_010.lower()


def test_migration_order_is_forward_only_and_files_exist():
    migrations = sorted((ROOT / "migrations").glob("*.sql"))
    names = [p.name for p in migrations]

    assert "005_driver_profiles.sql" in names
    assert "006_driver_verification.sql" in names
    assert "007_driver_availability_locations.sql" in names
    assert "008_driver_requests.sql" in names
    assert "009_driver_bookings.sql" in names
    assert "010_driver_profile_self_approval_guard.sql" in names

    assert names.index("010_driver_profile_self_approval_guard.sql") > names.index("009_driver_bookings.sql")


def test_driver_workflow_migration_limits_workflow_rpcs_to_server_role():
    sql = (ROOT / "migrations" / "011_driver_workflow_rpc.sql").read_text(encoding="utf-8")
    availability = (ROOT / "migrations" / "007_driver_availability_locations.sql").read_text(encoding="utf-8")
    bookings = (ROOT / "migrations" / "009_driver_bookings.sql").read_text(encoding="utf-8")
    verification = (ROOT / "migrations" / "006_driver_verification.sql").read_text(encoding="utf-8")
    booking_constraint = bookings.index("alter table public.driver_bookings\n  drop constraint")
    availability_policy = availability.find('"Matched seeker can read active driver location"')

    for function_name in (
        "save_driver_profile",
        "review_driver_profile",
        "set_driver_presence",
        "refresh_driver_location",
        "create_driver_request",
        "find_drivers_for_request",
        "list_driver_requests_for_driver",
        "accept_driver_request",
    ):
        assert f"revoke all on function public.{function_name}" in sql
        assert f"grant execute on function public.{function_name}" in sql
        assert "to service_role;" in sql
    assert "driver_availability_no_active_overlap" in sql
    assert "driver_locations_review_notes_guard" not in sql
    assert "driver_profiles_review_notes_guard" in sql
    assert "driver_bookings" in sql
    assert availability_policy == -1
    assert booking_constraint > bookings.index("create table if not exists public.driver_bookings")
    assert "revoke insert, update, delete on public.driver_bookings from anon, authenticated" in sql
    assert "revoke insert, update, delete on public.driver_profiles from anon, authenticated" in sql
    assert 'drop policy if exists "Drivers can create own driver profile" on public.driver_profiles' in sql
    assert 'drop policy if exists "Drivers can update own driver profile" on public.driver_profiles' in sql
    assert "and updated_at >= now() - interval '15 minutes'" in sql
    assert "and now() >= b.start_at" in sql
    assert "and now() < b.end_at" in sql
    assert "granted_by uuid references public.users(id) on delete set null" in verification

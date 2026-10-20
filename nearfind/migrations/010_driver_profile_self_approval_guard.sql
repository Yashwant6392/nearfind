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

-- Review grants remain out-of-band and are never exposed through public client routes.
-- Public or self-service reviewer registrations remain blocked by the existing insert policy.

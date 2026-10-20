alter table public.users
  add column if not exists business_name text null;

notify pgrst, 'reload schema';

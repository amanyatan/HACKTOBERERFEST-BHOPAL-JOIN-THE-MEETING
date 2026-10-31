-- Run once in the Supabase dashboard: SQL Editor -> New query -> paste -> Run.
-- Safe to re-run.

create table if not exists public.sites (
  id         uuid primary key default gen_random_uuid(),
  client_id  uuid not null,            -- anonymous per-browser id, no accounts
  site_id    text not null unique,     -- folder name under generated/
  title      text not null default 'Untitled site',
  brief      text not null default '',
  files      jsonb not null,           -- [{path, content}]
  passed     boolean,                  -- null = automated test could not run
  issues     jsonb not null default '[]',
  rounds     int not null default 0,
  created_at timestamptz not null default now()
);
create index if not exists sites_client_idx on public.sites (client_id, created_at desc);

create table if not exists public.deployments (
  id         uuid primary key default gen_random_uuid(),
  client_id  uuid not null,
  site_id    text not null,
  repo_name  text not null,
  visibility text not null check (visibility in ('private', 'public')),
  repo_url   text not null,
  live_url   text not null,
  created_at timestamptz not null default now()
);
create index if not exists deployments_client_idx on public.deployments (client_id, created_at desc);

-- Row-level security ON with no policies: the public anon key can neither read nor write.
-- Only the backend, using the service role key, can touch these tables.
alter table public.sites enable row level security;
alter table public.deployments enable row level security;

-- Make Supabase's API notice the new tables straight away (otherwise it can answer "table not found" for a minute).
notify pgrst, 'reload schema';

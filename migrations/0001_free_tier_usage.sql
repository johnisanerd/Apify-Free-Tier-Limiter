-- Free-tier usage ledger for the Actor fleet.
--
-- One counter row per (user, actor, calendar month). The counter IS the
-- enforcement authority: a point read at run start, an atomic add per flush.
-- No event log on the hot path (see DESIGN.md, v2 roadmap).
--
-- Security model: the anon key rides in every Actor's environment, so it must
-- be assumed public. RLS is on with ZERO policies and direct grants revoked,
-- which means the key cannot touch the table at all. The only reachable
-- surface is the two SECURITY DEFINER functions below.

create table if not exists public.free_tier_usage (
    user_id      text          not null,
    actor_id     text          not null,
    period       text          not null,          -- 'YYYY-MM', UTC
    spent_usd    numeric(12,6) not null default 0,-- never float: this is money
    charge_count integer       not null default 0,
    first_seen   timestamptz   not null default now(),
    updated_at   timestamptz   not null default now(),
    primary key (user_id, actor_id, period)
);

alter table public.free_tier_usage enable row level security;
-- Deliberately no policies. RLS with no policy = deny all for anon.

revoke all on table public.free_tier_usage from anon, authenticated;

-- The period key is computed server-side so a wrong clock in an Actor
-- container can never write into the wrong month, and so the monthly reset
-- needs no cron: a new month is simply a new key.
create or replace function public.current_period()
returns text
language sql
stable
set search_path = public
as $$
    select to_char(now() at time zone 'utc', 'YYYY-MM')
$$;

create or replace function public.get_usage(
    p_user_id  text,
    p_actor_id text
)
returns numeric
language sql
security definer
stable
set search_path = public
as $$
    select coalesce(
        (select spent_usd
           from public.free_tier_usage
          where user_id  = p_user_id
            and actor_id = p_actor_id
            and period   = public.current_period()),
        0
    )::numeric
$$;

-- Atomic check-and-add. ON CONFLICT ... RETURNING takes a row lock, so
-- concurrent runs from the same user serialize here instead of each reading a
-- stale total and overwriting the others. Returns the new authoritative total.
create or replace function public.increment_usage(
    p_user_id  text,
    p_actor_id text,
    p_amount   numeric
)
returns numeric
language plpgsql
security definer
set search_path = public
as $$
declare
    v_total numeric;
begin
    if p_user_id is null or p_user_id = ''
       or p_actor_id is null or p_actor_id = '' then
        raise exception 'user_id and actor_id are required';
    end if;

    -- Blocks clearing your own tab by passing a negative amount.
    if p_amount is null or p_amount < 0 then
        raise exception 'amount must be >= 0';
    end if;

    -- Caps the blast radius if the key is extracted and used to inflate
    -- someone else's counter. No single charge event in this fleet is
    -- anywhere near this large.
    if p_amount > 10 then
        raise exception 'amount exceeds the per-call maximum';
    end if;

    insert into public.free_tier_usage as u
        (user_id, actor_id, period, spent_usd, charge_count)
    values
        (p_user_id, p_actor_id, public.current_period(), p_amount, 1)
    on conflict (user_id, actor_id, period) do update
        set spent_usd    = u.spent_usd + excluded.spent_usd,
            charge_count = u.charge_count + 1,
            updated_at   = now()
    returning u.spent_usd into v_total;

    return v_total;
end
$$;

revoke all on function public.current_period()                      from public;
revoke all on function public.get_usage(text, text)                 from public;
revoke all on function public.increment_usage(text, text, numeric)  from public;

grant execute on function public.get_usage(text, text)                to anon;
grant execute on function public.increment_usage(text, text, numeric) to anon;

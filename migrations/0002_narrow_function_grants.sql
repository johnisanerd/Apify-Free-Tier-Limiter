-- Applied 2026-08-11. Supabase's default privileges grant EXECUTE on new public
-- functions to anon, authenticated and service_role, so the role-specific grants
-- survived 0001's `revoke ... from public`. Actors only ever use the anon key.
--
-- Clears two of the four Security Advisor warnings. The remaining two (anon can
-- execute the two SECURITY DEFINER functions) are the access model itself.

revoke all on function public.get_usage(text, text)                from authenticated;
revoke all on function public.increment_usage(text, text, numeric) from authenticated;
revoke all on function public.current_period()                     from anon, authenticated;

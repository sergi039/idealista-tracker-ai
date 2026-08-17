-- Subscriptions the owner has taken off the screen (owner request, 2026-08-17).
-- PostgreSQL-only migration, like every migration in this directory.
--
-- `is_active` already retires a saved search: it leaves the chips on
-- /properties and moves into the *Archive* section of the subscription menu,
-- one tick away, because its listings are real and have to stay reachable.
-- That is the right answer for a search that stopped. It is not an answer for
-- a search that is still running and that the owner simply does not want to
-- look at -- of the fourteen subscriptions in production, three were created
-- by the ingester and hold one listing each, and every one of them takes a
-- chip on the one working page.
--
-- So `is_hidden` is about the screen and nothing else:
--
--   * ingestion is untouched. A hidden subscription still receives its own
--     alert emails and still matches its own `email_matchers` -- routing the
--     mail elsewhere would put those listings in the catch-all, which is a
--     data change wearing a UI change's clothes;
--   * nothing is deleted, and nothing about the listings changes. They keep
--     their subscription, their scores and their comparable pool;
--   * the rows stay reachable. /profiles lists every subscription including
--     the hidden ones, and a hidden id named explicitly in `profile_id=<id>`
--     still renders, with its own checkbox in the menu -- the selection has to
--     survive a round trip or the next Apply silently widens the page.
--
-- FALSE for every existing row. Hiding is a choice somebody makes, and there
-- is nothing in the table from which it could be inferred.

ALTER TABLE search_profiles
    ADD COLUMN IF NOT EXISTS is_hidden BOOLEAN NOT NULL DEFAULT FALSE;

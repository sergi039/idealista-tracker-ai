-- Saved-search identity: key a subscription by its search URL (issue #102).
-- PostgreSQL-only migration, like every migration in this directory.
--
-- source_search_key is the fingerprint of the search URL the alert email
-- carries (idealista:v1:<sha256>, 77 characters); source_search_url keeps the
-- raw link for diagnostics and is deliberately NOT unique, because cosmetic
-- variants of one link share a key but differ as text.
--
-- Nothing is backfilled: no stored row records which saved search it came
-- from, so the key stays NULL until the next email for that subscription
-- arrives and binds it.

ALTER TABLE search_profiles
    ADD COLUMN IF NOT EXISTS source_search_key VARCHAR(77);

ALTER TABLE search_profiles
    ADD COLUMN IF NOT EXISTS source_search_url TEXT;

-- "The ingester invented this label", so a label the owner chose is never
-- rewritten by an incoming email. Existing rows keep the default FALSE: the
-- only evidence about them is a description string, and a string match is
-- exactly the signal this issue refuses to trust.
ALTER TABLE search_profiles
    ADD COLUMN IF NOT EXISTS is_auto_created BOOLEAN NOT NULL DEFAULT FALSE;

CREATE UNIQUE INDEX IF NOT EXISTS ux_search_profiles_source_search_key
    ON search_profiles (source_search_key);

-- Drop the UNIQUE constraint on the *label*. Two different subscriptions may
-- legitimately carry the same name with a different `shape`, which the
-- constraint made impossible to represent. The plain lookup index
-- ix_search_profiles_name (migration 008) stays and keeps the column indexed.
--
-- The constraint is found by shape rather than by its conventional name, so a
-- database whose constraint was created under a different name is handled too.
--
-- quote_ident() rather than format(): the runner hands this SQL straight to
-- psycopg2, which reads a percent sign as its own parameter marker and fails
-- before PostgreSQL ever sees the statement. A percent sign anywhere in a
-- migration file must be doubled; test_deployment_bootstrap.py enforces that.
DO $$
DECLARE
    unique_label_constraint TEXT;
BEGIN
    SELECT constraint_info.conname
        INTO unique_label_constraint
        FROM pg_constraint AS constraint_info
        WHERE constraint_info.conrelid = 'search_profiles'::regclass
          AND constraint_info.contype = 'u'
          AND constraint_info.conkey = ARRAY[
              (
                  SELECT attribute_info.attnum
                  FROM pg_attribute AS attribute_info
                  WHERE attribute_info.attrelid = 'search_profiles'::regclass
                    AND attribute_info.attname = 'name'
              )
          ]::smallint[];

    IF unique_label_constraint IS NOT NULL THEN
        EXECUTE 'ALTER TABLE search_profiles DROP CONSTRAINT '
            || quote_ident(unique_label_constraint);
    END IF;
END $$;

-- That constraint was also the only thing protecting the check-then-insert in
-- get_or_create_profile_by_name() and get_default_profile(): gunicorn serves
-- four threads and a manual ingest overlaps the scheduled one, so two
-- identical profiles could now be committed side by side. Restore exactly the
-- old invariant for rows that are not yet identified, while leaving two
-- *different* subscriptions free to share a label.
--
-- Created after the DROP above, and it fails loudly if duplicates somehow
-- already exist rather than silently skipping the protection.
CREATE UNIQUE INDEX IF NOT EXISTS ux_search_profiles_name_without_key
    ON search_profiles (name)
    WHERE source_search_key IS NULL;

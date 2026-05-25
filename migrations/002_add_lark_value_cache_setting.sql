-- Seeds the Layer-1 kill switch for the in-memory Lark record-value cache
-- (see CHANGELOG entry on 2026-05-25). Default is enabled; flip to 'false'
-- via dashboard or directly here to disable cache reads without a redeploy.
--
-- Run once against the production Supabase project (SQL editor or psql).
-- Idempotent: safe to re-run.

INSERT INTO settings (key, value)
VALUES ('lark_value_cache_enabled', 'true')
ON CONFLICT (key) DO NOTHING;

-- Adds the issue-type column to sync_history so the dashboard's Type
-- column can render Epic / Story / Task instead of "—".
--
-- Run once against the production Supabase project (SQL editor or psql).
-- Idempotent: safe to re-run.

ALTER TABLE sync_history ADD COLUMN IF NOT EXISTS type text;

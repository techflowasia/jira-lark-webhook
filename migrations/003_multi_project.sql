-- Multi-project sync: project registry + per-project scoping columns.
-- Schema only. Project #1 is seeded at startup by projects.bootstrap_default()
-- because its values live in env vars / the settings table.
--
-- Run once against the production Supabase project (SQL editor or psql).
-- Idempotent: safe to re-run.

create table if not exists sync_projects (
    id                 bigint generated always as identity primary key,
    name               text        not null,
    jira_project_key   text        not null,
    jira_board_id      text,
    lark_base_token    text        not null,
    lark_table_id      text        not null,
    lark_table_name    text        not null default '',
    sync_enabled       boolean     not null default false,
    reconcile_enabled  boolean     not null default false,
    allowed_jira_types jsonb       not null default '[]'::jsonb,
    allowed_lark_types jsonb       not null default '[]'::jsonb,
    assignee_map       jsonb       not null default '{}'::jsonb,
    setup_state        text        not null default 'draft',
    created_at         timestamptz default now(),
    unique (jira_project_key),
    unique (lark_base_token, lark_table_id)
);

alter table field_mappings
    add column if not exists project_id bigint references sync_projects(id);

alter table sync_history
    add column if not exists project_id bigint;

create index if not exists sync_history_project_ts_idx
    on sync_history (project_id, ts desc);

-- Per-snapshot DML activity per table (trend version of top_20_tables_by_dmls.sql)
-- Keeps per-snapshot rows instead of aggregating to SUM across the window
WITH pg_stat_all_tables_vw AS (
    SELECT a.snap_id, b.sample_start_time, a.schemaname, a.relname,
           a.n_tup_ins, a.n_tup_upd, a.n_tup_del, a.n_tup_hot_upd,
           a.n_live_tup, a.n_dead_tup
    FROM pg_stat_all_tables_history a
    JOIN pg_awr_snapshots_cust b ON a.snap_id = b.snap_id
    WHERE a.snap_id BETWEEN :begin_snap_id AND :end_snap_id
      AND a.schemaname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
),
deltas AS (
    SELECT snap_id, sample_start_time, schemaname, relname,
        CASE WHEN (n_tup_ins - LAG(n_tup_ins) OVER w) = n_tup_ins THEN NULL
             ELSE (n_tup_ins - LAG(n_tup_ins) OVER w) END AS delta_ins,
        CASE WHEN (n_tup_upd - LAG(n_tup_upd) OVER w) = n_tup_upd THEN NULL
             ELSE (n_tup_upd - LAG(n_tup_upd) OVER w) END AS delta_upd,
        CASE WHEN (n_tup_del - LAG(n_tup_del) OVER w) = n_tup_del THEN NULL
             ELSE (n_tup_del - LAG(n_tup_del) OVER w) END AS delta_del,
        CASE WHEN (n_tup_hot_upd - LAG(n_tup_hot_upd) OVER w) = n_tup_hot_upd THEN NULL
             ELSE (n_tup_hot_upd - LAG(n_tup_hot_upd) OVER w) END AS delta_hot_upd,
        n_live_tup, n_dead_tup
    FROM pg_stat_all_tables_vw
    WINDOW w AS (PARTITION BY schemaname, relname ORDER BY snap_id)
)
SELECT snap_id, sample_start_time, schemaname, relname,
       delta_ins, delta_upd, delta_del, delta_hot_upd, n_live_tup, n_dead_tup
FROM deltas
WHERE delta_ins IS NOT NULL
ORDER BY snap_id, (COALESCE(delta_ins,0) + COALESCE(delta_upd,0) + COALESCE(delta_del,0)) DESC;

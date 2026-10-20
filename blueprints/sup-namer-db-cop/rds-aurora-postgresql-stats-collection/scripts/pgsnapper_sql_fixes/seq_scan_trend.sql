-- Per-snapshot sequential scan trend per table (trend version of top_20_tables_by_seq_scans.sql)
-- Keeps per-snapshot rows instead of aggregating to SUM across the window
WITH pg_stat_all_tables_vw AS (
    SELECT a.snap_id, b.sample_start_time, a.schemaname, a.relname,
           a.seq_scan, a.seq_tup_read, a.idx_scan, a.n_live_tup, a.n_dead_tup
    FROM pg_stat_all_tables_history a
    JOIN pg_awr_snapshots_cust b ON a.snap_id = b.snap_id
    WHERE a.snap_id BETWEEN :begin_snap_id AND :end_snap_id
      AND a.schemaname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
),
deltas AS (
    SELECT snap_id, sample_start_time, schemaname, relname,
        CASE WHEN (seq_scan - LAG(seq_scan) OVER w) = seq_scan THEN NULL
             ELSE (seq_scan - LAG(seq_scan) OVER w) END AS delta_seq_scan,
        CASE WHEN (seq_tup_read - LAG(seq_tup_read) OVER w) = seq_tup_read THEN NULL
             ELSE (seq_tup_read - LAG(seq_tup_read) OVER w) END AS delta_seq_tup_read,
        CASE WHEN (idx_scan - LAG(idx_scan) OVER w) = idx_scan THEN NULL
             ELSE (idx_scan - LAG(idx_scan) OVER w) END AS delta_idx_scan,
        n_live_tup, n_dead_tup
    FROM pg_stat_all_tables_vw
    WINDOW w AS (PARTITION BY schemaname, relname ORDER BY snap_id)
)
SELECT snap_id, sample_start_time, schemaname, relname,
       delta_seq_scan, delta_seq_tup_read, delta_idx_scan, n_live_tup, n_dead_tup
FROM deltas
WHERE delta_seq_scan IS NOT NULL AND delta_seq_scan > 0
ORDER BY snap_id, delta_seq_scan DESC;

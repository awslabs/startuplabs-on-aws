-- Per-snapshot database activity deltas (trend version of db_stats.sql)
-- Keeps per-snapshot rows instead of aggregating to AVG across the window
WITH pg_stat_database_vw AS (
    SELECT a.snap_id, sample_start_time, datname, numbackends,
           xact_commit, xact_rollback, blks_read, blks_hit,
           tup_inserted, tup_updated, tup_deleted,
           temp_files, deadlocks
    FROM pg_stat_database_history a
    JOIN pg_awr_snapshots_cust b ON a.snap_id = b.snap_id
    WHERE a.snap_id BETWEEN :begin_snap_id AND :end_snap_id
      AND datname NOT IN ('rdsadmin', 'template0', 'template1')
),
deltas AS (
    SELECT snap_id, sample_start_time, datname, numbackends,
        (xact_commit - LAG(xact_commit) OVER w) AS delta_commits,
        (xact_rollback - LAG(xact_rollback) OVER w) AS delta_rollbacks,
        (blks_read - LAG(blks_read) OVER w) AS delta_blks_read,
        (blks_hit - LAG(blks_hit) OVER w) AS delta_blks_hit,
        (tup_inserted - LAG(tup_inserted) OVER w) AS delta_inserts,
        (tup_updated - LAG(tup_updated) OVER w) AS delta_updates,
        (tup_deleted - LAG(tup_deleted) OVER w) AS delta_deletes,
        (temp_files - LAG(temp_files) OVER w) AS delta_temp_files,
        (deadlocks - LAG(deadlocks) OVER w) AS delta_deadlocks
    FROM pg_stat_database_vw
    WINDOW w AS (PARTITION BY datname ORDER BY snap_id)
)
SELECT snap_id, sample_start_time, datname, numbackends,
       delta_commits, delta_rollbacks, delta_blks_read, delta_blks_hit,
       delta_inserts, delta_updates, delta_deletes, delta_temp_files, delta_deadlocks
FROM deltas
WHERE delta_commits IS NOT NULL
ORDER BY datname, snap_id;

-- Top 20 SQL by total CPU across the snapshot window (aggregated version)
-- Replaces per-snapshot top_10_sqls_by_cpu_by_snap_id_v2.sql which returns N*10 rows
WITH cpu_deltas AS (
    SELECT a.snap_id, b.sample_start_time, a.dbid, a.userid, a.queryid,
        (a.total_exec_time - LAG(a.total_exec_time) OVER w) AS cpu_delta,
        (a.calls - LAG(a.calls) OVER w) AS calls_delta
    FROM pg_stat_statements_history a
    JOIN pg_awr_snapshots_cust b ON a.snap_id = b.snap_id
    WHERE a.snap_id BETWEEN :begin_snap_id AND :end_snap_id
    WINDOW w AS (PARTITION BY a.dbid, a.userid, a.queryid ORDER BY a.snap_id)
)
SELECT dbid, userid, queryid,
    COALESCE((SELECT query FROM pg_stat_statements_history h WHERE h.dbid = d.dbid AND h.userid = d.userid AND h.queryid = d.queryid LIMIT 1), '') AS query,
    ROUND(SUM(cpu_delta)::numeric, 2) AS total_cpu_time,
    SUM(calls_delta) AS total_calls,
    ROUND((SUM(cpu_delta) / NULLIF(SUM(calls_delta), 0))::numeric, 2) AS avg_cpu_per_call,
    MIN(sample_start_time) AS first_seen,
    MAX(sample_start_time) AS last_seen,
    (array_agg(sample_start_time ORDER BY cpu_delta DESC))[1] AS peak_time,
    ROUND(MAX(cpu_delta)::numeric, 2) AS peak_cpu_in_interval
FROM cpu_deltas d
WHERE cpu_delta > 0
GROUP BY dbid, userid, queryid
ORDER BY SUM(cpu_delta) DESC
LIMIT 20;

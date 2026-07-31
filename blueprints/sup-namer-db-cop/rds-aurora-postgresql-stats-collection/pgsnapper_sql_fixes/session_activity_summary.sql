-- Per-snapshot session activity summary (replaces raw session_activity_hist.sql)
-- Aggregates session counts by wait event type per snapshot for trend visualization
SELECT 
    a.snap_id,
    b.sample_start_time,
    COUNT(*) as total_sessions,
    COUNT(*) FILTER (WHERE wait_event_type IS NULL) as running_on_cpu,
    COUNT(*) FILTER (WHERE wait_event_type = 'Client') as client_wait,
    COUNT(*) FILTER (WHERE wait_event_type = 'IO') as io_wait,
    COUNT(*) FILTER (WHERE wait_event_type = 'Lock') as lock_wait,
    COUNT(*) FILTER (WHERE wait_event_type = 'LWLock') as lwlock_wait,
    COUNT(*) FILTER (WHERE wait_event_type = 'BufferPin') as bufferpin_wait,
    COUNT(*) FILTER (WHERE wait_event_type = 'Activity') as activity_wait,
    COUNT(*) FILTER (WHERE wait_event_type NOT IN ('Client','IO','Lock','LWLock','BufferPin','Activity') AND wait_event_type IS NOT NULL) as other_wait,
    COUNT(DISTINCT application_name) as distinct_apps
FROM pg_stat_activity_history a
JOIN pg_awr_snapshots_cust b ON a.snap_id = b.snap_id
WHERE a.snap_id BETWEEN :begin_snap_id AND :end_snap_id
  AND usename != 'rdsadmin'
  AND query NOT ILIKE '%pg_stat_activity%'
GROUP BY a.snap_id, b.sample_start_time
ORDER BY a.snap_id;

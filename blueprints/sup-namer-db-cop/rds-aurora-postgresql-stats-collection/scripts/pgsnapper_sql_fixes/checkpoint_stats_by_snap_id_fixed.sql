-- Checkpoint/bgwriter stats per snapshot (PG17-compatible)
-- PG17 moved checkpoint columns to pg_stat_checkpointer (not captured by PGSnapper).
-- This fixed version uses the columns available in pg_stat_bgwriter_history on PG17:
--   buffers_clean, maxwritten_clean, buffers_alloc
-- On PG < 17, the original checkpoint_stats_by_snap_id.sql works (has all columns).

select a.snap_id, sample_start_time,
case WHEN (buffers_clean-lag(buffers_clean::numeric,1,0::numeric) OVER (ORDER BY a.snap_id) ) = buffers_clean then null
else (buffers_clean-lag(buffers_clean::numeric,1,0::numeric) OVER (ORDER BY a.snap_id) ) END AS delta_buffers_bgwriter,
case WHEN (maxwritten_clean-lag(maxwritten_clean::numeric,1,0::numeric) OVER (ORDER BY a.snap_id) ) = maxwritten_clean then null
else (maxwritten_clean-lag(maxwritten_clean::numeric,1,0::numeric) OVER (ORDER BY a.snap_id) ) END AS delta_maxwritten_clean,
case WHEN (buffers_alloc-lag(buffers_alloc::numeric,1,0::numeric) OVER (ORDER BY a.snap_id) ) = buffers_alloc then null
else (buffers_alloc-lag(buffers_alloc::numeric,1,0::numeric) OVER (ORDER BY a.snap_id) ) END AS delta_new_buffers_alloc
from pg_stat_bgwriter_history a, pg_awr_snapshots_cust b
where a.snap_id = b.snap_id
and a.snap_id between :begin_snap_id and :end_snap_id;

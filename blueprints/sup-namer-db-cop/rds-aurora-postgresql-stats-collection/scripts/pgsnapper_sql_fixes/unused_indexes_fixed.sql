-- Unused indexes: exclude system catalog schemas
-- PGSnapper temporal version: indexes with zero scans across the snapshot window
SELECT
    s.relid,
    s.indexrelid,
    s.schemaname,
    s.relname AS tablename,
    s.indexrelname AS indexname,
    s.idx_scan,
    CASE WHEN i.indisunique THEN 't' ELSE 'f' END AS is_unique
FROM pg_stat_all_indexes s
JOIN pg_index i ON s.indexrelid = i.indexrelid
WHERE s.idx_scan = 0
    AND s.schemaname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
    AND s.indexrelname NOT LIKE 'pg_toast_%'
ORDER BY pg_relation_size(s.indexrelid) DESC;

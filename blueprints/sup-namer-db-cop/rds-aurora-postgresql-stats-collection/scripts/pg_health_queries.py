"""SQL queries for collect_pg_health_insights().

All queries are tested and validated against Aurora PostgreSQL 17.4 and RDS PostgreSQL 15.16.
See design/pg_health_sql_queries.sql for standalone psql versions.
See design/data_collection_optimization_plan_v2.md Section 11f for column mappings.
"""

# ============================================================================
# Section 1: DATABASE OVERVIEW
# ============================================================================

OVERVIEW_SERVER_INFO = """
WITH RECURSIVE
database_stats AS (
    SELECT
        version() as full_version,
        current_setting('server_version') as version,
        current_setting('server_version_num')::integer as version_num,
        current_database() as current_db,
        pg_database_size(current_database()) as db_size,
        current_setting('max_connections') as max_connections,
        pg_postmaster_start_time() as start_time,
        pg_is_in_recovery() as is_replica,
        EXTRACT(EPOCH FROM (now() - pg_postmaster_start_time())) as uptime_seconds
),
activity_stats AS (
    SELECT
        COUNT(*) as total_connections,
        COUNT(*) FILTER (WHERE state = 'active') as active_connections,
        COUNT(*) FILTER (WHERE state = 'idle') as idle_connections,
        COUNT(*) FILTER (WHERE state = 'idle in transaction') as idle_in_trans,
        COUNT(DISTINCT usename) as unique_users,
        MAX(EXTRACT(EPOCH FROM (now() - backend_start)))::integer as oldest_connection_seconds
    FROM pg_stat_activity
    WHERE pid != pg_backend_pid()
),
encoding_info AS (
    SELECT
        current_setting('client_encoding') as client_encoding,
        current_setting('server_encoding') as server_encoding,
        current_setting('timezone') as timezone
),
extension_stats AS (
    SELECT
        COUNT(*) as total_extensions,
        string_agg(extname || ' (' || extversion || ')', ', ' ORDER BY extname) as extension_versions,
        COUNT(*) FILTER (WHERE extname = 'pg_stat_statements') > 0 as has_pg_stat_statements
    FROM pg_extension
)
SELECT metric, value, description, recommendation
FROM (
    SELECT 'PostgreSQL Version' as metric, full_version as value,
        'Complete PostgreSQL version information' as description,
        CASE WHEN version_num < 130000 THEN 'Consider upgrading to latest stable version' ELSE 'OK' END as recommendation
    FROM database_stats
    UNION ALL
    SELECT 'Server Uptime (seconds)', uptime_seconds::text,
        format('Up since: %s', start_time::timestamp(0)),
        CASE WHEN uptime_seconds < 300 THEN 'WARNING: Recent server restart'
             WHEN uptime_seconds < 86400 THEN 'Server restarted within last 24 hours'
             ELSE 'OK' END
    FROM database_stats
    UNION ALL
    SELECT 'Server Encoding', server_encoding, 'Database server character encoding',
        CASE WHEN server_encoding != 'UTF8' THEN 'Consider using UTF8' ELSE 'OK' END
    FROM encoding_info
    UNION ALL
    SELECT 'pg_stat_statements',
        CASE WHEN has_pg_stat_statements THEN 'Installed' ELSE 'Not Installed' END,
        'Query performance analysis extension',
        CASE WHEN NOT has_pg_stat_statements THEN 'Consider installing for query performance monitoring' ELSE 'OK' END
    FROM extension_stats
    UNION ALL
    SELECT 'Extension Inventory', extension_versions, 'Installed extensions with versions', 'OK'
    FROM extension_stats
) metrics
ORDER BY CASE WHEN recommendation LIKE 'WARNING%' THEN 1 WHEN recommendation LIKE 'Consider%' THEN 2 ELSE 3 END;
"""

OVERVIEW_EXTENSION_INVENTORY = """
SELECT extname, extversion FROM pg_extension ORDER BY extname;
"""

# ============================================================================
# Section 2: SYSTEM CONFIGURATION (Environment Setup)
# ============================================================================

CONFIG_PARAMETER_HEALTH = """
WITH deployment_context AS (
    SELECT '{deployment_type}'::text as deployment_type
),
recommended_settings AS (
    SELECT 'shared_buffers' as name, 'Aurora-managed via instance class - verify sizing' as recommendation,
        'Medium' as priority, 'Memory' as category, 'Aurora' as applicable_to,
        'Auto-managed but visible - verify adequate for workload' as description
    UNION ALL SELECT 'work_mem', 'Start at 4MB, tune for complex analytical queries',
        'High', 'Memory', 'All', 'Tunable - same guidance as standard PostgreSQL'
    UNION ALL SELECT 'maintenance_work_mem', 'Increase for large table maintenance',
        'High', 'Memory', 'All', 'Tunable - important for vacuum on large tables'
    UNION ALL SELECT 'max_connections', 'Instance-class scaled - use Aurora connection pooling',
        'High', 'Connection', 'All', 'Tunable but prefer built-in connection management'
    UNION ALL SELECT 'autovacuum', 'Must be on - Aurora storage still needs tuple cleanup',
        'Critical', 'Vacuum', 'All', 'Tunable - dead tuples still impact query performance'
    UNION ALL SELECT 'autovacuum_vacuum_scale_factor', '0.1 for large tables',
        'High', 'Vacuum', 'All', 'Tunable - Aurora storage does not auto-reclaim dead tuples'
    UNION ALL SELECT 'rds.force_ssl', 'Set to 1 to enforce encrypted connections',
        'Critical', 'Security', 'All', 'Enforce SSL on all Aurora connections'
    UNION ALL SELECT 'random_page_cost', '1.1 (Aurora uses distributed SSD storage)',
        'Medium', 'Query Planning', 'All', 'Tunable - Aurora storage is SSD-based'
    UNION ALL SELECT 'effective_io_concurrency', '200 (Aurora has high I/O throughput)',
        'Medium', 'Query Planning', 'All', 'Tunable - Aurora storage handles high concurrency'
    UNION ALL SELECT 'max_parallel_workers_per_gather', 'Instance vCPU based',
        'Medium', 'Parallel Query', 'All', 'Tunable - leverage Aurora read replicas for scaling'
    UNION ALL SELECT 'shared_preload_libraries', 'pg_stat_statements,auto_explain',
        'Critical', 'Extensions', 'All', 'Tunable via cluster parameter group'
    UNION ALL SELECT 'log_min_duration_statement', '1000ms for Performance Insights',
        'High', 'Logging', 'All', 'Tunable - integrates with Aurora Performance Insights'
    UNION ALL SELECT 'track_io_timing', 'Enable for Aurora monitoring integration',
        'High', 'Logging', 'All', 'Tunable - enhances CloudWatch and PI metrics'
    UNION ALL SELECT 'statement_timeout', 'Set based on application SLAs',
        'Critical', 'Statement', 'All', 'Tunable - prevents runaway queries on shared cluster'
    UNION ALL SELECT 'idle_in_transaction_session_timeout', '7200000 recommended',
        'Critical', 'Statement', 'All', 'Tunable - critical for Aurora connection management'
    UNION ALL SELECT 'aurora_lab_mode', 'Enable for experimental Aurora features',
        'Low', 'Aurora Specific', 'All', 'Access preview features - not for production'
    UNION ALL SELECT 'password_encryption', 'scram-sha-256 recommended',
        'Critical', 'Security', 'All', 'Tunable - use strongest available method'
)
SELECT
    dc.deployment_type as detected_deployment,
    rs.name as parameter_name,
    COALESCE(ps.setting, 'N/A') as current_value,
    CASE
        WHEN ps.name IN ('shared_buffers', 'work_mem', 'maintenance_work_mem', 'effective_cache_size', 'temp_file_limit', 'max_wal_size', 'min_wal_size')
        THEN COALESCE(pg_size_pretty(
            ps.setting::bigint *
            CASE WHEN ps.unit = '8kB' THEN 8192 WHEN ps.unit = 'kB' THEN 1024 WHEN ps.unit = 'MB' THEN 1048576 WHEN ps.unit = 'GB' THEN 1073741824 ELSE 1 END
        ), ps.setting || COALESCE(' ' || ps.unit, ''))
        ELSE COALESCE(ps.setting || COALESCE(' ' || NULLIF(ps.unit, ''), ''), 'N/A')
    END as readable_value,
    COALESCE(ps.boot_val, '-') as default_value,
    rs.recommendation,
    rs.priority as tuning_priority,
    rs.category as parameter_type,
    rs.description,
    COALESCE(ps.context, '-') as change_requires,
    CASE
        WHEN ps.setting IS NULL THEN 'N/A - Parameter not found'
        WHEN ps.name = 'autovacuum' AND ps.setting = 'off' THEN 'CRITICAL: Autovacuum disabled - enable immediately'
        WHEN ps.name = 'rds.force_ssl' AND ps.setting = '0' THEN 'CRITICAL: SSL not enforced'
        WHEN ps.name = 'random_page_cost' AND ps.setting::numeric > 2.0 THEN 'WARNING: Too high for SSD - set to 1.1'
        WHEN ps.name = 'shared_preload_libraries' AND ps.setting NOT LIKE '%pg_stat_statements%' THEN 'WARNING: pg_stat_statements not loaded'
        WHEN ps.name = 'password_encryption' AND ps.setting = 'md5' THEN 'WARNING: Upgrade to scram-sha-256'
        WHEN ps.name = 'track_io_timing' AND ps.setting = 'off' THEN 'INFO: Enable for I/O monitoring'
        ELSE 'OK'
    END as health_status
FROM recommended_settings rs
CROSS JOIN deployment_context dc
LEFT JOIN pg_settings ps ON ps.name = rs.name
WHERE rs.applicable_to IN (dc.deployment_type, 'All')
ORDER BY
    CASE rs.priority WHEN 'Critical' THEN 1 WHEN 'High' THEN 2 WHEN 'Medium' THEN 3 WHEN 'Low' THEN 4 WHEN 'Info' THEN 5 END,
    rs.category, rs.name;
"""

CONFIG_USER_ROLES = """
SELECT
    rolname AS role_name,
    CASE WHEN rolsuper THEN 'YES' ELSE 'no' END AS is_superuser,
    CASE WHEN rolcreatedb THEN 'YES' ELSE 'no' END AS can_create_db,
    CASE WHEN rolcreaterole THEN 'YES' ELSE 'no' END AS can_create_role,
    CASE WHEN rolcanlogin THEN 'YES' ELSE 'no' END AS can_login,
    CASE WHEN rolreplication THEN 'YES' ELSE 'no' END AS replication,
    CASE WHEN rolbypassrls THEN 'YES' ELSE 'no' END AS bypass_rls,
    rolconnlimit AS connection_limit,
    CASE
        WHEN rolname = 'rdsadmin' THEN 'AWS-MANAGED - RDS internal service account'
        WHEN rolname = 'rds_superuser' THEN 'AWS-MANAGED - RDS admin role'
        WHEN rolname LIKE 'rds%' THEN 'AWS-MANAGED - RDS/Aurora system role'
        WHEN rolsuper THEN 'CRITICAL - Superuser has unrestricted access'
        WHEN rolcreaterole AND rolcreatedb THEN 'HIGH - Can create roles and databases'
        WHEN rolbypassrls THEN 'HIGH - Can bypass row-level security'
        ELSE 'OK'
    END AS risk_level
FROM pg_roles
WHERE rolname NOT LIKE 'pg_%'
ORDER BY
    CASE WHEN rolsuper THEN 1 WHEN rolcreaterole AND rolcreatedb THEN 2 WHEN rolbypassrls THEN 4 ELSE 7 END,
    rolname
LIMIT 200;
"""

CONFIG_PRIVILEGE_AUDIT = """
WITH table_privs AS (
    SELECT
        grantee,
        table_schema,
        table_name,
        privilege_type,
        is_grantable
    FROM information_schema.table_privileges
    WHERE grantee NOT IN (SELECT rolname FROM pg_roles WHERE rolname LIKE 'pg_%')
      AND grantee NOT LIKE 'rds%'
),
ranked_tables AS (
    SELECT
        grantee,
        table_schema,
        table_name,
        ROW_NUMBER() OVER (PARTITION BY grantee, table_schema ORDER BY table_name) as rn
    FROM (SELECT DISTINCT grantee, table_schema, table_name FROM table_privs) dt
),
privilege_summary AS (
    SELECT
        tp.grantee,
        tp.table_schema,
        COUNT(DISTINCT tp.table_name) as table_count,
        (SELECT string_agg(rt.table_name, ', ' ORDER BY rt.table_name)
         FROM ranked_tables rt
         WHERE rt.grantee = tp.grantee AND rt.table_schema = tp.table_schema AND rt.rn <= 200
        ) as table_list,
        string_agg(DISTINCT tp.privilege_type, ', ' ORDER BY tp.privilege_type) as privileges,
        COUNT(*) as total_grants,
        COUNT(*) FILTER (WHERE tp.is_grantable = 'YES') as grantable_count,
        bool_or(tp.is_grantable = 'YES') as has_grantable
    FROM table_privs tp
    GROUP BY tp.grantee, tp.table_schema
)
SELECT
    grantee,
    table_schema as schema,
    table_count::text || ' tables' as scope,
    CASE
        WHEN table_count <= 200 THEN table_list
        ELSE table_list || ' ... +' || (table_count - 200)::text || ' more'
    END as tables,
    privileges,
    total_grants::text || ' grants' || CASE WHEN grantable_count > 0 THEN ' (' || grantable_count || ' grantable)' ELSE '' END as grant_summary,
    CASE
        WHEN grantee = 'PUBLIC' THEN 'WARNING - PUBLIC role has grants on ' || table_count || ' tables in ' || table_schema
        WHEN has_grantable AND privileges LIKE '%INSERT%' AND privileges LIKE '%DELETE%' THEN 'HIGH - Full DML with grant option on ' || table_count || ' tables'
        WHEN privileges LIKE '%INSERT%' AND privileges LIKE '%UPDATE%' AND privileges LIKE '%DELETE%' THEN 'MEDIUM - Full DML access on ' || table_count || ' tables'
        WHEN has_grantable THEN 'INFO - Has grant option (can delegate privileges)'
        WHEN table_count > 50 THEN 'INFO - Broad access across ' || table_count || ' tables'
        ELSE 'OK'
    END as risk_level
FROM privilege_summary
ORDER BY
    CASE
        WHEN grantee = 'PUBLIC' THEN 0
        WHEN has_grantable AND privileges LIKE '%INSERT%' AND privileges LIKE '%DELETE%' THEN 1
        WHEN privileges LIKE '%INSERT%' AND privileges LIKE '%UPDATE%' AND privileges LIKE '%DELETE%' THEN 2
        WHEN has_grantable THEN 3
        ELSE 4
    END,
    grantee,
    table_schema
LIMIT 50;
"""

CONFIG_SSL_CONNECTIONS = """
WITH ssl_summary AS (
    SELECT
        sa.usename, sa.datname, ss.ssl as is_ssl,
        COALESCE(ss.version, 'N/A') as ssl_version,
        COALESCE(ss.cipher, 'N/A') as ssl_cipher,
        COALESCE(ss.bits, 0) as cipher_bits,
        sa.client_addr IS NULL as is_local,
        COUNT(*) as connection_count
    FROM pg_stat_ssl ss
    JOIN pg_stat_activity sa ON ss.pid = sa.pid
    WHERE sa.pid IS NOT NULL
    GROUP BY sa.usename, sa.datname, ss.ssl, ss.version, ss.cipher, ss.bits, (sa.client_addr IS NULL)
)
SELECT
    usename as username, datname as database, connection_count as connections,
    CASE WHEN is_ssl THEN 'YES' ELSE 'NO' END as ssl_enabled,
    ssl_version, ssl_cipher, cipher_bits::text as bits,
    CASE WHEN is_local THEN 'Local' ELSE 'Remote' END as conn_type,
    CASE
        WHEN NOT is_ssl AND NOT is_local THEN 'WARNING - Unencrypted remote connection(s)'
        WHEN NOT is_ssl AND is_local THEN 'OK - Local connection(s)'
        WHEN is_ssl AND cipher_bits < 128 THEN 'WARNING - Weak cipher'
        WHEN is_ssl THEN 'OK - Encrypted (' || ssl_version || ')'
        ELSE 'UNKNOWN'
    END as security_status
FROM ssl_summary
ORDER BY CASE WHEN NOT is_ssl AND NOT is_local THEN 0 ELSE 3 END, connection_count DESC
LIMIT 50;
"""

CONFIG_PASSWORD_SECURITY = """
SELECT
    rolname AS role_name,
    CASE WHEN rolcanlogin THEN 'YES' ELSE 'no' END AS can_login,
    'N/A (requires superuser to inspect pg_authid)' AS password_method,
    CASE
        WHEN rolvaliduntil IS NULL THEN 'Never expires'
        WHEN rolvaliduntil < now() THEN 'EXPIRED on ' || rolvaliduntil::date::text
        WHEN rolvaliduntil < now() + interval '30 days' THEN 'Expires soon: ' || rolvaliduntil::date::text
        ELSE rolvaliduntil::date::text
    END AS password_expiry,
    CASE
        WHEN rolvaliduntil IS NOT NULL AND rolvaliduntil < now() THEN 'WARNING - Password expired'
        ELSE 'Limited check - connect as superuser for full audit'
    END AS security_status
FROM pg_roles
WHERE rolname NOT LIKE 'pg_%'
ORDER BY CASE WHEN rolvaliduntil IS NOT NULL AND rolvaliduntil < now() THEN 1 ELSE 2 END, rolname
LIMIT 200;
"""

CONFIG_SENSITIVE_COLUMNS = """
SELECT
    table_schema,
    table_name,
    column_name,
    data_type,
    CASE
        WHEN column_name ~* '(password|passwd|pwd|secret)' THEN 'PASSWORD/SECRET'
        WHEN column_name ~* '(ssn|social_security|national_id|sin_number)' THEN 'GOVERNMENT ID'
        WHEN column_name ~* '(credit_card|card_number|ccn|pan_number)' THEN 'CREDIT CARD'
        WHEN column_name ~* '(email|e_mail)' THEN 'EMAIL'
        WHEN column_name ~* '(phone|mobile|cell|telephone)' THEN 'PHONE'
        WHEN column_name ~* '(dob|date_of_birth|birth_date|birthday)' THEN 'DATE OF BIRTH'
        WHEN column_name ~* '(salary|income|wage|compensation)' THEN 'FINANCIAL'
        WHEN column_name ~* '(bank_account|account_number|routing_number|iban)' THEN 'BANK ACCOUNT'
        WHEN column_name ~* '(api_key|apikey|access_token|auth_token|bearer)' THEN 'API KEY/TOKEN'
        WHEN column_name ~* '(private_key|encryption_key|signing_key)' THEN 'ENCRYPTION KEY'
        ELSE 'POTENTIAL PII'
    END AS sensitivity_category,
    'WARNING - Column name suggests sensitive data; verify and ensure proper protection' AS recommendation
FROM information_schema.columns
WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
  AND column_name ~* '(password|passwd|pwd|secret|ssn|social_security|national_id|sin_number|credit_card|card_number|ccn|pan_number|email|e_mail|phone|mobile|cell|telephone|dob|date_of_birth|birth_date|birthday|salary|income|wage|compensation|bank_account|account_number|routing_number|iban|api_key|apikey|access_token|auth_token|bearer|private_key|encryption_key|signing_key)'
ORDER BY
    CASE
        WHEN column_name ~* '(password|passwd|pwd|secret|private_key|encryption_key)' THEN 1
        WHEN column_name ~* '(ssn|social_security|credit_card|card_number|bank_account)' THEN 2
        WHEN column_name ~* '(api_key|apikey|access_token|auth_token)' THEN 3
        ELSE 4
    END,
    table_schema,
    table_name,
    column_name
LIMIT 500;
"""

CONFIG_RLS_STATUS = """
SELECT
    n.nspname AS schema_name,
    c.relname AS table_name,
    CASE WHEN c.relrowsecurity THEN 'ENABLED' ELSE 'DISABLED' END AS rls_enabled,
    CASE WHEN c.relforcerowsecurity THEN 'YES' ELSE 'no' END AS force_rls,
    COALESCE(p.polname, 'N/A') AS policy_name,
    COALESCE(
        CASE p.polcmd
            WHEN '*' THEN 'ALL'
            WHEN 'r' THEN 'SELECT'
            WHEN 'a' THEN 'INSERT'
            WHEN 'w' THEN 'UPDATE'
            WHEN 'd' THEN 'DELETE'
            ELSE p.polcmd::text
        END, 'N/A') AS command_type,
    COALESCE(pg_catalog.pg_get_expr(p.polqual, p.polrelid), 'N/A') AS policy_expression,
    CASE
        WHEN c.relrowsecurity AND p.polname IS NOT NULL THEN 'OK - RLS active with policies'
        WHEN c.relrowsecurity AND p.polname IS NULL THEN 'WARNING - RLS enabled but no policies defined'
        ELSE 'INFO - RLS not enabled'
    END AS status
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_policy p ON p.polrelid = c.oid
WHERE c.relkind = 'r'
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND (c.relrowsecurity OR p.polname IS NOT NULL)
ORDER BY
    CASE WHEN c.relrowsecurity AND p.polname IS NULL THEN 0 ELSE 1 END,
    n.nspname,
    c.relname
LIMIT 200;
"""

CONFIG_AUDIT_CONFIG = """
SELECT
    'pgaudit Extension' AS check_item,
    CASE
        WHEN EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pgaudit') THEN 'INSTALLED (v' || (SELECT extversion FROM pg_extension WHERE extname = 'pgaudit') || ')'
        ELSE 'NOT INSTALLED'
    END AS current_value,
    CASE
        WHEN EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pgaudit') THEN 'OK - pgaudit provides detailed audit logging'
        ELSE 'RECOMMENDATION - Consider installing pgaudit for compliance audit trails'
    END AS status
UNION ALL
SELECT
    'log_statement Setting' AS check_item,
    current_setting('log_statement') AS current_value,
    CASE current_setting('log_statement')
        WHEN 'none' THEN 'WARNING - No SQL statements are logged'
        WHEN 'ddl' THEN 'OK - DDL statements logged (schema changes)'
        WHEN 'mod' THEN 'OK - DDL + data modification statements logged'
        WHEN 'all' THEN 'INFO - All statements logged (may impact performance)'
        ELSE 'UNKNOWN'
    END AS status
UNION ALL
SELECT
    'log_connections Setting' AS check_item,
    current_setting('log_connections') AS current_value,
    CASE WHEN current_setting('log_connections') = 'on' THEN 'OK - Connection attempts are logged'
         ELSE 'RECOMMENDATION - Enable to track connection attempts'
    END AS status
UNION ALL
SELECT
    'log_disconnections Setting' AS check_item,
    current_setting('log_disconnections') AS current_value,
    CASE WHEN current_setting('log_disconnections') = 'on' THEN 'OK - Disconnections are logged'
         ELSE 'RECOMMENDATION - Enable to track session durations'
    END AS status
UNION ALL
SELECT
    'log_checkpoints Setting' AS check_item,
    current_setting('log_checkpoints') AS current_value,
    CASE WHEN current_setting('log_checkpoints') = 'on' THEN 'OK - Checkpoint activity is logged'
         ELSE 'RECOMMENDATION - Enable to monitor checkpoint frequency'
    END AS status
LIMIT 10;
"""

CONFIG_DB_ROLE_OVERRIDES = """
SELECT
    COALESCE(d.datname, 'ALL DATABASES') AS database_name,
    COALESCE(r.rolname, 'ALL ROLES') AS role_name,
    unnest(s.setconfig) AS setting_override,
    'WARNING - Hidden ALTER DATABASE/ROLE SET override detected; may cause unexpected behavior' AS severity
FROM pg_db_role_setting s
LEFT JOIN pg_database d ON d.oid = s.setdatabase
LEFT JOIN pg_roles r ON r.oid = s.setrole
ORDER BY
    COALESCE(d.datname, ''),
    COALESCE(r.rolname, '')
LIMIT 200;
"""

# ============================================================================
# Section 3: CURRENT ACTIVITY (Real-time Status)
# ============================================================================

ACTIVITY_CONNECTION_SUMMARY = """
WITH connection_stats AS (
    SELECT
        state, wait_event_type, wait_event, datname, usename, application_name,
        client_addr, backend_type,
        EXTRACT(EPOCH FROM (now() - state_change))::integer as state_duration_secs,
        EXTRACT(EPOCH FROM (now() - xact_start))::integer as xact_duration_secs,
        EXTRACT(EPOCH FROM (now() - query_start))::integer as query_duration_secs,
        EXTRACT(EPOCH FROM (now() - backend_start))::integer as connection_duration_secs
    FROM pg_stat_activity
    WHERE pid != pg_backend_pid()
)
SELECT metric, value, description, details, recommendation
FROM (
    SELECT 'Total Connections' as metric, COUNT(*)::text as value,
        'All database connections' as description,
        format('Max allowed: %s, Current: %s (%s%% utilized)',
            current_setting('max_connections'), COUNT(*),
            ROUND(COUNT(*)::numeric * 100 / current_setting('max_connections')::numeric, 1)) as details,
        CASE WHEN COUNT(*)::float / current_setting('max_connections')::integer > 0.75
            THEN 'WARNING: Connection usage high, consider connection pooling' ELSE 'OK' END as recommendation
    FROM connection_stats
    UNION ALL
    SELECT 'Active Queries', COUNT(*)::text, 'Currently executing queries',
        format('Long running (>5 min): %s, Normal: %s',
            COUNT(*) FILTER (WHERE state = 'active' AND query_duration_secs > 300),
            COUNT(*) FILTER (WHERE state = 'active' AND query_duration_secs <= 300)),
        CASE WHEN COUNT(*) FILTER (WHERE state = 'active' AND query_duration_secs > 300) > 0
            THEN 'WARNING: Long-running queries detected' ELSE 'OK' END
    FROM connection_stats WHERE state = 'active'
    UNION ALL
    SELECT 'Idle Connections', COUNT(*)::text, 'Inactive connections',
        format('Idle time > 1 hour: %s, Total idle: %s',
            COUNT(*) FILTER (WHERE state_duration_secs > 3600), COUNT(*)),
        CASE WHEN COUNT(*) FILTER (WHERE state_duration_secs > 3600) > 10
            THEN 'Consider closing idle connections older than 1 hour' ELSE 'OK' END
    FROM connection_stats WHERE state = 'idle'
    UNION ALL
    SELECT 'Idle in Transaction', COUNT(*)::text, 'Transactions open but inactive',
        format('Count: %s, Max age: %s sec, Critical (>10 min): %s',
            COUNT(*) FILTER (WHERE state = 'idle in transaction'),
            COALESCE(MAX(xact_duration_secs) FILTER (WHERE state = 'idle in transaction'), 0),
            COUNT(*) FILTER (WHERE state like 'idle in transaction%' AND xact_duration_secs > 600)),
        CASE WHEN COUNT(*) FILTER (WHERE state like 'idle in transaction%' AND xact_duration_secs > 600) > 0
            THEN 'CRITICAL: Long-running idle transactions detected' ELSE 'OK' END
    FROM connection_stats WHERE state like 'idle in transaction%'
    UNION ALL
    SELECT 'Waiting Queries', COUNT(*)::text, 'Queries waiting for resources',
        format('Lock waits: %s, IO waits: %s, Other: %s',
            COUNT(*) FILTER (WHERE wait_event_type = 'Lock'),
            COUNT(*) FILTER (WHERE wait_event_type = 'IO'),
            COUNT(*) FILTER (WHERE wait_event_type NOT IN ('Lock', 'IO') AND wait_event_type IS NOT NULL)),
        CASE WHEN COUNT(*) FILTER (WHERE wait_event_type = 'Lock') > 0
            THEN 'WARNING: Lock contention detected' ELSE 'OK' END
    FROM connection_stats WHERE wait_event_type IS NOT NULL
) stats
ORDER BY CASE metric
    WHEN 'Total Connections' THEN 1 WHEN 'Active Queries' THEN 2
    WHEN 'Waiting Queries' THEN 3 WHEN 'Idle in Transaction' THEN 4 ELSE 5 END;
"""

ACTIVITY_POOLING_DETECTION = """
WITH session_ages AS (
    SELECT application_name, client_addr, COUNT(*) AS connection_count,
        MIN(EXTRACT(EPOCH FROM (now() - backend_start))) AS min_age_seconds,
        MAX(EXTRACT(EPOCH FROM (now() - backend_start))) AS max_age_seconds,
        AVG(EXTRACT(EPOCH FROM (now() - backend_start))) AS avg_age_seconds,
        STDDEV(EXTRACT(EPOCH FROM (now() - backend_start))) AS stddev_age_seconds
    FROM pg_stat_activity
    WHERE backend_type = 'client backend' AND pid <> pg_backend_pid()
    GROUP BY application_name, client_addr
)
SELECT
    COALESCE(application_name, '') AS application_name,
    COALESCE(client_addr::text, 'local') AS client_address,
    connection_count,
    ROUND(min_age_seconds::numeric, 1) AS min_age_sec,
    ROUND(max_age_seconds::numeric, 1) AS max_age_sec,
    ROUND(avg_age_seconds::numeric, 1) AS avg_age_sec,
    CASE
        WHEN LOWER(COALESCE(application_name, '')) ~ '(pgbouncer|pgpool|odyssey|pgcat|supavisor)' THEN 'HIGH - Known pooler detected'
        WHEN connection_count >= 10 AND COALESCE(stddev_age_seconds, 0) < 5 THEN 'MEDIUM - Many connections with similar ages'
        ELSE 'NONE - No pooling pattern detected'
    END AS pooling_confidence
FROM session_ages
ORDER BY connection_count DESC LIMIT 50;
"""

ACTIVITY_APP_IDENTIFICATION = """
SELECT
    COALESCE(NULLIF(application_name, ''), '<unnamed>') AS application,
    COUNT(*) AS total_connections,
    COUNT(*) FILTER (WHERE state = 'active') AS active,
    COUNT(*) FILTER (WHERE state = 'idle') AS idle,
    COUNT(*) FILTER (WHERE state = 'idle in transaction') AS idle_in_txn,
    COUNT(DISTINCT usename) AS distinct_users,
    COUNT(DISTINCT datname) AS distinct_databases,
    COUNT(DISTINCT client_addr) AS distinct_clients,
    ROUND(AVG(EXTRACT(EPOCH FROM (now() - backend_start)))::numeric, 1) AS avg_session_age_sec
FROM pg_stat_activity
WHERE backend_type = 'client backend' AND pid <> pg_backend_pid()
GROUP BY application_name
ORDER BY total_connections DESC LIMIT 50;
"""

ACTIVITY_WORKLOAD_CHARACTERIZATION = """
WITH query_types AS (
    SELECT
        CASE
            WHEN UPPER(LEFT(LTRIM(query), 6)) = 'SELECT' THEN 'READ (SELECT)'
            WHEN UPPER(LEFT(LTRIM(query), 6)) = 'INSERT' THEN 'WRITE (INSERT)'
            WHEN UPPER(LEFT(LTRIM(query), 6)) = 'UPDATE' THEN 'WRITE (UPDATE)'
            WHEN UPPER(LEFT(LTRIM(query), 6)) = 'DELETE' THEN 'WRITE (DELETE)'
            WHEN UPPER(LEFT(LTRIM(query), 4)) = 'COPY' THEN 'WRITE (COPY)'
            WHEN query = '' OR query IS NULL THEN 'NO QUERY'
            ELSE 'OTHER'
        END AS query_type, state, datname
    FROM pg_stat_activity
    WHERE backend_type = 'client backend' AND pid <> pg_backend_pid()
),
type_summary AS (
    SELECT query_type, COUNT(*) AS connection_count,
        COUNT(*) FILTER (WHERE state = 'active') AS active_count,
        COUNT(DISTINCT datname) AS databases
    FROM query_types GROUP BY query_type
),
totals AS (
    SELECT SUM(connection_count) AS total,
        SUM(CASE WHEN query_type LIKE 'READ%' THEN connection_count ELSE 0 END) AS reads,
        SUM(CASE WHEN query_type LIKE 'WRITE%' THEN connection_count ELSE 0 END) AS writes
    FROM type_summary
)
SELECT ts.query_type, ts.connection_count, ts.active_count, ts.databases,
    CASE WHEN t.total > 0 THEN ROUND((ts.connection_count * 100.0 / t.total)::numeric, 1) ELSE 0 END AS pct_of_total,
    CASE WHEN t.reads + t.writes > 0 THEN 'Read/Write Ratio: ' || t.reads || ':' || t.writes ELSE 'No read/write activity' END AS rw_ratio_summary
FROM type_summary ts CROSS JOIN totals t
ORDER BY ts.connection_count DESC LIMIT 20;
"""

ACTIVITY_CLIENT_ANALYSIS = """
SELECT
    COALESCE(client_addr::text, 'local/unix') AS client_address,
    COALESCE(client_hostname, '') AS client_hostname,
    COUNT(*) AS total_connections,
    COUNT(*) FILTER (WHERE state = 'active') AS active,
    COUNT(*) FILTER (WHERE state = 'idle') AS idle,
    COUNT(*) FILTER (WHERE state = 'idle in transaction') AS idle_in_txn,
    COUNT(DISTINCT usename) AS distinct_users,
    COUNT(DISTINCT datname) AS distinct_databases,
    STRING_AGG(DISTINCT COALESCE(NULLIF(application_name, ''), '<unnamed>'), ', ') AS applications,
    ROUND(AVG(EXTRACT(EPOCH FROM (now() - backend_start)))::numeric, 1) AS avg_session_age_sec
FROM pg_stat_activity
WHERE backend_type = 'client backend' AND pid <> pg_backend_pid()
GROUP BY client_addr, client_hostname
ORDER BY total_connections DESC LIMIT 50;
"""

ACTIVITY_CONNECTION_CHURN = """
WITH age_buckets AS (
    SELECT
        CASE
            WHEN EXTRACT(EPOCH FROM (now() - backend_start)) < 60 THEN '< 1 min (very new)'
            WHEN EXTRACT(EPOCH FROM (now() - backend_start)) < 300 THEN '1-5 min (new)'
            WHEN EXTRACT(EPOCH FROM (now() - backend_start)) < 3600 THEN '5-60 min (recent)'
            WHEN EXTRACT(EPOCH FROM (now() - backend_start)) < 86400 THEN '1-24 hours (stable)'
            ELSE '> 24 hours (long-lived)'
        END AS connection_age_bucket,
        CASE
            WHEN EXTRACT(EPOCH FROM (now() - backend_start)) < 60 THEN 1
            WHEN EXTRACT(EPOCH FROM (now() - backend_start)) < 300 THEN 2
            WHEN EXTRACT(EPOCH FROM (now() - backend_start)) < 3600 THEN 3
            WHEN EXTRACT(EPOCH FROM (now() - backend_start)) < 86400 THEN 4
            ELSE 5
        END AS bucket_order, state
    FROM pg_stat_activity
    WHERE backend_type = 'client backend' AND pid <> pg_backend_pid()
),
bucket_summary AS (
    SELECT connection_age_bucket, bucket_order, COUNT(*) AS connection_count,
        COUNT(*) FILTER (WHERE state = 'active') AS active,
        COUNT(*) FILTER (WHERE state = 'idle') AS idle,
        COUNT(*) FILTER (WHERE state = 'idle in transaction') AS idle_in_txn
    FROM age_buckets GROUP BY connection_age_bucket, bucket_order
),
totals AS (SELECT SUM(connection_count) AS total FROM bucket_summary)
SELECT bs.connection_age_bucket, bs.connection_count,
    CASE WHEN t.total > 0 THEN ROUND((bs.connection_count * 100.0 / t.total)::numeric, 1) ELSE 0 END AS pct_of_total,
    bs.active, bs.idle, bs.idle_in_txn,
    CASE
        WHEN bs.bucket_order <= 2 AND bs.connection_count > (t.total * 0.5) THEN 'HIGH CHURN'
        WHEN bs.bucket_order >= 4 AND bs.connection_count > (t.total * 0.8) THEN 'STABLE'
        ELSE 'NORMAL'
    END AS churn_indicator
FROM bucket_summary bs CROSS JOIN totals t
ORDER BY bs.bucket_order LIMIT 20;
"""

# ============================================================================
# Section 4: REPLICATION STATUS (High Availability)
# ============================================================================

REPLICATION_INFO = """
WITH db_role AS (
    SELECT
        CASE WHEN pg_is_in_recovery() THEN 'REPLICA' ELSE 'PRIMARY' END as server_role,
        current_setting('wal_level') as wal_level,
        '{deployment_type}' as deployment
)
SELECT metric, value, description, details, recommendation
FROM (
    SELECT 'Deployment Type' as metric, deployment as value,
        'Detected database deployment environment' as description,
        'Aurora uses storage-level replication (6-way, 3 AZs). WAL-based replication not applicable' as details,
        'INFO: Use Aurora Replicas for read scaling. Cross-region via Aurora Global Database' as recommendation,
        0 as sort_order
    FROM db_role
    UNION ALL
    SELECT 'Database Role', server_role, 'Current database role and WAL configuration',
        'WAL Level: ' || wal_level || ', Server Role: ' || server_role,
        CASE WHEN server_role = 'REPLICA' THEN 'OK: Aurora Reader instance' ELSE 'OK: Aurora Writer instance' END,
        1 FROM db_role
    UNION ALL
    SELECT 'Connected Replicas', COALESCE((SELECT COUNT(*)::text FROM pg_stat_replication), '0'),
        'Aurora Replicas connected via storage layer (may not appear in pg_stat_replication)',
        CASE WHEN (SELECT COUNT(*) FROM pg_stat_replication) > 0
            THEN (SELECT COUNT(*)::text FROM pg_stat_replication) || ' replica(s) connected'
            ELSE 'Aurora replicas use storage-level replication - check AWS Console' END,
        CASE WHEN (SELECT COUNT(*) FROM pg_stat_replication) > 0 THEN 'OK: Replication active'
            ELSE 'INFO: Check Aurora console for replica count and lag' END,
        2 FROM db_role
    UNION ALL
    SELECT 'WAL Archiving', 'Managed',
        'Aurora manages WAL archiving internally via continuous backup',
        'Continuous backup enabled. Retention configurable via AWS Console',
        'INFO: Managed by AWS. Configure backup retention in AWS Console',
        3 FROM pg_stat_archiver CROSS JOIN db_role
    UNION ALL
    SELECT 'WAL-Based Replication',
        CASE WHEN wal_level IN ('logical', 'replica') AND (SELECT COUNT(*) FROM pg_replication_slots) > 0
            THEN 'Active (' || (SELECT COUNT(*)::text FROM pg_replication_slots) || ' slot(s))'
            WHEN wal_level IN ('logical', 'replica') THEN 'Configured (no slots)'
            ELSE 'Not configured' END,
        CASE WHEN wal_level IN ('logical', 'replica')
            THEN 'User-configured WAL replication detected on Aurora. WAL accumulation requires monitoring'
            ELSE 'WAL level does not support replication' END,
        CASE WHEN (SELECT COUNT(*) FROM pg_replication_slots WHERE NOT active) > 0
            THEN 'INACTIVE SLOTS: ' || (SELECT COUNT(*)::text FROM pg_replication_slots WHERE NOT active)
            ELSE 'No replication slots' END,
        CASE WHEN (SELECT COUNT(*) FROM pg_replication_slots WHERE NOT active) > 0
            THEN 'CRITICAL: Drop inactive slots to prevent WAL buildup. Aurora storage grows with WAL - direct cost impact'
            ELSE 'INFO: No WAL-based replication configured' END,
        4 FROM db_role
) AS replication_metrics ORDER BY sort_order;
"""

REPLICATION_LAG = """
SELECT
    client_addr, application_name, state, sync_state,
    sent_lsn, write_lsn, flush_lsn, replay_lsn,
    COALESCE(write_lag::text, 'N/A') AS write_lag,
    COALESCE(flush_lag::text, 'N/A') AS flush_lag,
    COALESCE(replay_lag::text, 'N/A') AS replay_lag,
    CASE
        WHEN replay_lag > interval '5 minutes' THEN 'CRITICAL - Replay lag exceeds 5 minutes'
        WHEN replay_lag > interval '1 minute' THEN 'WARNING - Replay lag exceeds 1 minute'
        WHEN replay_lag IS NULL THEN 'N/A'
        ELSE 'OK'
    END AS lag_status
FROM pg_stat_replication
ORDER BY replay_lag DESC NULLS LAST LIMIT 50;
"""

REPLICATION_SYNC_CONFIG = """
SELECT
    current_setting('synchronous_standby_names') AS synchronous_standby_names,
    current_setting('synchronous_commit') AS synchronous_commit,
    CASE WHEN current_setting('synchronous_standby_names') = '' THEN 'Disabled' ELSE 'Enabled' END AS sync_replication_active,
    CASE
        WHEN current_setting('synchronous_standby_names') = '' THEN 'INFO - Asynchronous replication'
        WHEN current_setting('synchronous_commit') IN ('on', 'remote_write', 'remote_apply')
            THEN 'OK - Synchronous replication active with ' || current_setting('synchronous_commit')
        ELSE 'INFO - synchronous_commit=' || current_setting('synchronous_commit')
    END AS config_status
LIMIT 1;
"""

REPLICATION_LOGICAL_SLOTS = """
SELECT
    slot_name, plugin, slot_type, database, active,
    pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) as lag_bytes,
    pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) as lag_size,
    pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn) as unflushed_bytes,
    pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn)) as unflushed_size,
    CASE
        WHEN NOT active THEN 'CRITICAL: Inactive slot - WAL accumulation risk'
        WHEN pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) > 1073741824 THEN 'CRITICAL: >1GB lag'
        WHEN pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) > 104857600 THEN 'WARNING: >100MB lag'
        ELSE 'OK'
    END as status,
    CASE
        WHEN NOT active THEN 'Drop unused slot: SELECT pg_drop_replication_slot(''' || slot_name || ''');'
        WHEN pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) > 1073741824 THEN 'Check subscriber health'
        ELSE 'Monitor lag trends'
    END as recommendation
FROM pg_replication_slots
WHERE slot_type = 'logical'
ORDER BY lag_bytes DESC NULLS LAST;
"""

# ============================================================================
# Section 5: DATA FOOTPRINT (Size & WAL Analysis)
# ============================================================================

DATA_DATABASE_SIZES = """
SELECT
    datname as database_name,
    COALESCE(pg_size_pretty(pg_database_size(datname)), 'N/A') as formatted_size,
    COALESCE(ROUND(pg_database_size(datname)::numeric / 1073741824, 2), 0) as size_in_gb,
    CASE
        WHEN pg_database_size(datname) IS NULL THEN 'INACCESSIBLE'
        WHEN pg_database_size(datname) > 53687091200 THEN 'LARGE (>50GB)'
        WHEN pg_database_size(datname) > 10737418240 THEN 'MEDIUM (10-50GB)'
        ELSE 'SMALL (<10GB)'
    END as size_category,
    CASE
        WHEN datallowconn = false THEN 'No connections allowed'
        WHEN datname IN ('template0', 'template1') THEN 'Template database'
        ELSE 'Accessible'
    END as access_status
FROM pg_database
WHERE datname IS NOT NULL
ORDER BY CASE WHEN pg_database_size(datname) IS NULL THEN 1 ELSE 0 END,
    pg_database_size(datname) DESC NULLS LAST;
"""

DATA_WAL_DIRECTORY = """
SELECT
    COUNT(*) as wal_file_count,
    pg_size_pretty(SUM(size)) as total_wal_size,
    SUM(size) as total_wal_bytes,
    pg_size_pretty(AVG(size)::bigint) as avg_wal_file_size,
    pg_size_pretty(MAX(size)) as max_wal_file_size,
    pg_size_pretty(MIN(size)) as min_wal_file_size
FROM pg_ls_waldir();
"""

# ============================================================================
# Section 6: QUERY & I/O PERFORMANCE (Metrics & Analysis)
# ============================================================================

PERF_CHECKPOINT_STATS_PG17 = """
WITH checkpoint_counts AS (
    SELECT num_timed as checkpoints_timed, num_requested as checkpoints_req FROM pg_stat_checkpointer
),
max_duration AS (
    SELECT max(extract(epoch from now() - stats_reset)::integer) as max_uptime_seconds FROM pg_stat_bgwriter
)
SELECT metric, value, description, recommendation
FROM (
    SELECT 'Checkpoint Timeout' as metric, current_setting('checkpoint_timeout') as value,
        'Time between automatic WAL checkpoints' as description,
        CASE WHEN current_setting('checkpoint_timeout') < '5min' THEN 'WARNING: Frequent checkpoints'
             WHEN current_setting('checkpoint_timeout') > '30min' THEN 'WARNING: Long checkpoint intervals'
             ELSE 'Normal range (5min to 30min)' END as recommendation
    UNION ALL
    SELECT 'Checkpoint Completion Target', current_setting('checkpoint_completion_target'),
        'Target of checkpoint completion', CASE WHEN current_setting('checkpoint_completion_target')::float < 0.5
        THEN 'WARNING: Low target may cause I/O spikes' ELSE 'Normal range (0.5-0.9)' END
    UNION ALL
    SELECT 'Maximum WAL Size', current_setting('max_wal_size'), 'Maximum WAL size that triggers a checkpoint', 'Normal'
    UNION ALL
    SELECT 'Forced Checkpoint Ratio',
        CASE WHEN (SELECT COALESCE(checkpoints_timed, 0) + COALESCE(checkpoints_req, 0) FROM checkpoint_counts) = 0 THEN 'N/A'
        ELSE ROUND((SELECT checkpoints_req * 100.0 / NULLIF(checkpoints_timed + checkpoints_req, 0) FROM checkpoint_counts)::numeric, 1)::text || '%' END,
        'Percentage of checkpoints forced vs timed',
        CASE WHEN (SELECT checkpoints_req * 100.0 / NULLIF(checkpoints_timed + checkpoints_req, 0) FROM checkpoint_counts) > 50
        THEN 'WARNING: High forced checkpoint ratio' ELSE 'Normal' END
    UNION ALL
    SELECT 'Buffer Writer Stats', (SELECT buffers_clean::text FROM pg_stat_bgwriter),
        'Number of buffers written by background writer',
        CASE WHEN (SELECT buffers_clean FROM pg_stat_bgwriter) = 0 THEN 'WARNING: Background writer might be inactive' ELSE 'Normal' END
    UNION ALL
    SELECT 'Database Uptime', (SELECT (max_uptime_seconds/3600)::text || ' hours' FROM max_duration),
        'Time since last statistics reset', 'Informational'
) as checkpoint_info
ORDER BY CASE metric WHEN 'Checkpoint Timeout' THEN 2 WHEN 'Forced Checkpoint Ratio' THEN 4 ELSE 8 END;
"""

PERF_CHECKPOINT_STATS_PRE17 = """
WITH checkpoint_counts AS (
    SELECT checkpoints_timed, checkpoints_req FROM pg_stat_bgwriter
),
max_duration AS (
    SELECT max(extract(epoch from now() - stats_reset)::integer) as max_uptime_seconds FROM pg_stat_bgwriter
)
SELECT metric, value, description, recommendation
FROM (
    SELECT 'Checkpoint Timeout' as metric, current_setting('checkpoint_timeout') as value,
        'Time between automatic WAL checkpoints' as description,
        CASE WHEN current_setting('checkpoint_timeout') < '5min' THEN 'WARNING: Frequent checkpoints'
             WHEN current_setting('checkpoint_timeout') > '30min' THEN 'WARNING: Long checkpoint intervals'
             ELSE 'Normal range (5min to 30min)' END as recommendation
    UNION ALL
    SELECT 'Checkpoint Completion Target', current_setting('checkpoint_completion_target'),
        'Target of checkpoint completion', CASE WHEN current_setting('checkpoint_completion_target')::float < 0.5
        THEN 'WARNING: Low target may cause I/O spikes' ELSE 'Normal range (0.5-0.9)' END
    UNION ALL
    SELECT 'Maximum WAL Size', current_setting('max_wal_size'), 'Maximum WAL size that triggers a checkpoint', 'Normal'
    UNION ALL
    SELECT 'Forced Checkpoint Ratio',
        CASE WHEN (SELECT COALESCE(checkpoints_timed, 0) + COALESCE(checkpoints_req, 0) FROM checkpoint_counts) = 0 THEN 'N/A'
        ELSE ROUND((SELECT checkpoints_req * 100.0 / NULLIF(checkpoints_timed + checkpoints_req, 0) FROM checkpoint_counts)::numeric, 1)::text || '%' END,
        'Percentage of checkpoints forced vs timed',
        CASE WHEN (SELECT checkpoints_req * 100.0 / NULLIF(checkpoints_timed + checkpoints_req, 0) FROM checkpoint_counts) > 50
        THEN 'WARNING: High forced checkpoint ratio' ELSE 'Normal' END
    UNION ALL
    SELECT 'Buffer Writer Stats', (SELECT buffers_clean::text FROM pg_stat_bgwriter),
        'Number of buffers written by background writer',
        CASE WHEN (SELECT buffers_clean FROM pg_stat_bgwriter) = 0 THEN 'WARNING: Background writer might be inactive' ELSE 'Normal' END
    UNION ALL
    SELECT 'Database Uptime', (SELECT (max_uptime_seconds/3600)::text || ' hours' FROM max_duration),
        'Time since last statistics reset', 'Informational'
) as checkpoint_info
ORDER BY CASE metric WHEN 'Checkpoint Timeout' THEN 2 WHEN 'Forced Checkpoint Ratio' THEN 4 ELSE 8 END;
"""

PERF_BGWRITER_STATS = """
WITH bgwriter_stats AS (
    SELECT buffers_clean, maxwritten_clean, buffers_alloc, stats_reset,
        extract(epoch from (now() - stats_reset))::bigint as stats_uptime_secs
    FROM pg_stat_bgwriter
),
computed_stats AS (
    SELECT *,
        ROUND(buffers_clean::numeric / NULLIF(GREATEST(stats_uptime_secs, 1), 0), 2) as clean_rate_per_sec,
        ROUND(100.0 * buffers_clean::numeric / NULLIF(buffers_alloc, 0), 2) as clean_ratio
    FROM bgwriter_stats
)
SELECT metric, value, description, recommendation
FROM (
    SELECT 'Buffers Clean Rate' as metric, clean_rate_per_sec::text || ' buffers/sec' as value,
        'Average rate of buffers written by background writer' as description,
        CASE WHEN clean_rate_per_sec = 0 THEN 'WARNING: Background writer inactive' ELSE 'Normal activity' END as recommendation
    FROM computed_stats
    UNION ALL
    SELECT 'Maxwritten Clean Events', maxwritten_clean::text, 'Times background writer stopped due to writing too many buffers',
        CASE WHEN maxwritten_clean > 100 THEN 'Consider increasing bgwriter_lru_maxpages' ELSE 'Normal' END
    FROM computed_stats
    UNION ALL
    SELECT 'Background Writer Efficiency', clean_ratio::text || '%', 'Percentage of total allocations handled by background writer',
        CASE WHEN clean_ratio < 10 THEN 'Low efficiency - Consider tuning bgwriter parameters' ELSE 'Normal' END
    FROM computed_stats
    UNION ALL
    SELECT 'Background Writer Settings',
        CONCAT('delay: ', current_setting('bgwriter_delay'), ', lru_maxpages: ', current_setting('bgwriter_lru_maxpages'),
               ', lru_multiplier: ', current_setting('bgwriter_lru_multiplier')),
        'Current background writer configuration', 'Review if experiencing performance issues'
) as bgwriter_metrics;
"""

PERF_AUTOVACUUM_UTILIZATION = """
WITH autovacuum_activity AS (
    SELECT
        COUNT(*) FILTER (WHERE backend_type = 'autovacuum worker') as active_workers,
        current_setting('autovacuum_max_workers')::int as max_workers
    FROM pg_stat_activity WHERE state <> 'idle'
),
vacuum_stats AS (
    SELECT COUNT(*) as total_tables,
        COUNT(*) FILTER (WHERE n_dead_tup > 0) as tables_needing_vacuum,
        COUNT(*) FILTER (WHERE n_dead_tup > 10000) as high_dead_tuples,
        ROUND(AVG(n_dead_tup) FILTER (WHERE n_dead_tup > 0))::bigint as avg_dead_tuples,
        MAX(n_dead_tup) as max_dead_tuples
    FROM pg_stat_user_tables
)
SELECT metric, value, description, recommendation
FROM (
    SELECT 'Active Autovacuum Workers' as metric,
        active_workers::text || ' of ' || max_workers::text || ' workers' as value,
        'Currently running autovacuum processes' as description,
        CASE WHEN active_workers >= max_workers THEN 'CRITICAL: All workers busy'
             WHEN active_workers >= (max_workers * 0.8) THEN 'WARNING: Worker pool nearly full'
             ELSE 'Normal utilization' END as recommendation
    FROM autovacuum_activity
    UNION ALL
    SELECT 'Autovacuum Configuration',
        format('scale_factor: %s, analyze_scale_factor: %s, cost_delay: %s',
            current_setting('autovacuum_vacuum_scale_factor'),
            current_setting('autovacuum_analyze_scale_factor'),
            current_setting('autovacuum_vacuum_cost_delay')),
        'Current autovacuum configuration parameters',
        CASE WHEN current_setting('autovacuum_vacuum_scale_factor')::float > 0.2
            THEN 'Consider reducing vacuum_scale_factor' ELSE 'Configuration appears reasonable' END
    UNION ALL
    SELECT 'Tables Needing Vacuum', tables_needing_vacuum::text || ' of ' || total_tables::text || ' tables',
        format('Average dead tuples: %s, Maximum: %s', avg_dead_tuples, max_dead_tuples),
        CASE WHEN tables_needing_vacuum::float / NULLIF(total_tables, 0) > 0.3 THEN 'WARNING: Large percentage need vacuum'
             ELSE 'Normal vacuum need' END
    FROM vacuum_stats
    UNION ALL
    SELECT 'Autovacuum Settings',
        CASE WHEN current_setting('autovacuum')::boolean THEN 'Enabled' ELSE 'Disabled' END,
        format('analyze_threshold: %s, vacuum_threshold: %s',
            current_setting('autovacuum_analyze_threshold'), current_setting('autovacuum_vacuum_threshold')),
        CASE WHEN NOT current_setting('autovacuum')::boolean THEN 'CRITICAL: Autovacuum is disabled' ELSE 'Settings appear normal' END
) as autovacuum_metrics;
"""

PERF_POOLING_EFFICIENCY = """
WITH connection_stats AS (
    SELECT COUNT(*) as total_connections,
        COUNT(*) FILTER (WHERE state = 'active') as active_connections,
        COUNT(*) FILTER (WHERE state = 'idle') as idle_connections,
        COUNT(DISTINCT usename) as unique_users,
        COUNT(DISTINCT application_name) FILTER (WHERE application_name IS NOT NULL) as unique_applications,
        current_setting('max_connections')::int as max_connections
    FROM pg_stat_activity WHERE pid != pg_backend_pid()
),
connection_age AS (
    SELECT COUNT(*) FILTER (WHERE state = 'idle' AND EXTRACT(EPOCH FROM (now() - state_change)) > 300) as long_idle_connections
    FROM pg_stat_activity WHERE pid != pg_backend_pid()
)
SELECT metric, value, description, recommendation
FROM (
    SELECT 'Connection Utilization' as metric,
        total_connections::text || ' of ' || max_connections::text ||
        ' (' || ROUND(100.0 * total_connections / max_connections, 1)::text || '%)' as value,
        'Current connection usage' as description,
        CASE WHEN total_connections::float / max_connections > 0.85 THEN 'CRITICAL: Near connection limit'
             WHEN total_connections::float / max_connections > 0.70 THEN 'WARNING: High connection usage'
             ELSE 'Normal utilization' END as recommendation
    FROM connection_stats
    UNION ALL
    SELECT 'Connection State Distribution',
        'Active: ' || active_connections::text || ', Idle: ' || idle_connections::text,
        'Distribution of connection states',
        CASE WHEN idle_connections::float / NULLIF(total_connections, 0) > 0.75
            THEN 'WARNING: High proportion of idle connections' ELSE 'Normal distribution' END
    FROM connection_stats
    UNION ALL
    SELECT 'Long Idle Connections', long_idle_connections::text || ' connections',
        'Connections idle for more than 5 minutes',
        CASE WHEN long_idle_connections > 20 THEN 'CRITICAL: Many long-idle connections'
             WHEN long_idle_connections > 10 THEN 'WARNING: Consider connection timeout or pooling'
             ELSE 'Normal' END
    FROM connection_age
) metrics;
"""

PERF_BUFFER_HIT_RATIOS = """
WITH db_buffer_stats AS (
    SELECT d.datname, COALESCE(s.blks_hit, 0) as total_hits, COALESCE(s.blks_read, 0) as total_reads,
        COALESCE(s.blks_hit + s.blks_read, 0) as total_accesses, pg_database_size(d.datname) as db_size
    FROM pg_database d LEFT JOIN pg_stat_database s ON d.oid = s.datid
    WHERE d.datallowconn AND NOT d.datistemplate
)
SELECT datname as database_name, pg_size_pretty(db_size) as database_size,
    total_hits as buffer_hits, total_reads as disk_reads,
    COALESCE(ROUND(100.0 * total_hits / NULLIF(total_accesses, 0), 2), 0) as hit_ratio_percent,
    CASE WHEN total_accesses = 0 THEN 'No activity'
         WHEN ROUND(100.0 * total_hits / NULLIF(total_accesses, 0), 2) < 90 THEN 'CRITICAL: Poor cache performance'
         WHEN ROUND(100.0 * total_hits / NULLIF(total_accesses, 0), 2) < 95 THEN 'WARNING: Below optimal'
         ELSE 'OK: Good cache performance' END as performance_status
FROM db_buffer_stats ORDER BY hit_ratio_percent ASC, db_size DESC;
"""


PERF_LOCK_TREE = """
WITH lock_waits AS (
    SELECT
        pid,
        datname,
        usename,
        application_name,
        state,
        wait_event_type,
        wait_event,
        query,
        EXTRACT(EPOCH FROM (now() - state_change))::integer as wait_duration_seconds,
        EXTRACT(EPOCH FROM (now() - query_start))::integer as query_duration_seconds,
        EXTRACT(EPOCH FROM (now() - xact_start))::integer as transaction_duration_seconds,
        backend_type
    FROM pg_stat_activity
    WHERE wait_event_type IS NOT NULL
    AND pid != pg_backend_pid()
),
lock_tree AS (
    SELECT
        blocked.pid as blocked_pid,
        blocked.usename as blocked_user,
        blocked.query as blocked_query,
        blocking.pid as blocking_pid,
        blocking.usename as blocking_user,
        blocking.query as blocking_query,
        blocking.state as blocking_state,
        l.mode as lock_mode,
        l.locktype,
        l.relation::regclass as relation_name,
        EXTRACT(EPOCH FROM (now() - blocked.query_start))::integer as blocked_duration_seconds,
        EXTRACT(EPOCH FROM (now() - blocking.query_start))::integer as blocking_duration_seconds
    FROM pg_locks l
    JOIN pg_stat_activity blocked ON l.pid = blocked.pid
    JOIN pg_locks bl ON l.relation = bl.relation AND l.database = bl.database
    JOIN pg_stat_activity blocking ON bl.pid = blocking.pid
    WHERE NOT l.granted AND bl.granted
    AND blocked.pid != blocking.pid
),
deadlock_candidates AS (
    SELECT
        t1.blocked_pid,
        t1.blocking_pid,
        t2.blocked_pid as potential_deadlock_with,
        t1.relation_name,
        'Potential circular dependency' as deadlock_type
    FROM lock_tree t1
    JOIN lock_tree t2 ON t1.blocked_pid = t2.blocking_pid AND t1.blocking_pid = t2.blocked_pid
),
lock_summary AS (
    SELECT
        wait_event_type,
        wait_event,
        COUNT(*) as current_waiters,
        AVG(wait_duration_seconds)::integer as avg_wait_seconds,
        MAX(wait_duration_seconds) as max_wait_seconds,
        MIN(wait_duration_seconds) as min_wait_seconds,
        COUNT(*) FILTER (WHERE wait_duration_seconds > 60) as long_waiters_1min,
        COUNT(*) FILTER (WHERE wait_duration_seconds > 300) as long_waiters_5min
    FROM lock_waits
    WHERE wait_event_type = 'Lock'
    GROUP BY wait_event_type, wait_event
),
overall_wait_stats AS (
    SELECT
        COUNT(*) as total_waiting_processes,
        COUNT(*) FILTER (WHERE wait_event_type = 'Lock') as lock_waiters,
        COUNT(*) FILTER (WHERE wait_event_type = 'IO') as io_waiters,
        COUNT(*) FILTER (WHERE wait_event_type NOT IN ('Lock', 'IO')) as other_waiters,
        AVG(wait_duration_seconds) FILTER (WHERE wait_event_type = 'Lock')::integer as avg_lock_wait_seconds,
        MAX(wait_duration_seconds) FILTER (WHERE wait_event_type = 'Lock') as max_lock_wait_seconds
    FROM lock_waits
)
SELECT
    metric,
    value,
    description,
    details,
    recommendation
FROM (
    SELECT
        'Overall Wait Statistics' as metric,
        total_waiting_processes::text as value,
        'Summary of all waiting processes' as description,
        format('Total waiting: %s, Lock waits: %s, IO waits: %s, Other waits: %s, Avg lock wait: %ss, Max lock wait: %ss',
            total_waiting_processes,
            lock_waiters,
            io_waiters,
            other_waiters,
            COALESCE(avg_lock_wait_seconds, 0),
            COALESCE(max_lock_wait_seconds, 0)
        ) as details,
        CASE
            WHEN lock_waiters > 10 THEN 'CRITICAL: High number of lock waiters'
            WHEN max_lock_wait_seconds > 300 THEN 'WARNING: Long lock waits detected'
            WHEN lock_waiters > 5 THEN 'WARNING: Moderate lock contention'
            ELSE 'OK'
        END as recommendation
    FROM overall_wait_stats

    UNION ALL

    SELECT
        'Lock Tree Analysis' as metric,
        COUNT(*)::text as value,
        'Blocking relationships between sessions' as description,
        COALESCE(string_agg(
            format('BLOCKED: PID %s (%s) waiting for PID %s (%s) | Relation: %s | Lock: %s | Blocked: %ss | Blocking: %ss',
                blocked_pid,
                blocked_user,
                blocking_pid,
                blocking_user,
                COALESCE(relation_name::text, 'N/A'),
                lock_mode,
                blocked_duration_seconds,
                blocking_duration_seconds
            ),
            E'\n'
        ), 'No blocking relationships found') as details,
        CASE
            WHEN COUNT(*) > 10 THEN 'CRITICAL: High number of blocking relationships'
            WHEN COUNT(*) > 5 THEN 'WARNING: Multiple blocking relationships detected'
            WHEN COUNT(*) > 0 THEN 'INFO: Some blocking detected - monitor closely'
            ELSE 'OK: No blocking relationships'
        END as recommendation
    FROM lock_tree

    UNION ALL

    SELECT
        'Deadlock Detection' as metric,
        COUNT(*)::text as value,
        'Potential circular lock dependencies' as description,
        COALESCE(string_agg(
            format('DEADLOCK CANDIDATE: PID %s <-> PID %s <-> PID %s on relation %s',
                blocked_pid,
                blocking_pid,
                potential_deadlock_with,
                COALESCE(relation_name::text, 'N/A')
            ),
            E'\n'
        ), 'No potential deadlocks detected') as details,
        CASE
            WHEN COUNT(*) > 0 THEN 'CRITICAL: Potential deadlock situation detected - investigate immediately'
            ELSE 'OK: No deadlock patterns detected'
        END as recommendation
    FROM deadlock_candidates

    UNION ALL

    SELECT
        'Historical Deadlocks' as metric,
        'Database statistics' as value,
        'Deadlock count from pg_stat_database' as description,
        COALESCE((
            SELECT string_agg(
                format('Database: %s, Deadlocks: %s',
                    datname,
                    deadlocks
                ),
                E'\n'
            )
            FROM pg_stat_database
            WHERE deadlocks > 0
        ), 'No historical deadlocks recorded') as details,
        CASE
            WHEN EXISTS (SELECT 1 FROM pg_stat_database WHERE deadlocks > 100)
            THEN 'WARNING: High historical deadlock count - review application logic'
            WHEN EXISTS (SELECT 1 FROM pg_stat_database WHERE deadlocks > 10)
            THEN 'INFO: Some historical deadlocks - monitor patterns'
            ELSE 'OK: Low or no historical deadlocks'
        END as recommendation
) wait_metrics
ORDER BY
    CASE metric
        WHEN 'Overall Wait Statistics' THEN 1
        WHEN 'Lock Tree Analysis' THEN 2
        WHEN 'Deadlock Detection' THEN 3
        WHEN 'Historical Deadlocks' THEN 4
    END;
"""

PERF_IO_STATS = """
WITH table_io_stats AS (
    SELECT
        schemaname,
        relname as tablename,
        relid,
        heap_blks_read,
        heap_blks_hit,
        idx_blks_read,
        idx_blks_hit,
        toast_blks_read,
        toast_blks_hit,
        tidx_blks_read,
        tidx_blks_hit,
        COALESCE(pg_total_relation_size(relid), 0) as total_size_bytes,
        COALESCE(pg_relation_size(relid), 0) as table_size_bytes,
        COALESCE(pg_indexes_size(relid), 0) as index_size_bytes
    FROM pg_statio_user_tables
    WHERE heap_blks_read > 0
),
table_stats AS (
    SELECT
        schemaname,
        relname as tablename,
        relid,
        n_live_tup,
        n_dead_tup,
        seq_scan,
        idx_scan,
        n_tup_ins,
        n_tup_upd,
        n_tup_del,
        n_tup_hot_upd,
        last_vacuum,
        last_autovacuum,
        last_analyze,
        last_autoanalyze
    FROM pg_stat_user_tables
    WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
)
SELECT
    tio.schemaname,
    tio.tablename,
    CASE
        WHEN tio.total_size_bytes = 0 THEN 'N/A (inaccessible)'
        ELSE pg_size_pretty(tio.total_size_bytes)
    END as total_size,
    CASE
        WHEN tio.table_size_bytes = 0 THEN 'N/A (inaccessible)'
        ELSE pg_size_pretty(tio.table_size_bytes)
    END as table_size,
    CASE
        WHEN tio.index_size_bytes = 0 THEN 'N/A (inaccessible)'
        ELSE pg_size_pretty(tio.index_size_bytes)
    END as index_size,
    tio.heap_blks_read as table_blocks_read,
    tio.heap_blks_hit as table_blocks_hit,
    ROUND(100.0 * tio.heap_blks_hit / NULLIF(tio.heap_blks_hit + tio.heap_blks_read, 0), 2) as table_cache_hit_ratio,
    tio.idx_blks_read as index_blocks_read,
    tio.idx_blks_hit as index_blocks_hit,
    ROUND(100.0 * tio.idx_blks_hit / NULLIF(tio.idx_blks_hit + tio.idx_blks_read, 0), 2) as index_cache_hit_ratio,
    ROUND(100.0 * (tio.heap_blks_hit + tio.idx_blks_hit + tio.toast_blks_hit + tio.tidx_blks_hit)::numeric /
        NULLIF((tio.heap_blks_hit + tio.heap_blks_read +
                tio.idx_blks_hit + tio.idx_blks_read +
                tio.toast_blks_hit + tio.toast_blks_read +
                tio.tidx_blks_hit + tio.tidx_blks_read)::numeric, 0), 2) as overall_cache_hit_ratio,
    CASE
        WHEN tio.heap_blks_read > 1000 AND
             (tio.heap_blks_hit::float / NULLIF(tio.heap_blks_hit + tio.heap_blks_read, 0)) < 0.95
        THEN 'CRITICAL: Poor cache hit ratio'
        WHEN ts.n_dead_tup > 10000 AND (ts.n_dead_tup::float / NULLIF(ts.n_live_tup + ts.n_dead_tup, 0)) > 0.1
        THEN 'WARNING: High dead tuple ratio'
        WHEN ts.seq_scan > 1000 AND (ts.idx_scan::float / NULLIF(ts.seq_scan + ts.idx_scan, 0)) < 0.1
        THEN 'WARNING: High sequential scan ratio'
        ELSE 'OK'
    END as health_status,
    CASE
        WHEN tio.heap_blks_read > 1000 AND
             (tio.heap_blks_hit::float / NULLIF(tio.heap_blks_hit + tio.heap_blks_read, 0)) < 0.95
        THEN 'Consider increasing shared_buffers or work_mem'
        WHEN ts.n_dead_tup > 10000 AND (ts.n_dead_tup::float / NULLIF(ts.n_live_tup + ts.n_dead_tup, 0)) > 0.1
        THEN 'Consider running VACUUM'
        WHEN ts.seq_scan > 1000 AND (ts.idx_scan::float / NULLIF(ts.seq_scan + ts.idx_scan, 0)) < 0.1
        THEN 'Consider adding indexes'
        ELSE 'No immediate action needed'
    END as recommendation
FROM table_io_stats tio
JOIN table_stats ts ON tio.relid = ts.relid
ORDER BY tio.heap_blks_read DESC
LIMIT 20;
"""

PERF_TRANSACTION_STATS = """
WITH database_xid_info AS (
    SELECT d.datname, d.datallowconn,
        pg_current_xact_id()::text::bigint as current_xid,
        age(d.datfrozenxid) as xid_age,
        ROUND(100.0 * age(d.datfrozenxid) / 2000000000.0, 2) as xid_age_percent,
        pg_database_size(d.datname) as db_size,
        current_setting('autovacuum_freeze_max_age')::bigint as max_age
    FROM pg_database d WHERE d.datallowconn
),
table_vacuum_info AS (
    SELECT schemaname, relname,
        CASE WHEN n_live_tup + n_dead_tup > 0
            THEN ROUND(100.0 * n_dead_tup / (n_live_tup + n_dead_tup), 2) ELSE 0 END as dead_tuple_pct,
        n_dead_tup as dead_tuples, last_vacuum, last_autovacuum,
        CASE WHEN last_vacuum IS NOT NULL OR last_autovacuum IS NOT NULL
            THEN EXTRACT(EPOCH FROM (now() - COALESCE(last_vacuum, last_autovacuum))) ELSE NULL END as seconds_since_vacuum
    FROM pg_stat_user_tables WHERE n_dead_tup > 0 OR (n_live_tup + n_dead_tup) > 1000
),
vacuum_settings AS (
    SELECT name, setting, unit,
        CASE WHEN name = 'autovacuum' AND setting = 'off' THEN 'CRITICAL' ELSE 'OK' END as config_status
    FROM pg_settings
    WHERE name IN ('autovacuum', 'autovacuum_freeze_max_age', 'vacuum_freeze_min_age',
        'vacuum_freeze_table_age', 'autovacuum_naptime', 'autovacuum_max_workers')
)
SELECT category, name, size, value, status, recommendation
FROM (
    SELECT 1 as sort_order, 'Database XID Health' as category, datname as name,
        pg_size_pretty(db_size) as size,
        xid_age::text || ' (' || xid_age_percent || '% of 2B max)' as value,
        CASE WHEN xid_age >= 1800000000 THEN 'EMERGENCY'
             WHEN xid_age >= 1500000000 THEN 'CRITICAL'
             WHEN xid_age >= 1200000000 THEN 'WARNING'
             ELSE 'OK' END as status,
        CASE WHEN xid_age >= 1800000000 THEN 'EMERGENCY: Immediate VACUUM FREEZE required!'
             WHEN xid_age >= 1500000000 THEN 'CRITICAL: Urgent attention needed'
             ELSE 'XID age is healthy' END as recommendation
    FROM database_xid_info
    UNION ALL
    SELECT 2, 'Tables Needing Vacuum', quote_ident(schemaname) || '.' || quote_ident(relname),
        ROUND(dead_tuple_pct::numeric, 1)::text || '% dead',
        CASE WHEN seconds_since_vacuum IS NULL THEN 'Never vacuumed'
             ELSE ROUND((seconds_since_vacuum / 86400)::numeric, 1)::text || ' days ago' END,
        CASE WHEN dead_tuple_pct >= 50 THEN 'CRITICAL' WHEN dead_tuple_pct >= 30 THEN 'WARNING' ELSE 'OK' END,
        CASE WHEN dead_tuple_pct >= 50 THEN 'CRITICAL: Very high bloat - VACUUM FULL recommended'
             WHEN dead_tuple_pct >= 30 THEN 'WARNING: High dead tuple ratio - schedule VACUUM'
             ELSE 'Dead tuple ratio is acceptable' END
    FROM table_vacuum_info WHERE dead_tuple_pct >= 20
    UNION ALL
    SELECT 3, 'Autovacuum Configuration', name, setting || COALESCE(' ' || unit, ''),
        CASE WHEN name = 'autovacuum' THEN CASE WHEN setting = 'on' THEN 'Enabled' ELSE 'Disabled' END ELSE setting END,
        config_status,
        CASE WHEN name = 'autovacuum' AND setting = 'off' THEN 'CRITICAL: Enable autovacuum' ELSE 'Configuration appears reasonable' END
    FROM vacuum_settings
) transaction_analysis
ORDER BY sort_order, CASE status WHEN 'EMERGENCY' THEN 0 WHEN 'CRITICAL' THEN 1 WHEN 'WARNING' THEN 2 ELSE 5 END, name;
"""

PERF_FUNCTION_PERFORMANCE = """
WITH function_stats AS (
    SELECT
        n.nspname AS schema_name,
        p.proname AS function_name,
        l.lanname AS language,
        CASE p.prokind
            WHEN 'f' THEN 'function'
            WHEN 'p' THEN 'procedure'
            WHEN 'a' THEN 'aggregate'
            WHEN 'w' THEN 'window'
            ELSE p.prokind::text
        END AS kind,
        s.calls,
        ROUND(s.total_time::numeric, 2) AS total_time_ms,
        ROUND(s.self_time::numeric, 2) AS self_time_ms,
        CASE WHEN s.calls > 0
            THEN ROUND((s.total_time / s.calls)::numeric, 4)
            ELSE 0
        END AS avg_time_ms,
        CASE WHEN s.calls > 0
            THEN ROUND((s.self_time / s.calls)::numeric, 4)
            ELSE 0
        END AS avg_self_time_ms,
        CASE
            WHEN s.calls > 10000 AND (s.total_time / GREATEST(s.calls, 1)) > 100 THEN 'CRITICAL'
            WHEN s.calls > 1000 AND (s.total_time / GREATEST(s.calls, 1)) > 50 THEN 'WARNING'
            WHEN s.calls > 100 AND (s.total_time / GREATEST(s.calls, 1)) > 10 THEN 'INFO'
            ELSE 'OK'
        END AS severity,
        CASE
            WHEN s.calls > 10000 AND (s.total_time / GREATEST(s.calls, 1)) > 100
                THEN 'High-frequency slow function - review logic and consider caching'
            WHEN s.calls > 1000 AND (s.total_time / GREATEST(s.calls, 1)) > 50
                THEN 'Moderate concern - check for unnecessary complexity'
            WHEN s.total_time > 60000
                THEN 'High cumulative time - consider optimization'
            ELSE 'Performance acceptable'
        END AS recommendation
    FROM pg_stat_user_functions s
    JOIN pg_proc p ON p.oid = s.funcid
    JOIN pg_namespace n ON n.oid = p.pronamespace
    JOIN pg_language l ON l.oid = p.prolang
    WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
)
SELECT
    schema_name,
    function_name,
    language,
    kind,
    calls,
    total_time_ms,
    self_time_ms,
    avg_time_ms,
    avg_self_time_ms,
    severity,
    recommendation
FROM function_stats
ORDER BY total_time_ms DESC
LIMIT 50;
"""

PERF_WAIT_EVENT_ANALYSIS = """
WITH wait_events AS (
    SELECT COALESCE(wait_event_type, 'CPU/Running') AS wait_event_type,
        COALESCE(wait_event, 'CPU/Running') AS wait_event, state,
        COUNT(*) AS session_count,
        COUNT(*) FILTER (WHERE state = 'active') AS active_sessions,
        COUNT(*) FILTER (WHERE state = 'idle') AS idle_sessions,
        COUNT(*) FILTER (WHERE state = 'idle in transaction') AS idle_in_txn
    FROM pg_stat_activity
    WHERE pid != pg_backend_pid() AND backend_type = 'client backend'
    GROUP BY wait_event_type, wait_event, state
)
SELECT wait_event_type, wait_event, SUM(session_count) AS total_sessions,
    SUM(active_sessions) AS active_sessions,
    CASE WHEN wait_event_type = 'Lock' AND SUM(session_count) > 5 THEN 'CRITICAL'
         WHEN wait_event_type = 'Lock' THEN 'WARNING'
         WHEN wait_event_type = 'IO' AND SUM(active_sessions) > 10 THEN 'WARNING'
         ELSE 'OK' END AS severity,
    CASE WHEN wait_event_type = 'Lock' THEN 'Lock contention detected - investigate blocking queries'
         WHEN wait_event_type = 'IO' AND SUM(active_sessions) > 10 THEN 'High I/O wait - check storage performance'
         ELSE 'Normal operation' END AS recommendation
FROM wait_events
GROUP BY wait_event_type, wait_event
ORDER BY CASE WHEN wait_event_type = 'Lock' THEN 0 ELSE 3 END, SUM(session_count) DESC
LIMIT 50;
"""

PERF_SESSION_DURATION = """
WITH session_info AS (
    SELECT pid, COALESCE(NULLIF(application_name, ''), '<unnamed>') AS application_name,
        COALESCE(client_addr::text, 'local') AS client_address, state,
        COALESCE(wait_event_type, 'None') AS wait_event_type,
        EXTRACT(EPOCH FROM (now() - backend_start))::integer AS session_age_seconds,
        EXTRACT(EPOCH FROM (now() - COALESCE(xact_start, now())))::integer AS txn_age_seconds,
        EXTRACT(EPOCH FROM (now() - COALESCE(query_start, now())))::integer AS query_age_seconds
    FROM pg_stat_activity
    WHERE pid != pg_backend_pid() AND backend_type = 'client backend'
)
SELECT pid, application_name, client_address, state, wait_event_type,
    CASE WHEN session_age_seconds > 86400 THEN (session_age_seconds / 86400) || 'd'
         WHEN session_age_seconds > 3600 THEN (session_age_seconds / 3600) || 'h'
         ELSE (session_age_seconds / 60) || 'm' END AS session_duration,
    CASE WHEN state = 'idle in transaction' AND txn_age_seconds > 300 THEN 'CRITICAL'
         WHEN state = 'active' AND query_age_seconds > 3600 THEN 'CRITICAL'
         WHEN state = 'active' AND query_age_seconds > 300 THEN 'WARNING'
         ELSE 'OK' END AS severity,
    CASE WHEN state = 'idle in transaction' AND txn_age_seconds > 300
        THEN 'Long idle-in-transaction - holding locks, blocking vacuum'
         WHEN state = 'active' AND query_age_seconds > 3600
        THEN 'Query running > 1hr - check for missing indexes'
         ELSE 'Normal' END AS recommendation
FROM session_info
WHERE CASE WHEN state = 'idle in transaction' AND txn_age_seconds > 60 THEN true
           WHEN state = 'active' AND query_age_seconds > 300 THEN true ELSE false END
ORDER BY CASE WHEN state = 'idle in transaction' AND txn_age_seconds > 300 THEN 0 ELSE 3 END,
    session_age_seconds DESC LIMIT 50;
"""

PERF_PREPARED_STATEMENTS = """
WITH prepared_stats AS (
    SELECT
        name AS statement_name,
        statement,
        prepare_time,
        EXTRACT(EPOCH FROM (now() - prepare_time))::integer AS age_seconds,
        parameter_types::text AS parameter_types,
        from_sql,
        CASE
            WHEN EXTRACT(EPOCH FROM (now() - prepare_time)) > 86400 THEN 'WARNING'
            WHEN EXTRACT(EPOCH FROM (now() - prepare_time)) > 3600 THEN 'INFO'
            ELSE 'OK'
        END AS severity,
        CASE
            WHEN EXTRACT(EPOCH FROM (now() - prepare_time)) > 86400
                THEN 'Prepared statement older than 24h - may indicate connection leak or missing DEALLOCATE'
            WHEN EXTRACT(EPOCH FROM (now() - prepare_time)) > 3600
                THEN 'Prepared statement older than 1h - verify this is expected for long-lived connections'
            ELSE 'Normal lifecycle'
        END AS recommendation
    FROM pg_prepared_statements
),
summary AS (
    SELECT
        COUNT(*) AS total_prepared,
        COUNT(*) FILTER (WHERE age_seconds > 86400) AS older_than_24h,
        COUNT(*) FILTER (WHERE age_seconds > 3600) AS older_than_1h,
        COUNT(*) FILTER (WHERE from_sql) AS from_sql_count,
        COUNT(*) FILTER (WHERE NOT from_sql) AS from_protocol_count
    FROM prepared_stats
)
SELECT
    COALESCE(ps.statement_name, '(summary)') AS statement_name,
    CASE
        WHEN ps.statement_name IS NULL THEN 'Total: ' || s.total_prepared || ' prepared statements (' || s.from_sql_count || ' SQL, ' || s.from_protocol_count || ' protocol)'
        ELSE LEFT(ps.statement, 100)
    END AS statement_preview,
    CASE
        WHEN ps.statement_name IS NULL THEN NULL
        ELSE ps.prepare_time::text
    END AS prepare_time,
    CASE
        WHEN ps.statement_name IS NULL THEN
            CASE
                WHEN s.older_than_24h > 0 THEN 'WARNING'
                WHEN s.older_than_1h > 5 THEN 'INFO'
                ELSE 'OK'
            END
        ELSE ps.severity
    END AS severity,
    CASE
        WHEN ps.statement_name IS NULL THEN
            CASE
                WHEN s.total_prepared = 0 THEN 'No prepared statements found in current session'
                WHEN s.older_than_24h > 0 THEN s.older_than_24h || ' statements older than 24h - check for connection leaks'
                ELSE 'Prepared statement usage looks healthy'
            END
        ELSE ps.recommendation
    END AS recommendation
FROM summary s
LEFT JOIN prepared_stats ps ON true
ORDER BY
    CASE WHEN ps.statement_name IS NULL THEN 0 ELSE 1 END,
    CASE ps.severity
        WHEN 'WARNING' THEN 0
        WHEN 'INFO' THEN 1
        ELSE 2
    END,
    ps.age_seconds DESC NULLS LAST
LIMIT 50;
"""

PERF_TEMP_FILE_QUERIES = """
SELECT
    LEFT(query, 80) AS query_preview, calls, temp_blks_written, temp_blks_read,
    pg_size_pretty(temp_blks_written * 8192) AS temp_written_size,
    CASE WHEN temp_blks_written > 100000 THEN 'CRITICAL'
         WHEN temp_blks_written > 10000 THEN 'WARNING' ELSE 'OK' END AS severity,
    CASE WHEN temp_blks_written > 100000
        THEN 'Excessive temp file usage - increase work_mem (current: ' || current_setting('work_mem') || ')'
         WHEN temp_blks_written > 10000 THEN 'Significant temp file usage - consider increasing work_mem'
         ELSE 'Temp file usage within acceptable range' END AS recommendation
FROM pg_stat_statements
WHERE temp_blks_written > 0
ORDER BY temp_blks_written DESC LIMIT 10;
"""

PERF_QUERY_ANALYSIS_PG17 = """
SELECT
    LEFT(query, 100) as query_excerpt,
    calls as execution_count,
    ROUND(total_exec_time::numeric, 2) as total_time_ms,
    ROUND(mean_exec_time::numeric, 2) as avg_time_ms,
    ROUND(min_exec_time::numeric, 2) as min_time_ms,
    ROUND(max_exec_time::numeric, 2) as max_time_ms,
    rows as total_rows,
    ROUND((rows::float / NULLIF(calls, 0))::numeric, 2) as avg_rows,
    shared_blks_hit as cache_hits,
    shared_blks_read as disk_reads,
    ROUND((100.0 * shared_blks_hit / NULLIF(shared_blks_hit + shared_blks_read, 0))::numeric, 2) as cache_hit_ratio,
    temp_blks_written,
    COALESCE(wal_bytes, 0) as wal_bytes,
    ROUND(COALESCE(shared_blk_read_time, 0)::numeric, 2) as shared_blk_read_time_ms,
    CASE
        WHEN mean_exec_time > 1000 AND calls > 1000 THEN 'CRITICAL: Frequently used slow query'
        WHEN mean_exec_time > 1000 THEN 'CRITICAL: High average execution time'
        WHEN ROUND((100.0 * shared_blks_hit / NULLIF(shared_blks_hit + shared_blks_read, 0))::numeric, 2) < 80 THEN 'WARNING: Poor cache performance'
        WHEN temp_blks_written > 10000 THEN 'WARNING: High temporary space usage'
        ELSE 'OK'
    END as health_status
FROM pg_stat_statements
WHERE calls > 10 AND query IS NOT NULL AND query != '<insufficient privilege>'
ORDER BY total_exec_time DESC
LIMIT 20;
"""

PERF_QUERY_ANALYSIS_PRE17 = """
SELECT
    LEFT(query, 100) as query_excerpt,
    calls as execution_count,
    ROUND(total_exec_time::numeric, 2) as total_time_ms,
    ROUND(mean_exec_time::numeric, 2) as avg_time_ms,
    ROUND(min_exec_time::numeric, 2) as min_time_ms,
    ROUND(max_exec_time::numeric, 2) as max_time_ms,
    rows as total_rows,
    ROUND((rows::float / NULLIF(calls, 0))::numeric, 2) as avg_rows,
    shared_blks_hit as cache_hits,
    shared_blks_read as disk_reads,
    ROUND((100.0 * shared_blks_hit / NULLIF(shared_blks_hit + shared_blks_read, 0))::numeric, 2) as cache_hit_ratio,
    temp_blks_written,
    COALESCE(wal_bytes, 0) as wal_bytes,
    ROUND(COALESCE(blk_read_time, 0)::numeric, 2) as blk_read_time_ms,
    CASE
        WHEN mean_exec_time > 1000 AND calls > 1000 THEN 'CRITICAL: Frequently used slow query'
        WHEN mean_exec_time > 1000 THEN 'CRITICAL: High average execution time'
        WHEN ROUND((100.0 * shared_blks_hit / NULLIF(shared_blks_hit + shared_blks_read, 0))::numeric, 2) < 80 THEN 'WARNING: Poor cache performance'
        WHEN temp_blks_written > 10000 THEN 'WARNING: High temporary space usage'
        ELSE 'OK'
    END as health_status
FROM pg_stat_statements
WHERE calls > 10 AND query IS NOT NULL AND query != '<insufficient privilege>'
ORDER BY total_exec_time DESC
LIMIT 20;
"""

# ============================================================================
# Section 7: MAINTENANCE & HEALTH (Vacuum, XID & Integrity)
# ============================================================================

MAINT_DATABASE_INTEGRITY = """
WITH database_stats AS (
    SELECT d.datname, d.datallowconn, d.datconnlimit,
        age(d.datfrozenxid) as xid_age, pg_database_size(d.datname) as db_size,
        s.xact_commit, s.xact_rollback, s.deadlocks, s.blks_read, s.blks_hit
    FROM pg_database d LEFT JOIN pg_stat_database s ON d.oid = s.datid
    WHERE d.datname = current_database()
),
index_stats AS (
    SELECT n.nspname as schemaname, c.relname as tablename, ic.relname as indexname,
        i.indisvalid, i.indisready, i.indislive
    FROM pg_index i JOIN pg_class c ON i.indrelid = c.oid
    JOIN pg_class ic ON i.indexrelid = ic.oid JOIN pg_namespace n ON c.relnamespace = n.oid
    WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
),
table_stats AS (
    SELECT n.nspname as schemaname, c.relname as tablename, c.oid as relid,
        CASE WHEN COALESCE(s.n_live_tup, 0) + COALESCE(s.n_dead_tup, 0) > 0
            THEN ROUND(100.0 * COALESCE(s.n_dead_tup, 0) / (COALESCE(s.n_live_tup, 0) + COALESCE(s.n_dead_tup, 0)), 2)
            ELSE 0 END as dead_tuple_pct,
        COALESCE(s.n_dead_tup, 0) as n_dead_tup, COALESCE(s.n_live_tup, 0) as n_live_tup,
        s.last_analyze, s.last_autoanalyze, s.n_mod_since_analyze,
        COALESCE(c.reloptions::text LIKE '%autovacuum_enabled=false%', false) as autovacuum_disabled
    FROM pg_class c JOIN pg_namespace n ON c.relnamespace = n.oid
    LEFT JOIN pg_stat_user_tables s ON c.relname = s.relname AND n.nspname = s.schemaname
    WHERE c.relkind = 'r' AND n.nspname NOT IN ('pg_catalog', 'information_schema')
    AND pg_total_relation_size(c.oid) > 1048576
)
SELECT check_type as "Check Type", object_name as "Object", status as "Status",
    description as "Description", details as "Details", recommendation as "Recommendation"
FROM (
    SELECT 1 as check_order, 'Database Status' as check_type, datname as object_name,
        CASE WHEN xid_age > 1800000000 THEN 'EMERGENCY'
             WHEN xid_age > 1500000000 THEN 'CRITICAL' ELSE 'OK' END as status,
        'Database connectivity and XID age check' as description,
        CASE WHEN xid_age > 1800000000 THEN 'XID wraparound imminent (' || xid_age || ')'
             ELSE 'Database operational' END as details,
        CASE WHEN xid_age > 1800000000 THEN 'EMERGENCY: Immediate VACUUM FREEZE required'
             ELSE 'OK' END as recommendation
    FROM database_stats
    UNION ALL
    SELECT 2, 'Invalid Indexes', schemaname || '.' || tablename || '.' || indexname,
        'WARNING', 'Broken or incomplete index detected',
        CASE WHEN NOT indisvalid THEN 'Index is invalid' WHEN NOT indisready THEN 'Index is not ready' ELSE 'Index is not live' END,
        'WARNING: REINDEX recommended'
    FROM index_stats WHERE NOT indisvalid OR NOT indisready OR NOT indislive
    UNION ALL
    SELECT 3, 'Table Bloat', ts.schemaname || '.' || ts.tablename,
        CASE WHEN ts.dead_tuple_pct > 30 THEN 'WARNING' ELSE 'OK' END,
        'Real-time table bloat analysis',
        'Dead tuple percentage: ' || ROUND(ts.dead_tuple_pct::numeric, 1) || '%',
        CASE WHEN ts.dead_tuple_pct > 30 THEN 'WARNING: High bloat - VACUUM FULL recommended' ELSE 'OK' END
    FROM table_stats ts WHERE ts.dead_tuple_pct > 10
    UNION ALL
    SELECT 4, 'Statistics Status', ts.schemaname || '.' || ts.tablename,
        CASE WHEN ts.autovacuum_disabled THEN 'WARNING'
             WHEN ts.last_analyze IS NULL AND ts.last_autoanalyze IS NULL THEN 'WARNING' ELSE 'OK' END,
        'Table statistics freshness',
        'Last analyzed: ' || COALESCE(GREATEST(ts.last_analyze, ts.last_autoanalyze)::text, 'never'),
        CASE WHEN ts.autovacuum_disabled THEN 'WARNING: Autovacuum disabled on this table'
             WHEN ts.last_analyze IS NULL AND ts.last_autoanalyze IS NULL THEN 'WARNING: Run ANALYZE' ELSE 'OK' END
    FROM table_stats ts WHERE ts.n_live_tup > 1000
    UNION ALL
    SELECT 5, 'Cache Efficiency', datname,
        CASE WHEN blks_hit + blks_read = 0 THEN 'INFO'
             WHEN (blks_hit::numeric / NULLIF(blks_hit + blks_read, 0)) < 0.85 THEN 'WARNING' ELSE 'OK' END,
        'Buffer cache performance analysis',
        CASE WHEN blks_hit + blks_read = 0 THEN 'No I/O activity recorded'
             ELSE ROUND((blks_hit::numeric / (blks_hit + blks_read) * 100), 2)::text || '% cache hit ratio' END,
        CASE WHEN (blks_hit::numeric / NULLIF(blks_hit + blks_read, 0)) < 0.85
            THEN 'WARNING: Poor cache performance - increase shared_buffers' ELSE 'OK' END
    FROM database_stats
    UNION ALL
    SELECT 6, 'Transaction Health', datname,
        CASE WHEN xact_commit + xact_rollback = 0 THEN 'INFO'
             WHEN (xact_rollback::numeric / NULLIF(xact_commit + xact_rollback, 0)) > 0.10 THEN 'WARNING' ELSE 'OK' END,
        'Application transaction patterns',
        CASE WHEN xact_commit + xact_rollback = 0 THEN 'No transactions recorded'
             ELSE ROUND((xact_rollback::numeric / (xact_commit + xact_rollback) * 100), 2)::text || '% rollback ratio' END,
        CASE WHEN (xact_rollback::numeric / NULLIF(xact_commit + xact_rollback, 0)) > 0.10
            THEN 'WARNING: High rollback ratio - investigate application errors' ELSE 'OK' END
    FROM database_stats
) integrity_checks
ORDER BY check_order, CASE status WHEN 'EMERGENCY' THEN 0 WHEN 'CRITICAL' THEN 1 WHEN 'WARNING' THEN 2 ELSE 4 END;
"""

MAINT_SEQUENCE_EXHAUSTION = """
SELECT schemaname, sequencename, last_value, max_value,
    ROUND(100.0 * last_value / max_value, 2) as percent_used,
    CASE WHEN last_value::numeric / max_value::numeric > 0.9 THEN 'CRITICAL: >90% used'
         WHEN last_value::numeric / max_value::numeric > 0.75 THEN 'WARNING: >75% used'
         ELSE 'OK' END as status,
    CASE WHEN last_value::numeric / max_value::numeric > 0.9
        THEN 'URGENT: Sequence nearly exhausted. Consider BIGINT'
         ELSE 'Monitor sequence usage' END as recommendation
FROM pg_sequences
WHERE last_value IS NOT NULL AND max_value > 0 AND last_value::numeric / max_value::numeric > 0.5
ORDER BY percent_used DESC;
"""

MAINT_CONSTRAINT_VALIDATION = """
SELECT n.nspname as schema_name, c.conrelid::regclass as table_name, c.conname as constraint_name,
    CASE c.contype WHEN 'c' THEN 'CHECK' WHEN 'f' THEN 'FOREIGN KEY' WHEN 'u' THEN 'UNIQUE' ELSE c.contype::text END as constraint_type,
    pg_get_constraintdef(c.oid) as constraint_definition,
    CASE WHEN c.contype = 'f' THEN 'WARNING: Unvalidated FK' ELSE 'INFO: Unvalidated constraint' END as status,
    'Run: ALTER TABLE ' || c.conrelid::regclass || ' VALIDATE CONSTRAINT ' || c.conname || ';' as recommendation
FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace
WHERE NOT c.convalidated AND n.nspname NOT IN ('pg_catalog', 'information_schema')
ORDER BY CASE c.contype WHEN 'f' THEN 0 ELSE 2 END;
"""

MAINT_MATERIALIZED_VIEWS = """
SELECT m.schemaname, m.matviewname as view_name, m.ispopulated,
    pg_size_pretty(pg_total_relation_size(quote_ident(m.schemaname) || '.' || quote_ident(m.matviewname))) as view_size,
    s.n_live_tup as row_count, s.last_analyze, s.last_autoanalyze,
    CASE WHEN NOT m.ispopulated THEN 'CRITICAL: View never populated'
         WHEN s.last_analyze IS NULL AND s.last_autoanalyze IS NULL THEN 'WARNING: Never analyzed'
         ELSE 'OK' END as status,
    CASE WHEN NOT m.ispopulated
        THEN 'Run: REFRESH MATERIALIZED VIEW ' || quote_ident(m.schemaname) || '.' || quote_ident(m.matviewname) || ';'
         ELSE 'Refresh schedule appears adequate' END as recommendation
FROM pg_matviews m LEFT JOIN pg_stat_user_tables s ON s.relname = m.matviewname AND s.schemaname = m.schemaname
WHERE m.schemaname NOT IN ('pg_catalog', 'information_schema')
ORDER BY CASE WHEN NOT m.ispopulated THEN 0 ELSE 2 END;
"""

MAINT_TABLE_VACUUM_STATS = """
WITH global_defaults AS (
    SELECT current_setting('autovacuum_vacuum_scale_factor')::numeric as g_vac_scale,
        current_setting('autovacuum_vacuum_threshold')::bigint as g_vac_threshold
),
table_health AS (
    SELECT s.schemaname, s.relname, pg_total_relation_size(s.relid) as total_size,
        s.n_live_tup, s.n_dead_tup,
        CASE WHEN s.n_live_tup + s.n_dead_tup > 0
            THEN ROUND(100.0 * s.n_dead_tup / (s.n_live_tup + s.n_dead_tup), 2) ELSE 0 END as dead_pct,
        s.last_vacuum, s.last_autovacuum, s.last_analyze, s.last_autoanalyze,
        s.n_mod_since_analyze, c.reloptions,
        COALESCE(c.reloptions::text LIKE '%autovacuum_enabled=false%', false) as av_disabled
    FROM pg_stat_user_tables s JOIN pg_class c ON c.oid = s.relid
    WHERE s.schemaname NOT IN ('pg_catalog', 'information_schema')
    AND pg_total_relation_size(s.relid) > 1048576
)
SELECT schemaname || '.' || relname as table_name, pg_size_pretty(total_size) as size,
    n_live_tup::text as live_rows,
    n_dead_tup::text || ' (' || dead_pct::text || '%)' as dead_rows,
    CASE WHEN reloptions IS NOT NULL THEN array_to_string(reloptions, ', ') ELSE 'Using global defaults' END as vacuum_settings,
    COALESCE(TO_CHAR(GREATEST(last_vacuum, last_autovacuum), 'YYYY-MM-DD HH24:MI'), 'Never') as last_vacuumed,
    COALESCE(TO_CHAR(GREATEST(last_analyze, last_autoanalyze), 'YYYY-MM-DD HH24:MI'), 'Never') as last_analyzed,
    CASE WHEN av_disabled THEN 'CRITICAL: Autovacuum DISABLED'
         WHEN dead_pct > 30 THEN 'CRITICAL: ' || dead_pct || '% dead tuples'
         WHEN dead_pct > 10 THEN 'WARNING: Elevated dead tuples'
         WHEN last_analyze IS NULL AND last_autoanalyze IS NULL AND n_live_tup > 1000 THEN 'WARNING: Never analyzed'
         WHEN reloptions IS NOT NULL THEN 'OK: Custom autovacuum settings'
         ELSE 'OK' END as health_status
FROM table_health CROSS JOIN global_defaults g
WHERE av_disabled OR dead_pct > 10
    OR (last_analyze IS NULL AND last_autoanalyze IS NULL AND n_live_tup > 1000)
    OR reloptions IS NOT NULL
ORDER BY CASE WHEN av_disabled THEN 0 WHEN dead_pct > 30 THEN 1 ELSE 6 END, dead_pct DESC, total_size DESC
LIMIT 50;
"""

# ============================================================================
# Section 8: OPTIMIZATION (Index, Table & Schema)
# ============================================================================

OPT_INDEX_STATISTICS = """
WITH index_stats AS (
    SELECT ns.nspname as schemaname, ci.relname as indexname, ct.relname as tablename,
        i.indisunique, i.indisprimary, i.indisvalid, i.indisready, i.indislive,
        am.amname as index_type, pg_relation_size(i.indexrelid) as index_size_bytes,
        coalesce(s.idx_scan, 0) as idx_scan,
        coalesce(s.idx_tup_read, 0) as idx_tup_read,
        coalesce(s.idx_tup_fetch, 0) as idx_tup_fetch
    FROM pg_index i JOIN pg_class ci ON ci.oid = i.indexrelid
    JOIN pg_class ct ON ct.oid = i.indrelid JOIN pg_namespace ns ON ns.oid = ct.relnamespace
    JOIN pg_am am ON ci.relam = am.oid
    LEFT JOIN pg_stat_user_indexes s ON s.indexrelid = i.indexrelid
    WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
)
SELECT schemaname as "Schema", tablename as "Table", indexname as "Index",
    pg_size_pretty(index_size_bytes) as "Size", idx_scan as "Scans",
    idx_tup_read as "Rows Read", index_type as "Type",
    CASE WHEN indisprimary THEN 'PRIMARY KEY' WHEN indisunique THEN 'UNIQUE' ELSE 'NORMAL' END as "Category",
    CASE WHEN NOT indisvalid THEN 'INVALID' WHEN idx_scan = 0 THEN 'UNUSED' WHEN idx_scan < 50 THEN 'RARELY USED' ELSE 'HEALTHY' END as "Health",
    CASE
        WHEN NOT indisvalid THEN 'CRITICAL: Invalid index - REINDEX required'
        WHEN idx_scan = 0 AND NOT indisprimary AND NOT indisunique THEN
            CASE WHEN index_size_bytes > 100*1024*1024 THEN 'CRITICAL: Large unused index - consider dropping'
                 WHEN index_size_bytes > 10*1024*1024 THEN 'WARNING: Medium unused index - review usage'
                 ELSE 'INFO: Unused index - monitor usage' END
        WHEN idx_scan < 50 AND index_size_bytes > 10*1024*1024 THEN 'WARNING: Large rarely-used index'
        ELSE 'OK'
    END as "Recommendation"
FROM index_stats
WHERE NOT indisvalid OR NOT indisready OR NOT indislive
    OR (idx_scan = 0 AND index_size_bytes > 1024*1024 AND NOT indisprimary)
    OR (idx_scan < 50 AND index_size_bytes > 10*1024*1024)
ORDER BY CASE WHEN NOT indisvalid THEN 1 WHEN idx_scan = 0 THEN 3 ELSE 4 END,
    index_size_bytes DESC LIMIT 50;
"""

OPT_TOAST_TABLES = """
WITH toast_info AS (
    SELECT n.nspname as schema_name, t.relname AS table_name, tt.relname AS toast_table_name,
        pg_total_relation_size(t.oid) AS main_table_size,
        pg_total_relation_size(tt.oid) AS toast_table_size,
        COALESCE(s.n_dead_tup, 0) as n_dead_tup, COALESCE(s.n_live_tup, 0) as n_live_tup,
        s.last_vacuum, s.last_autovacuum,
        age(t.relfrozenxid) as main_table_age, age(tt.relfrozenxid) as toast_table_age
    FROM pg_class t JOIN pg_namespace n ON n.oid = t.relnamespace
    JOIN pg_class tt ON tt.oid = t.reltoastrelid
    LEFT JOIN pg_stat_user_tables s ON s.relid = t.oid
    WHERE t.relkind = 'r' AND t.reltoastrelid != 0
    AND n.nspname NOT IN ('pg_catalog', 'information_schema')
    AND pg_total_relation_size(t.oid) > 1048576
),
critical_age_info AS (
    SELECT current_setting('autovacuum_freeze_max_age')::bigint as max_age
)
SELECT metric, value, status, description, recommendation
FROM (
    SELECT 'TOAST Health Overview' as metric,
        (SELECT COUNT(*)::text || ' tables' FROM toast_info) as value,
        CASE WHEN (SELECT COUNT(*) FROM toast_info WHERE main_table_age > (SELECT max_age * 0.8 FROM critical_age_info)) > 0 THEN 'CRITICAL'
             ELSE 'OK' END as status,
        'Overall TOAST table health assessment' as description,
        CASE WHEN (SELECT COUNT(*) FROM toast_info WHERE main_table_age > (SELECT max_age * 0.8 FROM critical_age_info)) > 0
            THEN 'CRITICAL: Immediate VACUUM FREEZE required' ELSE 'TOAST tables are healthy' END as recommendation
) metrics;
"""

OPT_LARGE_TABLE_PARTITIONING = """
WITH table_sizes AS (
    SELECT n.nspname as schema_name, c.relname as table_name, c.relkind,
        pg_total_relation_size(c.oid) as total_size,
        pg_size_pretty(pg_total_relation_size(c.oid)) as formatted_size
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
    AND c.relkind IN ('r', 'p') AND pg_total_relation_size(c.oid) > 1073741824
),
partition_details AS (
    SELECT n.nspname as schema_name, c.relname as table_name,
        CASE WHEN c.relkind = 'p' THEN 'PARTITIONED' WHEN c.relispartition THEN 'PARTITION' ELSE 'REGULAR' END as table_type,
        count(i.inhrelid) as partition_count
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    LEFT JOIN pg_inherits i ON c.oid = i.inhparent
    WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
    GROUP BY n.nspname, c.relname, c.relkind, c.relispartition
)
SELECT ts.schema_name, ts.table_name, ts.formatted_size,
    ROUND(ts.total_size::numeric / 1073741824.0, 2) as size_gb,
    COALESCE(pd.table_type, 'REGULAR') as table_type,
    COALESCE(pd.partition_count, 0) as partition_count,
    CASE
        WHEN COALESCE(pd.table_type, 'REGULAR') = 'REGULAR' AND ts.total_size > 53687091200 THEN 'HIGH PRIORITY: Consider partitioning (>50GB)'
        WHEN COALESCE(pd.table_type, 'REGULAR') = 'REGULAR' AND ts.total_size > 10737418240 THEN 'LOW PRIORITY: Evaluate partitioning (>10GB)'
        WHEN COALESCE(pd.table_type, 'REGULAR') = 'PARTITIONED' THEN 'OK: Table is properly partitioned'
        ELSE 'INFO: Monitor growth patterns'
    END as "Recommendation"
FROM table_sizes ts
LEFT JOIN partition_details pd ON ts.schema_name = pd.schema_name AND ts.table_name = pd.table_name
ORDER BY ts.total_size DESC;
"""

OPT_UNUSED_INDEXES = """
SELECT
    s.schemaname AS schema_name,
    s.relname AS table_name,
    s.indexrelname AS index_name,
    pg_size_pretty(pg_relation_size(s.indexrelid)) AS index_size,
    s.idx_scan AS index_scans,
    s.idx_tup_read AS tuples_read,
    s.idx_tup_fetch AS tuples_fetched,
    CASE
        WHEN pg_relation_size(s.indexrelid) > 104857600 THEN 'HIGH PRIORITY - Unused index > 100MB, consider dropping'
        WHEN pg_relation_size(s.indexrelid) > 10485760 THEN 'MEDIUM PRIORITY - Unused index > 10MB, consider dropping'
        ELSE 'LOW PRIORITY - Small unused index, consider dropping'
    END AS recommendation
FROM pg_stat_user_indexes s
JOIN pg_indexes i ON s.schemaname = i.schemaname
    AND s.relname = i.tablename
    AND s.indexrelname = i.indexname
WHERE s.idx_scan = 0
    AND s.indexrelname NOT LIKE '%_pkey'
    AND s.schemaname NOT IN ('pg_catalog', 'information_schema')
ORDER BY pg_relation_size(s.indexrelid) DESC
LIMIT 200;
"""

OPT_DUPLICATE_INDEXES = """
WITH index_info AS (
    SELECT
        n.nspname AS schema_name,
        ct.relname AS table_name,
        ci.relname AS index_name,
        i.indkey::text AS index_columns,
        pg_get_indexdef(i.indexrelid) AS index_definition,
        pg_size_pretty(pg_relation_size(i.indexrelid)) AS index_size,
        pg_relation_size(i.indexrelid) AS index_size_bytes,
        i.indisunique AS is_unique,
        i.indisprimary AS is_primary
    FROM pg_index i
    JOIN pg_class ci ON ci.oid = i.indexrelid
    JOIN pg_class ct ON ct.oid = i.indrelid
    JOIN pg_namespace n ON n.oid = ct.relnamespace
    WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
)
SELECT
    a.schema_name,
    a.table_name,
    a.index_name AS index_1,
    b.index_name AS index_2,
    a.index_columns AS shared_columns,
    a.index_size AS index_1_size,
    b.index_size AS index_2_size,
    CASE
        WHEN a.is_primary THEN 'Consider dropping ' || b.index_name || ' (duplicate of primary key)'
        WHEN b.is_primary THEN 'Consider dropping ' || a.index_name || ' (duplicate of primary key)'
        WHEN a.is_unique AND NOT b.is_unique THEN 'Consider dropping ' || b.index_name || ' (covered by unique index)'
        WHEN b.is_unique AND NOT a.is_unique THEN 'Consider dropping ' || a.index_name || ' (covered by unique index)'
        WHEN a.index_size_bytes >= b.index_size_bytes THEN 'Consider dropping ' || b.index_name || ' (smaller duplicate)'
        ELSE 'Consider dropping ' || a.index_name || ' (smaller duplicate)'
    END AS recommendation
FROM index_info a
JOIN index_info b ON a.schema_name = b.schema_name
    AND a.table_name = b.table_name
    AND a.index_columns = b.index_columns
    AND a.index_name < b.index_name
ORDER BY a.schema_name, a.table_name, a.index_name
LIMIT 200;
"""

OPT_IDLE_CONNECTIONS = """
SELECT
    pid,
    usename AS username,
    datname AS database,
    COALESCE(NULLIF(application_name, ''), '<unnamed>') AS application_name,
    state,
    CASE
        WHEN state_change IS NOT NULL THEN
            EXTRACT(EPOCH FROM (now() - state_change))::integer || ' seconds'
        ELSE 'unknown'
    END AS idle_duration,
    COALESCE(client_addr::text, 'local') AS client_address,
    CASE
        WHEN state = 'idle' AND state_change < now() - interval '1 hour' THEN 'CRITICAL - Idle > 1 hour, strongly consider terminating'
        WHEN state = 'idle' AND state_change < now() - interval '15 minutes' THEN 'WARNING - Idle > 15 minutes, consider terminating'
        WHEN state = 'idle' AND state_change < now() - interval '5 minutes' THEN 'INFO - Idle > 5 minutes, monitor'
        ELSE 'OK - Recently active'
    END AS recommendation
FROM pg_stat_activity
WHERE state = 'idle'
    AND pid != pg_backend_pid()
ORDER BY state_change ASC NULLS FIRST
LIMIT 200;
"""

OPT_OVERSIZED_DATA_TYPES = """
SELECT
    table_schema AS schema_name,
    table_name,
    column_name,
    data_type ||
        CASE
            WHEN character_maximum_length IS NOT NULL THEN '(' || character_maximum_length || ')'
            WHEN numeric_precision IS NOT NULL AND data_type IN ('numeric', 'decimal') THEN '(' || numeric_precision || ',' || COALESCE(numeric_scale, 0) || ')'
            ELSE ''
        END AS current_type,
    CASE
        WHEN data_type = 'text' THEN 'Consider varchar(n) with appropriate length limit if max length is known'
        WHEN data_type = 'bigint' THEN 'Consider integer if values fit within -2147483648 to 2147483647 range'
        WHEN data_type IN ('numeric', 'decimal') AND numeric_precision IS NULL THEN 'Consider adding precision and scale to avoid arbitrary precision overhead'
        WHEN data_type = 'character varying' AND character_maximum_length IS NULL THEN 'Consider adding a length limit to varchar'
        WHEN data_type = 'double precision' THEN 'Consider real (4 bytes) if double precision (8 bytes) is not needed'
        WHEN data_type = 'timestamp with time zone' AND column_name LIKE '%date%' THEN 'Consider date type if time component is not needed'
        ELSE 'Review data type sizing'
    END AS recommendation
FROM information_schema.columns
WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
    AND (
        data_type = 'text'
        OR data_type = 'bigint'
        OR (data_type IN ('numeric', 'decimal') AND numeric_precision IS NULL)
        OR (data_type = 'character varying' AND character_maximum_length IS NULL)
        OR data_type = 'double precision'
        OR (data_type = 'timestamp with time zone' AND column_name LIKE '%date%')
    )
ORDER BY table_schema, table_name, ordinal_position
LIMIT 200;
"""

OPT_UNUSED_TABLES = """
SELECT
    schemaname AS schema_name,
    relname AS table_name,
    pg_size_pretty(pg_total_relation_size(relid)) AS total_size,
    n_live_tup AS estimated_row_count,
    COALESCE(last_vacuum::text, 'never') AS last_vacuum,
    COALESCE(last_autovacuum::text, 'never') AS last_autovacuum,
    COALESCE(last_analyze::text, 'never') AS last_analyze,
    COALESCE(last_autoanalyze::text, 'never') AS last_autoanalyze,
    seq_scan + idx_scan AS total_scans,
    CASE
        WHEN pg_total_relation_size(relid) > 104857600 THEN 'HIGH PRIORITY - Unused table > 100MB, consider archiving or dropping'
        WHEN pg_total_relation_size(relid) > 10485760 THEN 'MEDIUM PRIORITY - Unused table > 10MB, consider archiving or dropping'
        ELSE 'LOW PRIORITY - Small unused table, verify if still needed'
    END AS recommendation
FROM pg_stat_user_tables
WHERE seq_scan + idx_scan = 0
    AND schemaname NOT IN ('pg_catalog', 'information_schema')
ORDER BY pg_total_relation_size(relid) DESC
LIMIT 200;
"""

OPT_FK_WITHOUT_INDEXES = """
SELECT
    c.conrelid::regclass AS table_name,
    c.conname AS constraint_name,
    pg_get_constraintdef(c.oid) AS constraint_definition,
    'CREATE INDEX ON ' || c.conrelid::regclass || ' (' ||
    string_agg(a.attname, ', ' ORDER BY x.n) || ');' AS recommended_index,
    CASE
        WHEN pg_total_relation_size(c.conrelid) > 1073741824 THEN 'CRITICAL: Large table without FK index'
        WHEN pg_total_relation_size(c.conrelid) > 104857600 THEN 'WARNING: Medium table without FK index'
        ELSE 'INFO: Small table without FK index'
    END as severity
FROM pg_constraint c
CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS x(attnum, n)
JOIN pg_attribute a ON a.attnum = x.attnum AND a.attrelid = c.conrelid
WHERE c.contype = 'f'
  AND NOT EXISTS (
      SELECT 1 FROM pg_index i
      WHERE i.indrelid = c.conrelid
        AND c.conkey::int[] <@ i.indkey::int[]
  )
GROUP BY c.conrelid, c.conname, c.oid
ORDER BY pg_total_relation_size(c.conrelid) DESC
LIMIT 200;
"""

OPT_TRIGGER_ANALYSIS = """
SELECT
    tgrelid::regclass AS table_name,
    tgname AS trigger_name,
    CASE tgtype & 66
        WHEN 2 THEN 'BEFORE'
        WHEN 64 THEN 'INSTEAD OF'
        ELSE 'AFTER'
    END AS timing,
    CASE
        WHEN tgtype & 4 > 0 AND tgtype & 8 > 0 AND tgtype & 16 > 0 THEN 'INSERT/UPDATE/DELETE'
        WHEN tgtype & 4 > 0 AND tgtype & 8 > 0 THEN 'INSERT/UPDATE'
        WHEN tgtype & 4 > 0 AND tgtype & 16 > 0 THEN 'INSERT/DELETE'
        WHEN tgtype & 8 > 0 AND tgtype & 16 > 0 THEN 'UPDATE/DELETE'
        WHEN tgtype & 4 > 0 THEN 'INSERT'
        WHEN tgtype & 8 > 0 THEN 'UPDATE'
        WHEN tgtype & 16 > 0 THEN 'DELETE'
        WHEN tgtype & 32 > 0 THEN 'TRUNCATE'
        ELSE 'UNKNOWN'
    END AS event,
    CASE
        WHEN tgtype & 1 > 0 THEN 'FOR EACH ROW'
        ELSE 'FOR EACH STATEMENT'
    END AS granularity,
    p.proname AS function_name,
    CASE tgenabled
        WHEN 'O' THEN 'ENABLED (origin)'
        WHEN 'D' THEN 'DISABLED'
        WHEN 'R' THEN 'ENABLED (replica)'
        WHEN 'A' THEN 'ENABLED (always)'
        ELSE 'ENABLED'
    END AS status,
    CASE
        WHEN tgenabled = 'D' THEN 'INFO: Trigger is disabled - verify if intentional'
        WHEN tgtype & 1 = 0 THEN 'INFO: Statement-level trigger'
        WHEN (tgtype & 4 > 0 AND tgtype & 8 > 0 AND tgtype & 16 > 0) THEN 'WARNING: Trigger fires on all DML events - may impact performance'
        WHEN (tgtype & 66) = 2 AND tgtype & 1 > 0 THEN 'WARNING: BEFORE ROW trigger - runs for every row, monitor performance'
        ELSE 'OK'
    END AS recommendation
FROM pg_trigger t
JOIN pg_proc p ON p.oid = t.tgfoid
WHERE NOT t.tgisinternal
ORDER BY tgrelid::regclass::text, tgname
LIMIT 200;
"""

OPT_SEQ_SCAN_CANDIDATES = """
SELECT
    schemaname || '.' || relname AS table_name,
    pg_size_pretty(pg_total_relation_size(relid)) AS table_size,
    seq_scan,
    idx_scan,
    seq_tup_read,
    CASE WHEN seq_scan > 0
        THEN ROUND(seq_tup_read::numeric / seq_scan, 0)
        ELSE 0
    END AS avg_rows_per_seq_scan,
    CASE
        WHEN pg_total_relation_size(relid) > 1073741824 AND COALESCE(idx_scan, 0) = 0 THEN 'CRITICAL'
        WHEN seq_scan > 10000 AND COALESCE(idx_scan, 0) < seq_scan * 0.01 THEN 'CRITICAL'
        WHEN seq_scan > 1000 AND COALESCE(idx_scan, 0) < seq_scan * 0.1 THEN 'WARNING'
        ELSE 'INFO'
    END AS severity,
    CASE
        WHEN pg_total_relation_size(relid) > 1073741824 AND COALESCE(idx_scan, 0) = 0
            THEN 'CRITICAL: Large table (' || pg_size_pretty(pg_total_relation_size(relid)) || ') with zero index scans - add appropriate indexes immediately'
        WHEN seq_scan > 10000 AND COALESCE(idx_scan, 0) < seq_scan * 0.01
            THEN 'High sequential scan ratio - analyze query patterns and add covering indexes'
        WHEN seq_scan > 1000 AND COALESCE(idx_scan, 0) < seq_scan * 0.1
            THEN 'Sequential scans dominate - review WHERE clauses and consider adding indexes'
        ELSE 'Monitor sequential scan growth'
    END AS recommendation
FROM pg_stat_user_tables
WHERE pg_total_relation_size(relid) > 10485760
    AND seq_scan > 1000
    AND COALESCE(idx_scan, 0) < seq_scan * 0.1
ORDER BY seq_tup_read DESC
LIMIT 50;
"""

# ============================================================================
# Section 9: HEALTH SUMMARY (Findings & Actions)
# ============================================================================

SUMMARY_CORE_HEALTH = """
WITH health_metrics AS (
    SELECT ROUND((COUNT(*)::numeric / GREATEST(current_setting('max_connections')::integer, 1) * 100), 1) as conn_usage_pct,
        current_setting('max_connections')::integer as max_connections, COUNT(*) as current_connections
    FROM pg_stat_activity WHERE pid != pg_backend_pid()
),
xid_metrics AS (
    SELECT ROUND((age(datfrozenxid)::numeric / GREATEST(current_setting('autovacuum_freeze_max_age')::numeric, 1) * 100), 1) as xid_age_pct,
        age(datfrozenxid) as xid_age
    FROM pg_database WHERE datname = current_database()
),
cache_metrics AS (
    SELECT CASE WHEN blks_hit + blks_read = 0 THEN 100.0
        ELSE ROUND((blks_hit::numeric / GREATEST(blks_hit + blks_read, 1) * 100), 1) END as cache_hit_ratio
    FROM pg_stat_database WHERE datname = current_database()
),
query_metrics AS (
    SELECT COUNT(*) FILTER (WHERE state = 'active' AND query_start IS NOT NULL AND EXTRACT(EPOCH FROM (now() - query_start)) > 300) as long_queries,
        COUNT(*) FILTER (WHERE state = 'active') as active_queries,
        COUNT(*) FILTER (WHERE wait_event_type IS NOT NULL AND wait_event_type != 'Activity') as waiting_queries
    FROM pg_stat_activity WHERE pid != pg_backend_pid()
),
vacuum_metrics AS (
    SELECT COUNT(*) FILTER (WHERE pg_total_relation_size(relid) > 1048576 AND n_live_tup + n_dead_tup > 0
        AND n_dead_tup::numeric / (n_live_tup + n_dead_tup) > 0.2) as tables_needing_vacuum,
        COUNT(*) as total_tables
    FROM pg_stat_user_tables
),
lock_metrics AS (
    SELECT COUNT(*) FILTER (WHERE NOT granted) as blocked_queries
    FROM pg_locks l LEFT JOIN pg_stat_activity a ON l.pid = a.pid WHERE a.pid != pg_backend_pid()
)
SELECT check_category, current_value, status, recommendation
FROM (
    SELECT 'Connection Usage' as check_category,
        conn_usage_pct::text || '% (' || current_connections || '/' || max_connections || ')' as current_value,
        CASE WHEN conn_usage_pct > 85 THEN 'CRITICAL' WHEN conn_usage_pct > 70 THEN 'WARNING' ELSE 'OK' END as status,
        CASE WHEN conn_usage_pct > 85 THEN 'URGENT: Implement connection pooling immediately'
             WHEN conn_usage_pct > 70 THEN 'Consider connection pooling'
             ELSE 'Connection usage is healthy' END as recommendation,
        1 as priority FROM health_metrics
    UNION ALL
    SELECT 'Transaction ID Age', xid_age_pct::text || '% (' || xid_age::text || ' transactions)',
        CASE WHEN xid_age_pct > 80 THEN 'EMERGENCY' WHEN xid_age_pct > 50 THEN 'CRITICAL' WHEN xid_age_pct > 25 THEN 'WARNING' ELSE 'OK' END,
        CASE WHEN xid_age_pct > 80 THEN 'EMERGENCY: XID wraparound imminent - immediate VACUUM FREEZE required'
             WHEN xid_age_pct > 50 THEN 'CRITICAL: Schedule immediate VACUUM FREEZE'
             ELSE 'Transaction ID age is healthy' END,
        CASE WHEN xid_age_pct > 80 THEN 0 ELSE 4 END FROM xid_metrics
    UNION ALL
    SELECT 'Cache Hit Ratio', cache_hit_ratio::text || '%',
        CASE WHEN cache_hit_ratio < 85 THEN 'WARNING' WHEN cache_hit_ratio < 95 THEN 'INFO' ELSE 'OK' END,
        CASE WHEN cache_hit_ratio < 85 THEN 'Poor cache performance - increase shared_buffers'
             ELSE 'Cache performance is optimal' END,
        CASE WHEN cache_hit_ratio < 85 THEN 2 ELSE 4 END FROM cache_metrics
    UNION ALL
    SELECT 'Query Performance',
        long_queries::text || ' long queries (>5min), ' || active_queries::text || ' active, ' || waiting_queries::text || ' waiting',
        CASE WHEN long_queries > 5 THEN 'CRITICAL' WHEN long_queries > 0 THEN 'WARNING' ELSE 'OK' END,
        CASE WHEN long_queries > 5 THEN 'Multiple long-running queries - investigate immediately'
             WHEN long_queries > 0 THEN 'Long-running queries detected - review and optimize'
             ELSE 'Query performance is healthy' END,
        CASE WHEN long_queries > 5 THEN 1 ELSE 4 END FROM query_metrics
    UNION ALL
    SELECT 'Vacuum Status', tables_needing_vacuum::text || ' of ' || total_tables::text || ' tables need vacuum',
        CASE WHEN tables_needing_vacuum::numeric / GREATEST(total_tables, 1) > 0.5 THEN 'CRITICAL'
             WHEN tables_needing_vacuum > 0 THEN 'WARNING' ELSE 'OK' END,
        CASE WHEN tables_needing_vacuum::numeric / GREATEST(total_tables, 1) > 0.5 THEN 'Over half of tables need vacuum'
             WHEN tables_needing_vacuum > 0 THEN 'Schedule VACUUM for tables with high dead tuple ratio'
             ELSE 'All tables have healthy tuple ratios' END,
        CASE WHEN tables_needing_vacuum > 0 THEN 2 ELSE 4 END FROM vacuum_metrics
    UNION ALL
    SELECT 'Lock Contention', blocked_queries::text || ' blocked queries',
        CASE WHEN blocked_queries > 5 THEN 'CRITICAL' WHEN blocked_queries > 0 THEN 'WARNING' ELSE 'OK' END,
        CASE WHEN blocked_queries > 5 THEN 'Severe lock contention - investigate immediately'
             WHEN blocked_queries > 0 THEN 'Lock contention detected'
             ELSE 'No significant lock contention' END,
        CASE WHEN blocked_queries > 5 THEN 1 ELSE 4 END FROM lock_metrics
) health_summary ORDER BY priority, check_category;
"""

SUMMARY_CRITICAL_SYSTEM = """
WITH disk_space_metrics AS (
    SELECT pg_database_size(current_database()) as current_db_size
),
lock_contention AS (
    SELECT count(*) as waiting_session_count,
        max(COALESCE(EXTRACT(EPOCH FROM (now() - a.query_start)), 0)) as max_wait_time_seconds,
        COALESCE(l.relation::regclass::text, 'transaction') as locked_table
    FROM pg_locks l JOIN pg_stat_activity a ON l.pid = a.pid
    WHERE NOT l.granted GROUP BY l.relation ORDER BY count(*) DESC LIMIT 5
),
lock_summary AS (
    SELECT COALESCE(SUM(waiting_session_count), 0) as total_waiting,
        COALESCE(MAX(max_wait_time_seconds), 0) as max_wait,
        COALESCE((SELECT locked_table FROM lock_contention ORDER BY waiting_session_count DESC LIMIT 1), 'N/A') as top_locked_table
    FROM lock_contention
)
SELECT check_category, current_value, status, recommendation
FROM (
    SELECT 'Database Size' as check_category, pg_size_pretty(current_db_size) as current_value,
        CASE WHEN current_db_size::numeric > (100 * power(1024, 3))::numeric THEN 'WARNING' ELSE 'INFO' END as status,
        CASE WHEN current_db_size::numeric > (100 * power(1024, 3))::numeric THEN 'Large database - ensure proper backup strategy'
             ELSE 'Database size is within normal range' END as recommendation
    FROM disk_space_metrics
    UNION ALL
    SELECT 'Lock Contention',
        CASE WHEN total_waiting > 0 THEN total_waiting::text || ' sessions waiting for ' || top_locked_table
             ELSE 'No lock contention detected' END,
        CASE WHEN total_waiting > 3 THEN 'CRITICAL' WHEN total_waiting > 0 THEN 'WARNING' ELSE 'OK' END,
        CASE WHEN total_waiting > 3 THEN 'Severe lock contention - investigate immediately'
             ELSE 'No lock contention issues' END
    FROM lock_summary
) critical_metrics
ORDER BY CASE status WHEN 'CRITICAL' THEN 1 WHEN 'WARNING' THEN 2 ELSE 4 END;
"""

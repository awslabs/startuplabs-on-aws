#!/usr/bin/env python3
"""
Invasive data collection script for PostgreSQL WAL review.
Extends non-invasive collection with direct database queries and PGPerfStatsSnapper metrics.
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional

import boto3
import psycopg2
from psycopg2.extras import RealDictCursor
from botocore.exceptions import ClientError, NoCredentialsError

from non_invasive_collector import NonInvasiveCollector


class InvasiveCollector(NonInvasiveCollector):
    def __init__(self, region: str, db_host: str, db_user: str, db_password: Optional[str] = None, 
                 db_port: int = 5432, db_name: str = 'postgres', output_dir: str = "./data",
                 db_secret_arn: Optional[str] = None):
        super().__init__(region, output_dir)
        self.rds_client = boto3.client('rds', region_name=region)
        self.db_host = db_host
        self.db_user = db_user
        self.db_password = db_password
        self.db_port = db_port
        self.db_name = db_name
        self.db_secret_arn = db_secret_arn
        
        # Test database connection
        self._test_connection()

    def _test_connection(self):
        """Test database connection."""
        try:
            conn = self._get_db_connection()
            conn.close()
            self.logger.info("Database connection test successful")
        except Exception as e:
            self.logger.error(f"Database connection test failed: {e}")
            raise

    def _get_db_connection(self):
        """Get database connection."""
        return psycopg2.connect(
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
            user=self.db_user,
            password=self.db_password,
            sslmode='verify-full',
            sslrootcert='/certs/global-bundle.pem'
        )

    def collect_database_statistics(self) -> Dict[str, Any]:
        """Collect database-level statistics."""
        try:
            self.logger.info("Collecting database statistics")
            
            conn = self._get_db_connection()
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            
            stats_data = {}
            
            # Database size and object counts
            cursor.execute("""
                SELECT 
                    datname,
                    pg_size_pretty(pg_database_size(datname)) as size,
                    pg_database_size(datname) as size_bytes
                FROM pg_database 
                WHERE datistemplate = false;
            """)
            stats_data['database_sizes'] = [dict(row) for row in cursor.fetchall()]
            
            # Table statistics
            cursor.execute("""
                SELECT 
                    schemaname,
                    tablename,
                    attname,
                    n_distinct,
                    correlation
                FROM pg_stats 
                WHERE schemaname NOT IN ('information_schema', 'pg_catalog')
                ORDER BY schemaname, tablename, attname;
            """)
            stats_data['table_statistics'] = [dict(row) for row in cursor.fetchall()]
            
            # Index usage statistics
            cursor.execute("""
                SELECT 
                    schemaname,
                    relname as tablename,
                    indexrelname as indexname,
                    idx_tup_read,
                    idx_tup_fetch,
                    idx_scan
                FROM pg_stat_user_indexes
                ORDER BY schemaname, relname, indexrelname;
            """)
            stats_data['index_usage'] = [dict(row) for row in cursor.fetchall()]
            
            # Table I/O statistics
            cursor.execute("""
                SELECT 
                    schemaname,
                    relname,
                    seq_scan,
                    seq_tup_read,
                    idx_scan,
                    idx_tup_fetch,
                    n_tup_ins,
                    n_tup_upd,
                    n_tup_del,
                    n_tup_hot_upd,
                    n_live_tup,
                    n_dead_tup,
                    vacuum_count,
                    autovacuum_count,
                    analyze_count,
                    autoanalyze_count
                FROM pg_stat_user_tables
                ORDER BY schemaname, relname;
            """)
            stats_data['table_io'] = [dict(row) for row in cursor.fetchall()]
            
            # Connection and activity statistics
            cursor.execute("""
                SELECT 
                    state,
                    COUNT(*) as count
                FROM pg_stat_activity 
                GROUP BY state;
            """)
            stats_data['connection_states'] = [dict(row) for row in cursor.fetchall()]
            
            # Lock statistics
            cursor.execute("""
                SELECT 
                    mode,
                    COUNT(*) as count
                FROM pg_locks 
                GROUP BY mode;
            """)
            stats_data['lock_modes'] = [dict(row) for row in cursor.fetchall()]
            
            # Background writer statistics (handle version differences)
            try:
                # Get PostgreSQL version to determine which columns are available
                cursor.execute("SELECT version();")
                version_info = cursor.fetchone()['version']
                
                # Extract major version number
                import re
                version_match = re.search(r'PostgreSQL (\d+)', version_info)
                major_version = int(version_match.group(1)) if version_match else 0
                
                if major_version >= 17:
                    # PostgreSQL 17+: checkpoint stats moved to pg_stat_checkpointer
                    cursor.execute("""
                        SELECT 
                            buffers_clean,
                            maxwritten_clean,
                            buffers_alloc,
                            stats_reset
                        FROM pg_stat_bgwriter;
                    """)
                    bgwriter_result = cursor.fetchone()
                    bgwriter_data = dict(bgwriter_result) if bgwriter_result else {}
                    
                    # Try to get checkpoint stats from pg_stat_checkpointer
                    try:
                        cursor.execute("""
                            SELECT 
                                num_timed as checkpoints_timed,
                                num_requested as checkpoints_req,
                                write_time as checkpoint_write_time,
                                sync_time as checkpoint_sync_time,
                                buffers_written as buffers_checkpoint,
                                stats_reset as checkpointer_stats_reset
                            FROM pg_stat_checkpointer;
                        """)
                        checkpointer_result = cursor.fetchone()
                        if checkpointer_result:
                            bgwriter_data.update(dict(checkpointer_result))
                    except Exception:
                        self.logger.warning("pg_stat_checkpointer not available")
                    
                    stats_data['bgwriter'] = bgwriter_data
                else:
                    # PostgreSQL < 17: all stats in pg_stat_bgwriter
                    cursor.execute("""
                        SELECT 
                            checkpoints_timed,
                            checkpoints_req,
                            checkpoint_write_time,
                            checkpoint_sync_time,
                            buffers_checkpoint,
                            buffers_clean,
                            maxwritten_clean,
                            buffers_backend,
                            buffers_backend_fsync,
                            buffers_alloc,
                            stats_reset
                        FROM pg_stat_bgwriter;
                    """)
                    bgwriter_result = cursor.fetchone()
                    stats_data['bgwriter'] = dict(bgwriter_result) if bgwriter_result else {}
                    
            except Exception as e:
                self.logger.warning(f"Could not collect bgwriter stats: {str(e)}")
                # Fallback to minimal columns that exist in all versions
                try:
                    cursor.execute("""
                        SELECT 
                            buffers_clean,
                            buffers_alloc
                        FROM pg_stat_bgwriter;
                    """)
                    bgwriter_result = cursor.fetchone()
                    stats_data['bgwriter'] = dict(bgwriter_result) if bgwriter_result else {}
                except Exception as e2:
                    self.logger.warning(f"Could not collect bgwriter stats with fallback: {str(e2)}")
                    stats_data['bgwriter'] = {'error': str(e2)}
            
            cursor.close()
            conn.close()
            
            return stats_data
            
        except Exception as e:
            self.logger.error(f"Error collecting database statistics: {e}")
            raise

    def collect_configuration_parameters(self, database_info: Dict[str, Any] = None) -> Dict[str, Any]:
        """Collect PostgreSQL configuration parameters via direct DB connection.
        
        The database_info parameter is accepted for compatibility with the
        parent class (NonInvasiveCollector) but is not used — the invasive
        collector already has a DB connection and queries pg_settings directly.
        """
        try:
            self.logger.info("Collecting configuration parameters")
            
            conn = self._get_db_connection()
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            
            # Get all configuration parameters
            cursor.execute("""
                SELECT 
                    name,
                    setting,
                    unit,
                    category,
                    short_desc,
                    context,
                    vartype,
                    source,
                    min_val,
                    max_val,
                    boot_val,
                    reset_val
                FROM pg_settings
                ORDER BY category, name;
            """)
            
            parameters = [dict(row) for row in cursor.fetchall()]
            
            cursor.close()
            conn.close()
            
            return {
                'parameters': parameters,
                'collection_timestamp': datetime.utcnow().isoformat()
            }
            
        except Exception as e:
            self.logger.error(f"Error collecting configuration parameters: {e}")
            raise

    def collect_schema_information(self) -> Dict[str, Any]:
        """Collect schema and object information.

        Excludes extension-owned tables/views (e.g., pg_stat_statements,
        pg_buffercache) by joining pg_depend + pg_extension.
        """
        try:
            self.logger.info("Collecting schema information")
            
            conn = self._get_db_connection()
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            
            schema_data = {}
            
            # Tables and their sizes — exclude extension-owned objects
            cursor.execute("""
                SELECT 
                    t.schemaname,
                    t.tablename,
                    pg_size_pretty(pg_total_relation_size(t.schemaname||'.'||t.tablename)) as total_size,
                    pg_total_relation_size(t.schemaname||'.'||t.tablename) as total_size_bytes,
                    pg_size_pretty(pg_relation_size(t.schemaname||'.'||t.tablename)) as table_size,
                    pg_relation_size(t.schemaname||'.'||t.tablename) as table_size_bytes,
                    s.n_live_tup as estimated_rows,
                    s.n_dead_tup as dead_rows,
                    (SELECT count(*) FROM information_schema.columns c
                     WHERE c.table_schema = t.schemaname AND c.table_name = t.tablename) as column_count,
                    (SELECT count(*) FROM pg_indexes i
                     WHERE i.schemaname = t.schemaname AND i.tablename = t.tablename) as index_count
                FROM pg_tables t
                LEFT JOIN pg_stat_user_tables s ON t.schemaname = s.schemaname AND t.tablename = s.relname
                WHERE t.schemaname NOT IN ('information_schema', 'pg_catalog')
                  AND NOT EXISTS (
                    SELECT 1 FROM pg_depend d
                    JOIN pg_extension e ON d.refobjid = e.oid
                    JOIN pg_class c ON d.objid = c.oid
                    WHERE c.relname = t.tablename
                  )
                ORDER BY pg_total_relation_size(t.schemaname||'.'||t.tablename) DESC;
            """)
            schema_data['tables'] = [dict(row) for row in cursor.fetchall()]
            
            # Indexes and their sizes
            cursor.execute("""
                SELECT 
                    schemaname,
                    tablename,
                    indexname,
                    pg_size_pretty(pg_relation_size(schemaname||'.'||indexname)) as index_size,
                    pg_relation_size(schemaname||'.'||indexname) as index_size_bytes
                FROM pg_indexes 
                WHERE schemaname NOT IN ('information_schema', 'pg_catalog')
                  AND tablename NOT IN (
                    SELECT c.relname FROM pg_class c
                    JOIN pg_depend d ON c.oid = d.objid
                    JOIN pg_extension e ON d.refobjid = e.oid
                  )
                ORDER BY pg_relation_size(schemaname||'.'||indexname) DESC;
            """)
            schema_data['indexes'] = [dict(row) for row in cursor.fetchall()]
            
            # Column information
            cursor.execute("""
                SELECT 
                    table_schema,
                    table_name,
                    column_name,
                    data_type,
                    is_nullable,
                    column_default
                FROM information_schema.columns
                WHERE table_schema NOT IN ('information_schema', 'pg_catalog')
                  AND table_name NOT IN (
                    SELECT c.relname FROM pg_class c
                    JOIN pg_depend d ON c.oid = d.objid
                    JOIN pg_extension e ON d.refobjid = e.oid
                  )
                ORDER BY table_schema, table_name, ordinal_position;
            """)
            schema_data['columns'] = [dict(row) for row in cursor.fetchall()]
            
            cursor.close()
            conn.close()
            
            return schema_data
            
        except Exception as e:
            self.logger.error(f"Error collecting schema information: {e}")
            raise

    def collect_query_performance(self) -> Dict[str, Any]:
        """Collect query performance data using pg_stat_statements if available."""
        try:
            self.logger.info("Collecting query performance data")
            
            conn = self._get_db_connection()
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            
            # Check if pg_stat_statements is available
            cursor.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements'
                ) as has_pg_stat_statements;
            """)
            
            has_pg_stat_statements = cursor.fetchone()['has_pg_stat_statements']
            
            query_data = {
                'has_pg_stat_statements': has_pg_stat_statements,
                'collection_timestamp': datetime.utcnow().isoformat()
            }
            
            if has_pg_stat_statements:
                # Get PostgreSQL version to determine column names
                cursor.execute("SHOW server_version_num;")
                version_num = int(cursor.fetchone()['server_version_num'])
                
                # PostgreSQL 13+ uses total_exec_time, older versions use total_time
                # Whitelist the column name to prevent any SQL injection
                time_column = 'total_exec_time' if version_num >= 130000 else 'total_time'
                assert time_column in ('total_exec_time', 'total_time'), "Unexpected time column"

                # Column name is whitelisted above via assert; psycopg2 does not support bind params for identifiers.
                # nosec B608 - time_column is constrained to one of two hardcoded strings, not user input.
                top_by_time_sql = (
                    "SELECT query, calls,"
                    f" {time_column} as total_time,"
                    f" {time_column} / calls as mean_time,"
                    " min_exec_time as min_time, max_exec_time as max_time,"
                    " stddev_exec_time as stddev_time, rows,"
                    " 100.0 * shared_blks_hit / nullif(shared_blks_hit + shared_blks_read, 0) AS hit_percent"
                    f" FROM pg_stat_statements ORDER BY {time_column} DESC LIMIT 50;"  # nosec B608
                )
                cursor.execute(top_by_time_sql)
                query_data['top_queries_by_time'] = [dict(row) for row in cursor.fetchall()]

                top_by_calls_sql = (
                    "SELECT query, calls,"
                    f" {time_column} as total_time,"
                    f" {time_column} / calls as mean_time"
                    " FROM pg_stat_statements ORDER BY calls DESC LIMIT 50;"  # nosec B608
                )
                cursor.execute(top_by_calls_sql)
                query_data['top_queries_by_calls'] = [dict(row) for row in cursor.fetchall()]

                slowest_sql = (
                    "SELECT query, calls,"
                    f" {time_column} as total_time,"
                    f" {time_column} / calls as mean_time"
                    f" FROM pg_stat_statements ORDER BY {time_column} / calls DESC LIMIT 50;"  # nosec B608
                )
                cursor.execute(slowest_sql)
                query_data['slowest_queries'] = [dict(row) for row in cursor.fetchall()]
            
            cursor.close()
            conn.close()
            
            return query_data
            
        except Exception as e:
            self.logger.error(f"Error collecting query performance data: {e}")
            return {'error': str(e)}

    def collect_pg_health_insights(self, skip_security: bool = False) -> Dict[str, Any]:
        """Collect comprehensive PostgreSQL health insights across 9 sections.
        
        Args:
            skip_security: If True, skip security-related queries (user roles, privileges,
                          SSL, passwords, sensitive columns, RLS, audit config).
        
        Uses validated SQL queries from pg_health_queries.py. Version-aware branching
        for PG17+ checkpoint stats and pg_stat_statements column differences.
        """
        try:
            from scripts.pg_health_queries import (
                OVERVIEW_SERVER_INFO, OVERVIEW_EXTENSION_INVENTORY,
                CONFIG_PARAMETER_HEALTH, CONFIG_USER_ROLES, CONFIG_PRIVILEGE_AUDIT,
                CONFIG_SSL_CONNECTIONS, CONFIG_PASSWORD_SECURITY, CONFIG_SENSITIVE_COLUMNS,
                CONFIG_RLS_STATUS, CONFIG_AUDIT_CONFIG, CONFIG_DB_ROLE_OVERRIDES,
                ACTIVITY_CONNECTION_SUMMARY, ACTIVITY_POOLING_DETECTION,
                ACTIVITY_APP_IDENTIFICATION, ACTIVITY_WORKLOAD_CHARACTERIZATION,
                ACTIVITY_CLIENT_ANALYSIS, ACTIVITY_CONNECTION_CHURN,
                REPLICATION_INFO, REPLICATION_LAG, REPLICATION_SYNC_CONFIG,
                REPLICATION_LOGICAL_SLOTS,
                DATA_DATABASE_SIZES, DATA_WAL_DIRECTORY,
                PERF_CHECKPOINT_STATS_PG17, PERF_CHECKPOINT_STATS_PRE17,
                PERF_BGWRITER_STATS, PERF_AUTOVACUUM_UTILIZATION,
                PERF_POOLING_EFFICIENCY, PERF_BUFFER_HIT_RATIOS, PERF_LOCK_TREE,
                PERF_IO_STATS, PERF_TRANSACTION_STATS, PERF_FUNCTION_PERFORMANCE,
                PERF_WAIT_EVENT_ANALYSIS, PERF_SESSION_DURATION,
                PERF_PREPARED_STATEMENTS, PERF_TEMP_FILE_QUERIES,
                PERF_QUERY_ANALYSIS_PG17, PERF_QUERY_ANALYSIS_PRE17,
                MAINT_DATABASE_INTEGRITY, MAINT_SEQUENCE_EXHAUSTION,
                MAINT_CONSTRAINT_VALIDATION, MAINT_MATERIALIZED_VIEWS,
                MAINT_TABLE_VACUUM_STATS,
                OPT_INDEX_STATISTICS, OPT_TOAST_TABLES, OPT_LARGE_TABLE_PARTITIONING,
                OPT_UNUSED_INDEXES, OPT_DUPLICATE_INDEXES, OPT_IDLE_CONNECTIONS,
                OPT_OVERSIZED_DATA_TYPES, OPT_UNUSED_TABLES, OPT_FK_WITHOUT_INDEXES,
                OPT_TRIGGER_ANALYSIS, OPT_SEQ_SCAN_CANDIDATES,
                SUMMARY_CORE_HEALTH, SUMMARY_CRITICAL_SYSTEM,
            )
        except ImportError:
            # Fallback: try relative import
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "pg_health_queries",
                os.path.join(os.path.dirname(__file__), "pg_health_queries.py")
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            # Pull all constants from the module
            for name in dir(mod):
                if name.isupper():
                    locals()[name] = getattr(mod, name)

        self.logger.info("Collecting PostgreSQL health insights (9 sections)")
        
        conn = self._get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        # Detect version and extensions once
        cursor.execute("SELECT current_setting('server_version_num')::int AS ver")
        pg_version = cursor.fetchone()['ver']
        
        cursor.execute("SELECT extname FROM pg_extension")
        extensions = {row['extname'] for row in cursor.fetchall()}
        has_pg_stat_statements = 'pg_stat_statements' in extensions
        
        # Determine deployment type from the API-detected db type.
        # Do NOT use hostname heuristics — both Aurora and RDS Multi-AZ DB Clusters
        # use the .cluster-xxx endpoint format, making hostname patterns unreliable.
        _detected = getattr(self, '_detected_db_type', '')
        if _detected == 'aurora_cluster':
            deployment_type = 'Aurora'
        elif _detected == 'rds_multiaz_cluster':
            deployment_type = 'RDS Multi-AZ DB Cluster'
        else:
            deployment_type = 'RDS'
        
        def _run(sql, **kwargs):
            """Execute query safely, return list of dicts or [] on error."""
            try:
                formatted = sql.format(**kwargs) if kwargs else sql
                cursor.execute(formatted)
                return [dict(row) for row in cursor.fetchall()]
            except Exception as e:
                self.logger.warning(f"pg_health query failed: {e}")
                try:
                    conn.rollback()
                except Exception:
                    pass
                return []
        
        result = {
            'collection_timestamp': datetime.utcnow().isoformat(),
            'pg_version': pg_version,
            'deployment_type': deployment_type,
            'has_pg_stat_statements': has_pg_stat_statements,
        }
        
        # Section 1: Overview
        result['overview'] = {
            'server_info': _run(OVERVIEW_SERVER_INFO),
            'extension_inventory': _run(OVERVIEW_EXTENSION_INVENTORY),
        }
        
        # Section 2: Configuration
        if skip_security:
            result['configuration'] = {
                'parameter_health': _run(CONFIG_PARAMETER_HEALTH, deployment_type=deployment_type),
                'db_role_overrides': _run(CONFIG_DB_ROLE_OVERRIDES),
            }
        else:
            result['configuration'] = {
                'parameter_health': _run(CONFIG_PARAMETER_HEALTH, deployment_type=deployment_type),
                'user_roles': _run(CONFIG_USER_ROLES),
                'privilege_audit': _run(CONFIG_PRIVILEGE_AUDIT),
                'ssl_connections': _run(CONFIG_SSL_CONNECTIONS),
                'password_security': _run(CONFIG_PASSWORD_SECURITY),
                'sensitive_columns': _run(CONFIG_SENSITIVE_COLUMNS),
                'rls_status': _run(CONFIG_RLS_STATUS),
                'audit_config': _run(CONFIG_AUDIT_CONFIG),
                'db_role_overrides': _run(CONFIG_DB_ROLE_OVERRIDES),
            }
        
        # Section 3: Activity
        result['activity'] = {
            'connection_summary': _run(ACTIVITY_CONNECTION_SUMMARY),
            'pooling_detection': _run(ACTIVITY_POOLING_DETECTION),
            'app_identification': _run(ACTIVITY_APP_IDENTIFICATION),
            'workload_characterization': _run(ACTIVITY_WORKLOAD_CHARACTERIZATION),
            'client_analysis': _run(ACTIVITY_CLIENT_ANALYSIS),
            'connection_churn': _run(ACTIVITY_CONNECTION_CHURN),
        }
        
        # Section 4: Replication
        result['replication'] = {
            'replication_info': _run(REPLICATION_INFO, deployment_type=deployment_type),
            'replication_lag': _run(REPLICATION_LAG),
            'sync_replication_config': _run(REPLICATION_SYNC_CONFIG),
            'logical_replication_slots': _run(REPLICATION_LOGICAL_SLOTS),
        }
        
        # Section 5: Data Footprint
        result['data_footprint'] = {
            'database_sizes': _run(DATA_DATABASE_SIZES),
            'wal_directory': _run(DATA_WAL_DIRECTORY),
        }
        
        # Section 6: Performance (version-aware + extension-aware)
        checkpoint_stats = _run(PERF_CHECKPOINT_STATS_PG17) if pg_version >= 170000 else _run(PERF_CHECKPOINT_STATS_PRE17)
        
        temp_file_queries = _run(PERF_TEMP_FILE_QUERIES) if has_pg_stat_statements else []
        query_perf = _run(PERF_QUERY_ANALYSIS_PG17) if (has_pg_stat_statements and pg_version >= 170000) else (
            _run(PERF_QUERY_ANALYSIS_PRE17) if has_pg_stat_statements else []
        )
        
        result['performance'] = {
            'checkpoint_stats': checkpoint_stats,
            'bgwriter_stats': _run(PERF_BGWRITER_STATS),
            'autovacuum_utilization': _run(PERF_AUTOVACUUM_UTILIZATION),
            'pooling_efficiency': _run(PERF_POOLING_EFFICIENCY),
            'buffer_hit_ratios': _run(PERF_BUFFER_HIT_RATIOS),
            'lock_tree': _run(PERF_LOCK_TREE),
            'io_stats': _run(PERF_IO_STATS),
            'transaction_stats': _run(PERF_TRANSACTION_STATS),
            'function_performance': _run(PERF_FUNCTION_PERFORMANCE),
            'wait_event_analysis': _run(PERF_WAIT_EVENT_ANALYSIS),
            'session_duration_analysis': _run(PERF_SESSION_DURATION),
            'prepared_statements': _run(PERF_PREPARED_STATEMENTS),
            'temp_file_heavy_queries': temp_file_queries,
            'query_performance_analysis': query_perf,
        }
        
        # Section 7: Maintenance
        result['maintenance'] = {
            'database_integrity': _run(MAINT_DATABASE_INTEGRITY),
            'sequence_exhaustion': _run(MAINT_SEQUENCE_EXHAUSTION),
            'constraint_validation': _run(MAINT_CONSTRAINT_VALIDATION),
            'materialized_view_freshness': _run(MAINT_MATERIALIZED_VIEWS),
            'table_vacuum_stats': _run(MAINT_TABLE_VACUUM_STATS),
        }
        
        # Section 8: Optimization
        result['optimization'] = {
            'index_statistics': _run(OPT_INDEX_STATISTICS),
            'toast_tables': _run(OPT_TOAST_TABLES),
            'large_table_partitioning': _run(OPT_LARGE_TABLE_PARTITIONING),
            'unused_indexes': _run(OPT_UNUSED_INDEXES),
            'duplicate_indexes': _run(OPT_DUPLICATE_INDEXES),
            'idle_connections': _run(OPT_IDLE_CONNECTIONS),
            'oversized_data_types': _run(OPT_OVERSIZED_DATA_TYPES),
            'unused_tables': _run(OPT_UNUSED_TABLES),
            'foreign_keys_without_indexes': _run(OPT_FK_WITHOUT_INDEXES),
            'trigger_analysis': _run(OPT_TRIGGER_ANALYSIS),
            'seq_scan_candidates': _run(OPT_SEQ_SCAN_CANDIDATES),
        }
        
        # Section 9: Summary
        result['summary'] = {
            'core_health_metrics': _run(SUMMARY_CORE_HEALTH),
            'critical_system_metrics': _run(SUMMARY_CRITICAL_SYSTEM),
        }
        
        cursor.close()
        conn.close()
        
        self.logger.info(f"PostgreSQL health insights collected: {sum(len(v) if isinstance(v, list) else len(v) for v in result.values() if isinstance(v, (list, dict)))} data points across 9 sections")
        return result

    def check_pgsnapper_prerequisites(self) -> Dict[str, Any]:
        """Check PGSnapper prerequisites on remote database."""
        try:
            self.logger.info("Checking PGSnapper prerequisites on remote database")

            conn = self._get_db_connection()
            cursor = conn.cursor(cursor_factory=RealDictCursor)

            # --- pg_stat_statements extension installed ---
            cursor.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements'
                ) as has_extension;
            """)
            ext_installed = cursor.fetchone()['has_extension']

            # --- shared_preload_libraries (static — reboot required) ---
            cursor.execute("SELECT setting FROM pg_settings WHERE name = 'shared_preload_libraries';")
            shared_preload = cursor.fetchone()['setting']
            preload_ok = 'pg_stat_statements' in shared_preload

            # --- track_functions (dynamic) ---
            cursor.execute("SELECT setting FROM pg_settings WHERE name = 'track_functions';")
            track_functions = cursor.fetchone()['setting']
            track_functions_ok = track_functions == 'all'

            # --- track_activity_query_size (static — reboot required) ---
            cursor.execute("SELECT setting FROM pg_settings WHERE name = 'track_activity_query_size';")
            query_size = int(cursor.fetchone()['setting'])
            query_size_ok = query_size >= 102400

            # --- pg_monitor role ---
            cursor.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM pg_roles
                    WHERE rolname = %s AND pg_has_role(rolname, 'pg_monitor', 'member')
                ) as has_monitor_role;
            """, (self.db_user,))
            monitor_role_ok = cursor.fetchone()['has_monitor_role']

            cursor.close()
            conn.close()

            prerequisites = {
                'pg_stat_statements_extension': ext_installed,
                'shared_preload_libraries': preload_ok,
                'track_functions': track_functions_ok,
                'track_activity_query_size': query_size_ok,
                'pgsnapper_user_exists': monitor_role_ok,
            }
            required = {
                'pg_stat_statements_extension': ext_installed,
                'shared_preload_libraries': preload_ok,
                'pgsnapper_user_exists': monitor_role_ok,
            }
            all_ready = all(required.values())

            # Build actionable fix messages, flagging which require a reboot
            missing = []
            if not preload_ok:
                missing.append(
                    "[Required for deep dive] shared_preload_libraries does not include 'pg_stat_statements' "
                    "[REBOOT REQUIRED]: Add 'pg_stat_statements' to shared_preload_libraries "
                    "in the RDS/Aurora parameter group, then reboot the DB instance."
                )
            if not ext_installed:
                if preload_ok:
                    # Library is loaded but extension not created — no reboot needed
                    missing.append(
                        "[Required for deep dive] pg_stat_statements extension not installed: "
                        "The library is already loaded (shared_preload_libraries is set). "
                        f"Run: CREATE EXTENSION IF NOT EXISTS pg_stat_statements; on database '{self.db_name}'. "
                        "No reboot required."
                    )
                else:
                    # Library not loaded — need both steps
                    missing.append(
                        "[Required for deep dive] pg_stat_statements extension not installed: "
                        "After adding to shared_preload_libraries and rebooting, run: "
                        f"CREATE EXTENSION IF NOT EXISTS pg_stat_statements; on database '{self.db_name}'."
                    )
            if not track_functions_ok:
                missing.append(
                    f"[Optional] track_functions is '{track_functions}', recommend 'all' to track procedural-language, SQL and C language functions: "
                    "Set track_functions = 'all' in the RDS/Aurora parameter group "
                    "(dynamic — no reboot required, takes effect immediately). "
                    "Note: Profiling overhead on systems with extremely high function call rates. "
                    "The impact is usually very low compared to its benefits for performance profiling."
                )
            if not query_size_ok:
                missing.append(
                    f"[Optional] track_activity_query_size is {query_size}, recommend 102400 "
                    "[REBOOT REQUIRED] to capture the full text of very long SQL statements otherwise truncated during monitoring: "
                    "Set track_activity_query_size = 102400 in the RDS/Aurora parameter group, then reboot the DB instance. "
                    "Note: Setting the value extremely large requires more shared memory for caching the full SQL statements, impacting the cache hit ratio."
                )
            if not monitor_role_ok:
                missing.append(
                    f"[Required for deep dive] User '{self.db_user}' does not have the pg_monitor role: "
                    f"Run: GRANT pg_monitor TO {self.db_user};"
                )

            return {
                'ready': all_ready,
                'prerequisites': prerequisites,
                'current_values': {
                    'shared_preload_libraries': shared_preload,
                    'track_functions': track_functions,
                    'track_activity_query_size': query_size,
                },
                'missing_requirements': missing,
            }

        except Exception as e:
            self.logger.error(f"Error checking PGSnapper prerequisites: {e}")
            return {
                'ready': False,
                'error': str(e),
                'missing_requirements': ['Failed to check prerequisites'],
            }
    
    def check_local_postgresql(self) -> bool:
        """Check if local PostgreSQL is installed and running."""
        try:
            result = subprocess.run(['which', 'psql'], capture_output=True, text=True)
            if result.returncode != 0:
                return False
            
            # Check if PostgreSQL service is running
            result = subprocess.run(['systemctl', 'is-active', 'postgresql'], capture_output=True, text=True)
            return result.returncode == 0
        except Exception:
            return False
    
    def setup_local_postgresql(self):
        """Setup local PostgreSQL for PGSnapper analysis."""
        try:
            self.logger.info("Setting up local PostgreSQL for PGSnapper analysis")
            
            # Check if PostgreSQL is already running
            if self.check_local_postgresql():
                self.logger.info("PostgreSQL already running")
                return
            
            # Start PostgreSQL service (should be installed by CloudFormation)
            subprocess.run(['sudo', 'systemctl', 'start', 'postgresql'], check=True)
            subprocess.run(['sudo', 'systemctl', 'enable', 'postgresql'], check=True)
            
            self.logger.info("PostgreSQL service started")
            
        except Exception as e:
            self.logger.error(f"Error setting up local PostgreSQL: {e}")
            raise
    
    def check_pgsnapper_installation(self) -> Dict[str, Any]:
        """Check if PGSnapper is properly installed."""
        pgsnapper_script = '/home/ec2-user/pgperfstats/Code/PGPerfStatsSnapper/pg_perf_stat_snapper.py'
        loader_script = '/home/ec2-user/pgperfstats/Code/PGPerfStatsSnapper/pg_perf_stat_loader.py'
        sql_dir = '/home/ec2-user/pgperfstats/Code/PGPerfStatsSnapper/SQLs'
        
        status = {
            'pgsnapper_script': os.path.exists(pgsnapper_script),
            'loader_script': os.path.exists(loader_script),
            'sql_queries': os.path.exists(sql_dir),
            'local_postgresql': self.check_local_postgresql()
        }
        
        status['ready'] = all(status.values())
        
        # Add detailed error messages
        if not status['ready']:
            missing_details = []
            if not status['pgsnapper_script']:
                missing_details.append(f"PGSnapper script not found at {pgsnapper_script}")
            if not status['loader_script']:
                missing_details.append(f"PGSnapper loader not found at {loader_script}")
            if not status['sql_queries']:
                missing_details.append(f"PGSnapper SQL queries directory not found at {sql_dir}. Clone repository: git clone https://github.com/aws-samples/aurora-and-database-migration-labs.git /home/ec2-user/pgperfstats")
            if not status['local_postgresql']:
                missing_details.append("Local PostgreSQL not installed or not running. Install: sudo yum install postgresql15 postgresql15-server")
            status['missing_details'] = missing_details
        
        return status
    
    @staticmethod
    def _validate_identifier(value: str, name: str) -> str:
        """Validate that a value contains only safe characters for use in subprocess args."""
        if not re.match(r'^[a-zA-Z0-9._:\-/]+$', value):
            raise ValueError(f"Invalid characters in {name}: {value!r}")
        return value

    def setup_pgsnapper_cron(self, interval_minutes: int = 60) -> str:
        """Setup cron job for periodic PGSnapper snapshots."""
        try:
            self.logger.info(f"Setting up PGSnapper cron job (every {interval_minutes} minutes)")

            status = self.check_pgsnapper_installation()
            if not status['ready']:
                missing = status.get('missing_details', [])
                raise Exception("PGSnapper installation incomplete: " + "; ".join(missing))

            pgsnapper_script = '/home/ec2-user/pgperfstats/Code/PGPerfStatsSnapper/pg_perf_stat_snapper.py'
            output_dir = '/home/ec2-user/pgperfstats/output'

            db_host = self._validate_identifier(self.db_host, 'db_host')
            db_name = self._validate_identifier(self.db_name, 'db_name')
            db_user = self._validate_identifier(self.db_user, 'db_user')
            region = self._validate_identifier(self.region, 'region')

            log_file = f'{output_dir}/{db_host}/{db_name}/pgsnapper_cron.log'
            os.makedirs(f'{output_dir}/{db_host}/{db_name}', exist_ok=True)

            wrapper_script = '/home/ec2-user/pgsnapper_snap.sh'
            # Build the pgsnapper command as a list to avoid shell injection, then join for the wrapper
            pgsnapper_args = [
                'python3', pgsnapper_script,
                '-e', db_host, '-P', str(self.db_port),
                '-d', db_name, '-u', db_user,
                '-s', self.db_secret_arn,
                '-m', 'snap', '-o', output_dir, '-r', region
            ]
            pgsnapper_cmd = ' '.join(pgsnapper_args)
            # FLAGS_DIR is the canonical location written by enable-invasive-collection.sh.
            # The wrapper checks it on every cron execution so the cron self-terminates
            # when collection is no longer needed — even if collect-and-share.sh's own
            # removal logic was never reached (e.g. flag file deleted, instance abandoned).
            flags_dir = '/home/ec2-user/wal-db-stats-collection/data/flags'
            wrapper_content = f"""#!/bin/bash
export PATH=/usr/pgsql-15/bin:/usr/local/pgsql/bin:$PATH
export LD_LIBRARY_PATH=/usr/pgsql-15/lib:/usr/local/pgsql/lib

# ── Self-guard: stop snapshotting when collection is no longer needed ───
FLAGS_DIR="{flags_dir}"

# Guard 1: no flag files present → cluster was deregistered, remove cron and exit
if ! ls "$FLAGS_DIR"/*.flag > /dev/null 2>&1; then
    crontab -l 2>/dev/null | grep -v 'pgsnapper_snap.sh' | crontab - 2>/dev/null
    exit 0
fi

# Guard 2: all registered clusters have status=analyzed → Run 2 succeeded,
# collection is complete. Remove cron and exit.
ALL_DONE=true
for STATUS_FILE in "$FLAGS_DIR"/*_pgsnapper_status.json; do
    [ -f "$STATUS_FILE" ] || {{ ALL_DONE=false; break; }}
    STATUS=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('status',''))" "$STATUS_FILE" 2>/dev/null)
    if [ "$STATUS" != "analyzed" ]; then
        ALL_DONE=false
        break
    fi
done
if [ "$ALL_DONE" = "true" ]; then
    crontab -l 2>/dev/null | grep -v 'pgsnapper_snap.sh' | crontab - 2>/dev/null
    exit 0
fi
# ────────────────────────────────────────────────────────────────────────

{pgsnapper_cmd} >> {log_file} 2>&1
"""
            # Remove existing wrapper if present (previous run sets 0o500 = no write)
            if os.path.exists(wrapper_script):
                os.chmod(wrapper_script, 0o700)
            with open(wrapper_script, 'w', encoding='utf-8') as f:
                f.write(wrapper_content)
            os.chmod(wrapper_script, 0o500)  # owner read+execute only; no write needed after creation

            # Run initial snapshot immediately
            self.logger.info("Running initial PGSnapper snapshot...")
            result = subprocess.run([wrapper_script], capture_output=True, text=True, timeout=300)  # nosec B603 - path is a hardcoded constant written by this process  # nosemgrep: dangerous-subprocess-use-audit
            if result.returncode == 0:
                self.logger.info("Initial snapshot completed successfully")
                if result.stdout.strip():
                    self.logger.info(f"PGSnapper stdout: {result.stdout.strip()}")
            else:
                error_msg = result.stderr.strip() or result.stdout.strip() or "Unknown error"
                self.logger.error(f"Initial snapshot failed (exit code {result.returncode}): {error_msg}")
                self.logger.error("NOT installing cron job — fix the issue and re-run")
                return {
                    'status': 'error',
                    'error': f'Initial snapshot failed: {error_msg}',
                    'next_steps': [
                        'Check the database credentials (secret ARN)',
                        'Verify network connectivity to the database',
                        'Re-run enable-invasive-collection.sh with correct parameters'
                    ]
                }
            
            # Only reaches here if snapshot succeeded — install cron
            cron_schedule = f"*/{interval_minutes} * * * *" if interval_minutes < 60 else f"0 */{interval_minutes//60} * * *"
            cron_entry = f"{cron_schedule} {wrapper_script}\n"
            
            # Try to find crontab command
            crontab_cmd = None
            for path in ['/usr/bin/crontab', '/bin/crontab']:
                if os.path.exists(path):
                    crontab_cmd = path
                    break
            
            if not crontab_cmd:
                # Fallback: install cronie if not available
                self.logger.warning("crontab not found, attempting to install cronie")
                subprocess.run(['sudo', 'yum', 'install', '-y', 'cronie'], check=True)  # nosemgrep: dangerous-subprocess-use-audit
                crontab_cmd = '/usr/bin/crontab'
            
            result = subprocess.run([crontab_cmd, '-l'], capture_output=True, text=True)  # nosemgrep: dangerous-subprocess-use-audit
            existing_cron = result.stdout if result.returncode == 0 else ""
            new_cron = "\n".join([line for line in existing_cron.split("\n") if 'pgsnapper_snap.sh' not in line])
            new_cron += "\n" + cron_entry

            subprocess.run([crontab_cmd, '-'], input=new_cron, text=True, check=True)  # nosemgrep: dangerous-subprocess-use-audit
            
            # Ensure crond service is running
            subprocess.run(['sudo', 'systemctl', 'enable', 'crond'], capture_output=True)  # nosemgrep: dangerous-subprocess-use-audit
            subprocess.run(['sudo', 'systemctl', 'start', 'crond'], capture_output=True)  # nosemgrep: dangerous-subprocess-use-audit
            
            self.logger.info(f"PGSnapper cron job installed: {cron_schedule}")
            return f'{output_dir}/{self.db_host}/{self.db_name}'
            
        except Exception as e:
            self.logger.error(f"Error setting up PGSnapper cron: {e}")
            raise
    
    @staticmethod
    def _format_remaining_time(remaining_days: float) -> str:
        """Format remaining wait time in human-readable units."""
        minutes = remaining_days * 24 * 60
        if minutes < 60:
            return f'{minutes:.0f} more minute(s)'
        hours = remaining_days * 24
        if hours < 24:
            return f'{hours:.1f} more hour(s)'
        return f'{remaining_days:.1f} more day(s)'

    def check_pgsnapper_data_age(self, output_dir: str, min_days: int = 3) -> Dict[str, Any]:
        """Check age and count of collected PGSnapper snapshots."""
        try:
            # PGSnapper creates subdirectories: output_dir/hostname/dbname/
            actual_output_dir = f'{output_dir}/{self.db_host}/{self.db_name}'
            
            if not os.path.exists(actual_output_dir):
                return {'ready': False, 'days': 0, 'snapshots': 0, 'message': 'No data collected yet'}
            
            snap_files = []
            for root, dirs, files in os.walk(actual_output_dir):
                snap_files.extend([os.path.join(root, f) for f in files if f.endswith('.csv')])
            
            if not snap_files:
                return {'ready': False, 'days': 0, 'snapshots': 0, 'message': 'No snapshots found'}
            
            file_times = [os.path.getmtime(f) for f in snap_files]
            oldest_time = datetime.fromtimestamp(min(file_times))
            newest_time = datetime.fromtimestamp(max(file_times))
            
            # Measure time since first snapshot was created (not file span —
            # PGSnapper may overwrite files, making newest == oldest)
            now = datetime.now()
            days_since_first = (now - oldest_time).total_seconds() / 86400
            
            ready = days_since_first >= min_days
            
            return {
                'ready': ready,
                'days': round(days_since_first, 2),
                'snapshots': len(snap_files),
                'oldest_snapshot': oldest_time.isoformat(),
                'newest_snapshot': newest_time.isoformat(),
                'message': f'{days_since_first:.1f} days of data collected ({len(snap_files)} snapshots)' if ready else self._format_remaining_time(min_days - days_since_first) if days_since_first < min_days else f'Only {len(snap_files)} snapshot(s) found, need at least {min_snapshots}'
            }
            
        except Exception as e:
            self.logger.error(f"Error checking PGSnapper data age: {e}")
            return {'ready': False, 'error': str(e)}
    
    def load_pgsnapper_data(self, output_dir: str) -> str:
        """Load PGSnapper data into local PostgreSQL for analysis."""
        try:
            self.logger.info("Loading PGSnapper data into local PostgreSQL")
            
            # Ensure local PostgreSQL is running
            self.setup_local_postgresql()
            
            # Create analysis database
            analysis_db = f"pgsnapper_analysis_{int(datetime.now().timestamp())}"
            
            # Connect as postgres user to create database
            self._validate_identifier(analysis_db, 'analysis_db')
            create_db_cmd = [
                'sudo', '-u', 'postgres', 'psql',
                '-c', f"CREATE DATABASE {analysis_db};"
            ]

            result = subprocess.run(create_db_cmd, capture_output=True, text=True)  # nosec B603 - args are static strings and validated identifier  # nosemgrep: dangerous-subprocess-use-audit
            if result.returncode != 0:
                self.logger.error(f"Failed to create analysis database: {result.stderr}")
                raise Exception(f"Database creation failed: {result.stderr}")
            
            # Load data using PGSnapper loader (no secret needed for localhost with trust auth)
            loader_script = '/home/ec2-user/pgperfstats/Code/PGPerfStatsSnapper/pg_perf_stat_loader.py'
            
            load_cmd = [
                'python3', loader_script,
                '-e', 'localhost',
                '-P', '5432',
                '-d', self._validate_identifier(analysis_db, 'analysis_db'),
                '-u', 'postgres',
                '-s', self.db_secret_arn,
                '-o', output_dir,
                '-r', self._validate_identifier(self.region, 'region')
            ]

            result = subprocess.run(load_cmd, capture_output=True, text=True, timeout=1800)  # nosec B603 - all args are static strings or validated identifiers  # nosemgrep: dangerous-subprocess-use-audit
            
            # Log loader output for debugging
            if result.stdout:
                self.logger.info(f"PGSnapper loader output: {result.stdout}")
            if result.stderr:
                self.logger.warning(f"PGSnapper loader stderr: {result.stderr}")
            
            if result.returncode != 0:
                self.logger.error(f"Failed to load PGSnapper data: {result.stderr}")
                self.logger.error(f"Loader stdout: {result.stdout}")
                raise Exception(f"Data loading failed: {result.stderr}")
            
            self.logger.info(f"PGSnapper data loaded into database: {analysis_db}")
            return analysis_db
            
        except Exception as e:
            self.logger.error(f"Error loading PGSnapper data: {e}")
            raise
    
    def run_pgsnapper_analysis(self, analysis_db: str, min_days: float, skip_pg_stat_statements: bool = False) -> Dict[str, Any]:
        """Run analysis queries on loaded PGSnapper data."""
        try:
            self.logger.info("Running PGSnapper analysis queries")
            
            # Use ALL available snapshots — the readiness gate (check_pgsnapper_data_age)
            # already ensures we have enough data. No time-based filtering here.
            self._validate_identifier(analysis_db, 'analysis_db')
            if not isinstance(min_days, (int, float)) or min_days < 0:
                raise ValueError(f"Invalid min_days value: {min_days}")
            get_snap_ids_cmd = [
                'sudo', '-u', 'postgres', 'psql',
                '-d', analysis_db,
                '-t', '-A',
                '-c', 'SELECT MIN(snap_id), MAX(snap_id) FROM pg_awr_snapshots_cust;'
            ]

            result = subprocess.run(get_snap_ids_cmd, capture_output=True, text=True, timeout=30, cwd='/tmp')  # nosec B603 B108 - analysis_db validated; cwd=/tmp is working dir only  # nosemgrep: dangerous-subprocess-use-audit
            if result.returncode != 0 or not result.stdout.strip():
                self.logger.error("Failed to get snapshot IDs")
                return {'error': 'No snapshots found'}
            
            snap_ids = result.stdout.strip().split('|')
            begin_snap_id = snap_ids[0]
            end_snap_id = snap_ids[1]
            
            if not begin_snap_id or not end_snap_id:
                return {'error': 'No snapshots found in analysis database'}
            
            self.logger.info(f"Using snapshot range: {begin_snap_id} to {end_snap_id}")
            
            # Check if pg_stat_statements data exists
            check_statements_cmd = [
                'sudo', '-u', 'postgres', 'psql',
                '-d', analysis_db,
                '-t', '-A',
                '-c', "SELECT CASE WHEN EXISTS (SELECT 1 FROM pg_stat_statements_history LIMIT 1) THEN 1 ELSE 0 END;"
            ]
            result = subprocess.run(check_statements_cmd, capture_output=True, text=True, timeout=10, cwd='/tmp')  # nosec B603 B108 - cwd=/tmp is working dir only  # nosemgrep: dangerous-subprocess-use-audit
            stmt_count = int(result.stdout.strip()) if result.returncode == 0 and result.stdout.strip() else 0
            if stmt_count == 0:
                self.logger.warning("No pg_stat_statements data found - queries requiring statement history will return 0 rows")
            
            analysis_results = {}
            sql_dir = '/home/ec2-user/pgperfstats/Code/PGPerfStatsSnapper/SQLs'
            
            # Detect column naming convention (old vs new PostgreSQL versions)
            check_columns_cmd = [
                'sudo', '-u', 'postgres', 'psql',
                '-d', analysis_db,
                '-t', '-A',
                '-c', "SELECT column_name FROM information_schema.columns WHERE table_name='pg_stat_statements_history' AND column_name IN ('blk_read_time', 'shared_blk_read_time');"
            ]
            result = subprocess.run(check_columns_cmd, capture_output=True, text=True, timeout=30, cwd='/tmp')  # nosec B603 B108 - cwd=/tmp is working dir only  # nosemgrep: dangerous-subprocess-use-audit
            use_fixed_queries = 'shared_blk_read_time' in result.stdout
            
            # Use fixed SQL queries for newer PostgreSQL versions with different column names
            fixed_sql_dir = os.path.join(os.path.dirname(__file__), 'pgsnapper_sql_fixes')
            
            # Key analysis queries from PGSnapper with expected column headers
            key_queries = {
                'snapshots': {
                    'sql_file': 'list_snaps.sql',
                    'columns': ['snap_id', 'sample_start_time', 'sample_end_time']
                },
                'db_stats': {
                    'sql_file': 'db_stats.sql',
                    'columns': ['datname', 'numbackends', 'xact_commit', 'xact_rollback', 'blks_read', 'blks_hit', 'tup_returned', 'tup_fetched', 'tup_inserted', 'tup_updated', 'tup_deleted', 'conflicts', 'temp_files', 'temp_bytes', 'deadlocks']
                },
                'cache_hit_ratio': {
                    'sql_file': 'cache_hit_ratio.sql',
                    'columns': ['snap_id', 'datname', 'cache_hit_ratio']
                },
                'top_sqls_by_time': {
                    'sql_file': 'top_20_sqls_by_elapsed_time_v2_fixed.sql' if use_fixed_queries else 'top_20_sqls_by_elapsed_time_v2.sql',
                    'columns': ['dbid', 'userid', 'queryid', 'avg_elapsed_time', 'calls', 'avg_shared_blks_hit', 'avg_rows', 'avg_shared_blks_dirtied', 'avg_shared_blks_read', 'avg_shared_blks_written', 'avg_temp_blks_written', 'avg_blk_read_time']
                },
                'top_sqls_by_calls': {
                    'sql_file': 'top_20_sqls_by_calls_v2_fixed.sql' if use_fixed_queries else 'top_20_sqls_by_calls_v2.sql',
                    'columns': ['dbid', 'userid', 'queryid', 'calls', 'avg_elapsed_time', 'avg_shared_blks_hit', 'avg_rows', 'avg_shared_blks_dirtied', 'avg_shared_blks_read', 'avg_shared_blks_written', 'avg_temp_blks_written', 'avg_blk_read_time']
                },
                'table_bloat': {
                    'sql_file': 'table_bloat.sql',
                    'columns': ['database', 'schemaname', 'tablename', 'table_size', 'bloat_size', 'bloat_pct', 'live_pct', 'dead_tuples', 'live_tuples', 'autovacuum_enabled']
                },
                'index_bloat': {
                    'sql_file': 'index_bloat.sql',
                    'columns': ['database', 'schemaname', 'tablename', 'indexname', 'index_size', 'bloat_size', 'bloat_ratio', 'idx_scan', 'idx_tup_read', 'idx_tup_fetch', 'is_bloated']
                },
                'unused_indexes': {
                    'sql_file': 'unused_indexes_fixed.sql',
                    'columns': ['relid', 'indexrelid', 'schemaname', 'tablename', 'indexname', 'idx_scan', 'is_unique']
                },
                'vacuum_history': {
                    'sql_file': 'vacuum_history.sql',
                    'columns': ['snap_id', 'pid', 'relid', 'query', 'state', 'duration', 'wait_event']
                },
                'session_activity': {
                    'sql_file': 'session_activity_summary.sql',
                    'columns': ['snap_id', 'sample_start_time', 'total_sessions', 'running_on_cpu', 'client_wait', 'io_wait', 'lock_wait', 'lwlock_wait', 'bufferpin_wait', 'activity_wait', 'other_wait', 'distinct_apps']
                },
                'top_tables_by_seq_scans': {
                    'sql_file': 'top_20_tables_by_seq_scans.sql',
                    'columns': ['snap_id', 'schemaname', 'tablename', 'seq_scan', 'seq_tup_read', 'idx_scan', 'n_live_tup', 'n_dead_tup', 'last_vacuum', 'last_autovacuum', 'last_analyze', 'last_autoanalyze', 'vacuum_count']
                },
                'top_tables_by_dmls': {
                    'sql_file': 'top_20_tables_by_dmls.sql',
                    'columns': ['relid', 'schemaname', 'tablename', 'n_tup_ins', 'n_tup_upd', 'n_tup_del', 'n_tup_hot_upd', 'n_live_tup', 'n_dead_tup', 'last_vacuum', 'last_autovacuum', 'last_analyze', 'last_autoanalyze', 'vacuum_count']
                },
                # --- Trend queries (per-snapshot, in pgsnapper_sql_fixes/) ---
                'db_stats_trend': {
                    'sql_file': 'db_stats_trend.sql',
                    'columns': ['snap_id', 'sample_start_time', 'datname', 'numbackends', 'delta_commits', 'delta_rollbacks', 'delta_blks_read', 'delta_blks_hit', 'delta_inserts', 'delta_updates', 'delta_deletes', 'delta_temp_files', 'delta_deadlocks']
                },
                'table_dml_trend': {
                    'sql_file': 'table_dml_trend.sql',
                    'columns': ['snap_id', 'sample_start_time', 'schemaname', 'relname', 'delta_ins', 'delta_upd', 'delta_del', 'delta_hot_upd', 'n_live_tup', 'n_dead_tup']
                },
                'seq_scan_trend': {
                    'sql_file': 'seq_scan_trend.sql',
                    'columns': ['snap_id', 'sample_start_time', 'schemaname', 'relname', 'delta_seq_scan', 'delta_seq_tup_read', 'delta_idx_scan', 'n_live_tup', 'n_dead_tup']
                },
                # --- Additional PGSnapper queries (from PGSnapper SQLs/ directory) ---
                'top_sqls_by_cpu': {
                    'sql_file': 'top_sqls_by_cpu_fixed.sql',
                    'columns': ['dbid', 'userid', 'queryid', 'query', 'total_cpu_time', 'total_calls', 'avg_cpu_per_call', 'first_seen', 'last_seen', 'peak_time', 'peak_cpu_in_interval'],
                },
                'aging_tables_for_vacuum': {
                    'sql_file': 'aging_tables_for_vacuum.sql',
                    'columns': ['relname', 'xid_age', 'toast_size']
                },
                'needed_indexes': {
                    'sql_file': 'needed_indexes.sql',
                    'columns': ['schemaname', 'relname', 'seq_scan', 'seq_tup_read', 'idx_scan', 'too_much_seq', 'missing_index']
                },
                'checkpoint_stats_trend': {
                    'sql_file': 'checkpoint_stats_by_snap_id_fixed.sql' if use_fixed_queries else 'checkpoint_stats_by_snap_id.sql',
                    'columns': ['snap_id', 'sample_start_time', 'delta_buffers_bgwriter', 'delta_maxwritten_clean', 'delta_new_buffers_alloc'] if use_fixed_queries else ['snap_id', 'sample_start_time', 'delta_checkpoints_timed', 'delta_checkpoints_req', 'delta_buffers_checkpoint', 'delta_buffers_bgwriter', 'delta_buffers_backend', 'delta_new_buffers_alloc']
                },
                'temp_file_trend': {
                    'sql_file': 'temp_file_by_snap_id.sql',
                    'columns': ['snap_id', 'sample_start_time', 'datname', 'temp_files', 'temp_bytes']
                },
                'blockers_and_waiters': {
                    'sql_file': 'blockers_and_waiters_hist.sql',
                    'columns': ['snap_id', 'sample_start_time', 'blocked_pid', 'blocking_pid', 'blocked_query', 'blocking_query', 'wait_duration']
                },
                'top_functions': {
                    'sql_file': 'top_20_functions_by_avg_total_time.sql',
                    'columns': ['schemaname', 'funcname', 'avg_time', 'calls', 'self_time']
                },
            }
            
            if use_fixed_queries:
                self.logger.info("Using fixed SQL queries for newer PostgreSQL version (shared_blk_read_time columns)")
            
            # Queries that require pg_stat_statements data
            requires_statements = {'top_sqls_by_time', 'top_sqls_by_calls', 'top_sqls_by_cpu'}

            for analysis_name, query_info in key_queries.items():
                try:
                    sql_file = query_info['sql_file']
                    columns = query_info['columns']

                    # Skip queries that need pg_stat_statements if skipped or no data
                    if analysis_name in requires_statements and (skip_pg_stat_statements or stmt_count == 0):
                        self.logger.warning(f"Skipping {analysis_name} - no pg_stat_statements data available")
                        analysis_results[analysis_name] = {'columns': columns, 'data': []}
                        continue
                    # Check fixed SQL directory first, then fall back to PGSnapper SQLs directory
                    sql_path_fixed = os.path.join(fixed_sql_dir, sql_file)
                    sql_path_pgsnapper = os.path.join(sql_dir, sql_file)
                    if os.path.exists(sql_path_fixed):
                        sql_path = sql_path_fixed
                    else:
                        sql_path = sql_path_pgsnapper
                    
                    if os.path.exists(sql_path):
                        # Validate snap IDs are integers before interpolating into psql -v args
                        if not re.match(r'^\d+$', str(begin_snap_id)) or not re.match(r'^\d+$', str(end_snap_id)):
                            raise ValueError(f"Invalid snap IDs: {begin_snap_id}, {end_snap_id}")
                        cmd = [
                            'sudo', '-u', 'postgres', 'psql',
                            '-d', analysis_db,
                            '-v', f'begin_snap_id={begin_snap_id}',
                            '-v', f'end_snap_id={end_snap_id}',
                            '-f', sql_path,
                            '-t', '--csv'
                        ]

                        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, cwd='/tmp')  # nosec B603 B108 - snap IDs validated as integers above; cwd=/tmp is working dir only  # nosemgrep: dangerous-subprocess-use-audit
                        if result.returncode == 0 and not result.stderr:
                            # Parse CSV output (handles embedded commas, pipes, newlines in fields)
                            import csv
                            import io
                            rows = []
                            reader = csv.reader(io.StringIO(result.stdout))
                            for row in reader:
                                if row:  # skip empty rows
                                    rows.append(row)
                            # Apply max_rows limit if specified (prevents oversized output)
                            max_rows = query_info.get('max_rows')
                            if max_rows and len(rows) > max_rows:
                                self.logger.info(f"Truncating {analysis_name} from {len(rows)} to {max_rows} rows")
                                rows = rows[:max_rows]
                            # Store with column metadata so downstream consumers
                            # don't need a hardcoded column mapping
                            analysis_results[analysis_name] = {
                                'columns': columns,
                                'data': rows
                            }
                            self.logger.info(f"Collected {len(rows)} rows for {analysis_name}")
                        else:
                            error_msg = result.stderr if result.stderr else 'Query returned no data'
                            if 'does not exist' in error_msg:
                                self.logger.warning(f"Skipping {analysis_name} - incompatible with PostgreSQL version: {error_msg.split('ERROR:')[1].strip() if 'ERROR:' in error_msg else error_msg}")
                            else:
                                self.logger.error(f"Error running {analysis_name}: {error_msg}")
                            analysis_results[analysis_name] = {'columns': columns, 'data': []}
                    else:
                        self.logger.warning(f"SQL file not found: {sql_path}")
                        analysis_results[analysis_name] = {'columns': columns, 'data': []}
                        
                except Exception as e:
                    self.logger.error(f"Error running {analysis_name}: {e}")
                    analysis_results[analysis_name] = {'columns': query_info.get('columns', []), 'data': [], 'error': str(e)}
            
            return {
                'collection_timestamp': datetime.utcnow().isoformat(),
                'analysis_database': analysis_db,
                'snapshot_range': {'begin': begin_snap_id, 'end': end_snap_id},
                'pg_stat_statements_count': stmt_count,
                'analysis_results': analysis_results,
                'data_format': 'structured_with_columns'
            }
            
        except Exception as e:
            self.logger.error(f"Error running PGSnapper analysis: {e}")
            return {'error': str(e)}
    
    def collect_pgsnapper_data(self, min_days: int = 3, snap_interval_minutes: int = 60, status_file: Optional[str] = None, skip_pg_stat_statements: bool = False) -> Dict[str, Any]:
        """Setup PGSnapper collection or analyze if enough data exists."""
        try:
            # --- Prerequisite gate ---
            prereq = self.check_pgsnapper_prerequisites()
            if not prereq['ready']:
                missing = prereq.get('missing_requirements', [])
                required_items = [m for m in missing if m.startswith('[Required')]
                optional_items = [m for m in missing if m.startswith('[Optional')]
                error_lines = ["PGSnapper prerequisites not met on the target database."]
                if required_items:
                    error_lines.append("Fix the following required items before proceeding:")
                    for i, item in enumerate(required_items, 1):
                        error_lines.append(f"  {i}. {item}")
                if optional_items:
                    error_lines.append("")
                    error_lines.append("Recommended improvements (collection will still run without these):")
                    for i, item in enumerate(optional_items, 1):
                        error_lines.append(f"  {i}. {item}")
                error_lines.append("")
                error_lines.append("Once required prerequisites are satisfied, re-run the collection.")
                error_lines.append("If you want to proceed without pg_stat_statements (SQL stats will be skipped),")
                error_lines.append("re-run with --skip-pg-stat-statements.")
                error_msg = "\n".join(error_lines)
                raise RuntimeError(error_msg)

            # PGSnapper automatically creates subdirectories, so use base output dir
            output_dir = f'/home/ec2-user/pgperfstats/output'
            
            data_status = self.check_pgsnapper_data_age(output_dir, min_days)
            
            # Write status file for collect-and-share.sh to read
            if status_file:
                status_data = {
                    'status': 'analyzed' if data_status.get('ready') else 'collecting',
                    'pgsnapper_days_collected': data_status.get('days', 0),
                    'pgsnapper_min_days_required': min_days,
                    'pgsnapper_snapshots': data_status.get('snapshots', 0),
                    'pgsnapper_ready': data_status.get('ready', False),
                    'collection_timestamp': datetime.utcnow().isoformat()
                }
                with open(status_file, 'w', encoding='utf-8') as f:
                    json.dump(status_data, f, indent=2)
            
            if not data_status['ready']:
                remaining_seconds = (min_days - data_status.get('days', 0)) * 86400
                remaining_msg = self._format_remaining_time(remaining_seconds / 86400)
                
                # Ensure cron is running so snapshots keep accumulating
                try:
                    self.setup_pgsnapper_cron(snap_interval_minutes)
                except Exception:
                    pass  # Cron may already be installed
                
                self.logger.info(
                    f"PGSnapper data not ready: {data_status.get('snapshots', 0)} snapshots collected, "
                    f"need ~{remaining_msg}. Re-run this command later."
                )
                
                return {
                    'status': 'collecting',
                    'cron_status': 'configured',
                    'message': f"PGSnapper cron job running (every {snap_interval_minutes} min). "
                               f"{data_status.get('snapshots', 0)} snapshots so far, need ~{remaining_msg}.",
                    'next_steps': f"Re-run this command after ~{remaining_msg} to analyze the collected data.",
                    'data_status': data_status,
                    'output_dir': output_dir
                }
            
            self.logger.info(f"PGSnapper data ready: {data_status['days']} days, {data_status['snapshots']} snapshots")
            
            pgsnapper_script = '/home/ec2-user/pgperfstats/Code/PGPerfStatsSnapper/pg_perf_stat_snapper.py'
            env = os.environ.copy()
            # Password still needed for direct database queries (non-PGSnapper operations)
            if self.db_password:
                env['PGPASSWORD'] = self.db_password
            
            self.logger.info("Running PGSnapper package mode")
            package_cmd = [
                'python3', pgsnapper_script,
                '-e', self._validate_identifier(self.db_host, 'db_host'),
                '-P', str(self.db_port),
                '-d', self._validate_identifier(self.db_name, 'db_name'),
                '-u', self._validate_identifier(self.db_user, 'db_user'),
                '-s', self.db_secret_arn,
                '-m', 'package',
                '-o', output_dir,
                '-r', self._validate_identifier(self.region, 'region')
            ]

            result = subprocess.run(package_cmd, env=env, capture_output=True, text=True, timeout=600)  # nosec B603 - all args are static strings or validated identifiers  # nosemgrep: dangerous-subprocess-use-audit
            
            # Log PGSnapper output for debugging
            if result.stdout:
                self.logger.info(f"PGSnapper stdout: {result.stdout}")
            if result.stderr:
                self.logger.warning(f"PGSnapper stderr: {result.stderr}")
            
            if result.returncode != 0:
                error_msg = f"PGSnapper package failed with return code {result.returncode}"
                if result.stderr:
                    error_msg += f": {result.stderr}"
                raise Exception(error_msg)
            
            actual_output_dir = f'{output_dir}/{self.db_host}/{self.db_name}'
            analysis_db = self.load_pgsnapper_data(actual_output_dir)
            analysis_results = self.run_pgsnapper_analysis(analysis_db, min_days, skip_pg_stat_statements)
            
            # Collection complete — remove the PGSnapper cron job to stop accumulating snapshots
            try:
                result = subprocess.run(['crontab', '-l'], capture_output=True, text=True, timeout=10)  # nosec B603 B607
                if result.returncode == 0 and 'pg_perf_stat_snapper' in result.stdout:
                    new_cron = '\n'.join(
                        line for line in result.stdout.strip().split('\n')
                        if 'pg_perf_stat_snapper' not in line
                    )
                    subprocess.run(['crontab', '-'], input=new_cron + '\n', text=True, timeout=10)  # nosec B603 B607
                    self.logger.info("PGSnapper cron job removed (collection complete)")
            except Exception as e:
                self.logger.warning(f"Could not remove PGSnapper cron: {e}")
            
            return {
                'status': 'analyzed',
                'pgsnapper_output_dir': actual_output_dir,
                'analysis_database': analysis_db,
                'data_status': data_status,
                **analysis_results
            }
            
        except Exception as e:
            return {
                'status': 'error',
                'error': str(e),
                'next_steps': 'Fix the error above and run the command again to retry PGSnapper data collection.'
            }

    def _detect_db_type(self, identifier: str) -> Dict[str, str]:
        """Detect whether identifier is an Aurora cluster, RDS Multi-AZ DB Cluster, or RDS instance."""
        try:
            response = self.rds_client.describe_db_clusters(DBClusterIdentifier=identifier)
            engine = response['DBClusters'][0].get('Engine', '')
            if engine.startswith('aurora'):
                return {'type': 'aurora_cluster', 'wal_framework': 'AuroraPostgreSQL_CustomLens_v1.json'}
            # engine='postgres' at cluster level → RDS Multi-AZ DB Cluster (not Aurora)
            return {'type': 'rds_multiaz_cluster', 'wal_framework': 'RDS_PostgreSQL_CustomLens_v1.json'}
        except ClientError:
            pass
        return {'type': 'rds_instance', 'wal_framework': 'RDS_PostgreSQL_CustomLens_v1.json'}

    def collect_all_data(self, cluster_id: str, pgsnapper_min_days: int = 3, 
                     pgsnapper_interval_minutes: int = 60, status_file: Optional[str] = None, 
                     skip_pg_stat_statements: bool = False,
                     setup_only: bool = False,
                     skip_non_invasive: bool = False) -> Dict[str, Any]:
        """Collect all invasive data for the cluster."""
        self.logger.info(f"Starting invasive data collection for cluster {cluster_id}")
        
        # Detect database type dynamically
        db_type_info = self._detect_db_type(cluster_id)
        self._detected_db_type = db_type_info['type']
        self.logger.info(f"Detected {db_type_info['type']} for identifier: {cluster_id}")
        
        # ── Run 1: Setup only — install pgsnapper cron, no data collection ──
        if setup_only:
            self.logger.info("Setup-only mode: installing PGSnapper cron job, no data collection")
            collected_data = {
                'collection_type': 'invasive_setup_only',
                'identifier': cluster_id,
                **db_type_info
            }
            try:
                if pgsnapper_min_days > 0:
                    # Only check prerequisites and install cron — do NOT run analysis
                    prereq = self.check_pgsnapper_prerequisites()
                    if not prereq['ready']:
                        missing = prereq.get('missing_requirements', [])
                        required_items = [m for m in missing if m.startswith('[Required')]
                        if required_items:
                            self.logger.error("PGSnapper prerequisites not met:")
                            for item in required_items:
                                self.logger.error(f"  {item}")
                            collected_data['pgsnapper'] = {'status': 'error', 'error': 'Prerequisites not met', 'missing': required_items}
                            return collected_data

                    try:
                        self.setup_pgsnapper_cron(pgsnapper_interval_minutes)
                        collected_data['pgsnapper'] = {
                            'status': 'collecting',
                            'message': f'PGSnapper cron job configured to collect snapshots every {pgsnapper_interval_minutes} minutes.',
                            'next_steps': f'Wait {pgsnapper_min_days} days for data collection, then run this command again.',
                        }
                    except Exception as cron_error:
                        self.logger.error(f"Failed to setup PGSnapper cron: {cron_error}")
                        collected_data['pgsnapper'] = {'status': 'error', 'error': str(cron_error)}

                    # Write status file so collect-and-share.sh knows setup is done but data isn't ready
                    if status_file:
                        import json as _json
                        with open(status_file, 'w', encoding='utf-8') as f:
                            _json.dump({
                                'status': 'collecting',
                                'pgsnapper_days_collected': 0,
                                'pgsnapper_min_days_required': pgsnapper_min_days,
                                'pgsnapper_snapshots': 0,
                                'collection_timestamp': datetime.utcnow().isoformat(),
                            }, f, indent=2)

                return collected_data
            except Exception as e:
                self.logger.error(f"Error during PGSnapper setup: {e}")
                raise
        
        database_info = {
            'identifier': cluster_id,
            **db_type_info
        }
        
        # ── Run 2 (or no-flag run): Collect data ──
        if skip_non_invasive:
            # Non-invasive already done by fleet — load it and merge
            self.logger.info("Skipping internal non-invasive collection (already done by fleet)")
            non_invasive_file = os.path.join(self.output_dir, f"{cluster_id}_non_invasive_data.json")
            if os.path.exists(non_invasive_file):
                with open(non_invasive_file, 'r') as f:
                    collected_data = json.load(f)
                collected_data['collection_type'] = 'invasive'
                self.logger.info(f"Loaded fleet non-invasive data from {non_invasive_file}")
            else:
                self.logger.warning(f"Fleet non-invasive file not found: {non_invasive_file} — running internal non-invasive")
                collected_data = super().collect_database_data(database_info)
                collected_data['collection_type'] = 'invasive'
        else:
            collected_data = super().collect_database_data(database_info)
            collected_data['collection_type'] = 'invasive'
        
        try:
            # Add invasive data collection
            collected_data['database_statistics'] = self.collect_database_statistics()
            collected_data['configuration_parameters'] = self.collect_configuration_parameters()
            collected_data['schema_information'] = self.collect_schema_information()
            collected_data['query_performance'] = self.collect_query_performance()
            
            # Collect comprehensive health insights (57 queries across 9 sections)
            try:
                collected_data['pg_health_insights'] = self.collect_pg_health_insights(
                    skip_security=getattr(self, '_skip_security', False)
                )
            except Exception as e:
                self.logger.warning(f"pg_health_insights collection failed (non-fatal): {e}")
                collected_data['pg_health_insights'] = {'error': str(e)}
            
            # Collect PGSnapper data if requested
            if pgsnapper_min_days > 0:
                collected_data['pgsnapper'] = self.collect_pgsnapper_data(pgsnapper_min_days, pgsnapper_interval_minutes, status_file, skip_pg_stat_statements)
            
            # Apply PII redaction before writing to disk
            query_hash_map = {}
            if not getattr(self, '_skip_redaction', False):
                try:
                    from utils.pii_redactor import PiiRedactor
                except ImportError:
                    try:
                        _script_dir = os.path.dirname(os.path.realpath(__file__))
                        sys.path.insert(0, os.path.dirname(_script_dir))
                        from utils.pii_redactor import PiiRedactor
                    except ImportError:
                        self.logger.warning("PII redaction skipped — utils/pii_redactor.py not found")
                        PiiRedactor = None
                if PiiRedactor:
                    redactor = PiiRedactor()
                    collected_data, query_hash_map = redactor.redact(collected_data)
                self.logger.info("PII redaction applied")
            
            # Save to file
            output_file = os.path.join(self.output_dir, f"{cluster_id}_invasive_data.json")
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(collected_data, f, indent=2, default=str)
            
            # Save query hash map (allows SA to look up original query text if needed)
            if query_hash_map:
                hash_map_file = os.path.join(self.output_dir, f"{cluster_id}_query_hash_map.json")
                with open(hash_map_file, 'w', encoding='utf-8') as f:
                    json.dump(query_hash_map, f, indent=2)
                self.logger.info(f"Query hash map saved: {len(query_hash_map)} entries")
            
            self.logger.info(f"Invasive data collection completed. Output saved to {output_file}")
            return collected_data
            
        except Exception as e:
            self.logger.error(f"Error during invasive data collection: {e}")
            raise


def main():
    parser = argparse.ArgumentParser(description='Invasive PostgreSQL data collector')
    parser.add_argument('--cluster-id', required=True, help='Aurora cluster identifier')
    parser.add_argument('--region', required=True, help='AWS region')
    parser.add_argument('--db-host', required=True, help='Database host')
    parser.add_argument('--db-user', required=True, help='Database user')
    parser.add_argument('--db-password', help='Database password (will prompt if not provided, not needed if using --db-secret-arn)')
    parser.add_argument('--db-secret-arn', required=True, help='AWS Secrets Manager ARN for database credentials (REQUIRED for PGSnapper)')
    parser.add_argument('--db-port', type=int, default=5432, help='Database port')
    parser.add_argument('--db-name', default='postgres', help='Database name')
    parser.add_argument('--output-dir', default='./data', help='Output directory for collected data')
    parser.add_argument('--days', type=int, default=7, help='Number of days of metrics to collect')
    parser.add_argument('--pgsnapper-min-days', type=float, default=3, help='Minimum days of PGSnapper data required before analysis (0 to skip)')
    parser.add_argument('--pgsnapper-interval', type=int, default=60, help='PGSnapper snapshot interval in minutes')
    parser.add_argument('--status-file', help='Path to write PGSnapper status JSON file for collect-and-share.sh')
    parser.add_argument('--skip-pg-stat-statements', action='store_true',
                        help='Skip pg_stat_statements prerequisite check and SQL stats queries. '
                             'Use when pg_stat_statements is not enabled and you want to proceed with other PGSnapper metrics only.')
    parser.add_argument('--setup-only', action='store_true',
                        help='Only install PGSnapper cron job, no data collection')
    parser.add_argument('--skip-non-invasive', action='store_true',
                        help='Skip internal non-invasive collection (already done by fleet)')
    parser.add_argument('--skip-security', action='store_true',
                        help='Skip security-related queries in pg_health_insights (user roles, privileges, SSL, passwords, RLS, audit config). '
                             'Use when security data should not be collected or shared.')
    parser.add_argument('--no-redact', action='store_true',
                        help='Skip PII redaction (endpoints, client IPs, KMS ARNs). '
                             'Use only if you need the raw data for internal analysis.')
    
    args = parser.parse_args()
    
    # Get password from Secrets Manager if secret ARN provided and no password given
    if not args.db_password and args.db_secret_arn:
        try:
            import boto3
            import json
            secrets_client = boto3.client('secretsmanager', region_name=args.region)
            secret_response = secrets_client.get_secret_value(SecretId=args.db_secret_arn)
            secret_data = json.loads(secret_response['SecretString'])
            args.db_password = secret_data.get('password')
            if not args.db_password:
                print("⚠️  Warning: Could not extract password from secret, will prompt")
        except Exception as e:
            print(f"⚠️  Warning: Could not retrieve password from Secrets Manager: {e}")
            print("   Will prompt for password instead")
    
    # Prompt for password only if still not available
    if not args.db_password:
        import getpass
        args.db_password = getpass.getpass("Database password: ")
    
    try:
        collector = InvasiveCollector(
            region=args.region,
            db_host=args.db_host,
            db_user=args.db_user,
            db_password=args.db_password,
            db_port=args.db_port,
            db_name=args.db_name,
            output_dir=args.output_dir,
            db_secret_arn=args.db_secret_arn
        )
        collector._skip_redaction = args.no_redact
        collector._skip_security = args.skip_security
        
        result = collector.collect_all_data(
            args.cluster_id,
            pgsnapper_min_days=args.pgsnapper_min_days,
            pgsnapper_interval_minutes=args.pgsnapper_interval,
            status_file=args.status_file,
            skip_pg_stat_statements=args.skip_pg_stat_statements,
            setup_only=args.setup_only,
            skip_non_invasive=args.skip_non_invasive,
        )
        
        # Check PGSnapper status and provide user guidance
        if 'pgsnapper' in result:
            pgsnapper_status = result['pgsnapper'].get('status')
            if pgsnapper_status == 'collecting':
                print(f"\n⚠️  PGSnapper data collection initiated but not yet complete")
                print(f"   {result['pgsnapper'].get('message')}")
                print(f"\n📋 Next Steps:")
                print(f"   1. {result['pgsnapper'].get('next_steps')}")
                print(f"   2. Re-run this command after {args.pgsnapper_min_days} days:")
                print(f"      python3.11 ./scripts/invasive_collector.py \\")
                print(f"        --cluster-id {args.cluster_id} \\")
                print(f"        --region {args.region} \\")
                print(f"        --db-host {args.db_host} \\")
                print(f"        --db-user {args.db_user} \\")
                print(f"        --db-name {args.db_name} \\")
                print(f"        --db-secret-arn '{args.db_secret_arn}' \\")
                print(f"        --pgsnapper-min-days {args.pgsnapper_min_days} \\")
                print(f"        --pgsnapper-interval {args.pgsnapper_interval} \\")
                print(f"        --output-dir {args.output_dir}")
                print(f"\n✅ Non-invasive and basic invasive data collection completed (without PGSnapper analysis)")
            elif pgsnapper_status == 'analyzed':
                print(f"✅ Invasive data collection completed successfully for cluster {args.cluster_id}")
                print(f"   Including PGSnapper analysis with {result['pgsnapper']['data_status']['days']} days of data")
            elif pgsnapper_status == 'error':
                print(f"\n⚠️  PGSnapper data collection encountered an error")
                print(f"   Error: {result['pgsnapper'].get('error')}")
                print(f"   {result['pgsnapper'].get('next_steps')}")
                print(f"\n✅ Non-invasive and basic invasive data collection completed (without PGSnapper)")
        else:
            print(f"✅ Invasive data collection completed successfully for cluster {args.cluster_id}")
        
    except NoCredentialsError:
        print("❌ AWS credentials not found. Please configure AWS CLI or set environment variables.")
        return 1
    except Exception as e:
        print(f"❌ Error during data collection: {e}")
        return 1
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
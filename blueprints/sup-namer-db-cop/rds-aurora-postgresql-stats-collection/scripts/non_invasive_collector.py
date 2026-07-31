#!/usr/bin/env python3
"""
Non-invasive data collection script for PostgreSQL WAL review.
Collects cluster configuration, CloudWatch metrics, and Performance Insights data.
Supports both single database and fleet-wide collection.
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from typing import Dict, List, Any
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

# Add parent directory to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.fleet_discovery import FleetDiscovery


class NonInvasiveCollector:
    def __init__(self, region: str, output_dir: str = "./data"):
        self.region = region
        self.output_dir = output_dir
        self.rds_client = boto3.client('rds', region_name=region)
        self.cloudwatch_client = boto3.client('cloudwatch', region_name=region)
        self.pi_client = boto3.client('pi', region_name=region)
        
        # Ensure output directory exists
        os.makedirs(output_dir, exist_ok=True)
        
        # Setup logging
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler(f'{output_dir}/collection.log'),
                logging.StreamHandler()
            ]
        )
        self.logger = logging.getLogger(__name__)

    def _get_writer_instance_id(self, cluster_id: str) -> str | None:
        """Return the DBInstanceIdentifier of the writer for an RDS Multi-AZ DB Cluster.

        Uses describe_db_clusters → DBClusterMembers[].IsClusterWriter which is the
        authoritative API source. Never relies on hostname patterns or external hints.
        """
        try:
            resp = self.rds_client.describe_db_clusters(DBClusterIdentifier=cluster_id)
            for member in resp['DBClusters'][0].get('DBClusterMembers', []):
                if member.get('IsClusterWriter'):
                    return member['DBInstanceIdentifier']
        except ClientError as e:
            self.logger.warning(f"Could not resolve writer for {cluster_id}: {e}")
        return None

    def collect_database_configuration(self, database_info: Dict[str, Any]) -> Dict[str, Any]:
        """Collect database configuration details (Aurora cluster, RDS Multi-AZ DB Cluster, or RDS instance)."""
        db_id = database_info['identifier']
        db_type = database_info['type']
        try:
            self.logger.info(f"Collecting configuration for {db_type}: {db_id}")

            if db_type in ('aurora_cluster', 'rds_multiaz_cluster'):
                # Both Aurora and RDS Multi-AZ DB Cluster use the same cluster API shape:
                # describe_db_clusters + enumerate member instances via DBClusterIdentifier.
                return self._collect_cluster_config(db_id, db_type)
            else:
                return self._collect_rds_instance_config(db_id)
                
        except ClientError as e:
            self.logger.error(f"Error collecting configuration for {db_id}: {e}")
            raise

    def _collect_cluster_config(self, cluster_id: str, db_type: str) -> Dict[str, Any]:
        """Collect cluster configuration for Aurora or RDS Multi-AZ DB Cluster.

        Both share the same API shape: describe_db_clusters for the cluster entity,
        then enumerate member instances. The output structure is cluster + instances[],
        with each instance annotated with its role (writer/readable_standby) sourced
        from DBClusterMembers[].IsClusterWriter — the authoritative API field.
        """
        try:
            self.logger.info(f"Collecting cluster configuration for {cluster_id} ({db_type})")

            cluster_response = self.rds_client.describe_db_clusters(
                DBClusterIdentifier=cluster_id
            )
            cluster = cluster_response['DBClusters'][0]

            # Build writer-lookup from cluster members for role annotation
            writer_set = {
                m['DBInstanceIdentifier']
                for m in cluster.get('DBClusterMembers', [])
                if m.get('IsClusterWriter')
            }
            promotion_tiers = {
                m['DBInstanceIdentifier']: m.get('PromotionTier', 1)
                for m in cluster.get('DBClusterMembers', [])
            }

            # Get instance details (paginated)
            paginator = self.rds_client.get_paginator('describe_db_instances')
            cluster_instances = [
                instance
                for page in paginator.paginate()
                for instance in page['DBInstances']
                if instance.get('DBClusterIdentifier') == cluster_id
            ]

            # Base cluster config — common to Aurora and RDS Multi-AZ DB Cluster
            cluster_cfg: Dict[str, Any] = {
                'identifier': cluster['DBClusterIdentifier'],
                'engine': cluster['Engine'],
                'engine_version': cluster['EngineVersion'],
                'status': cluster['Status'],
                'multi_az': cluster['MultiAZ'],
                'backup_retention_period': cluster['BackupRetentionPeriod'],
                'preferred_backup_window': cluster['PreferredBackupWindow'],
                'preferred_maintenance_window': cluster['PreferredMaintenanceWindow'],
                'encrypted': cluster['StorageEncrypted'],
                'kms_key_id': cluster.get('KmsKeyId'),
                'deletion_protection': cluster['DeletionProtection'],
                'availability_zones': cluster['AvailabilityZones'],
                'vpc_security_groups': [sg['VpcSecurityGroupId'] for sg in cluster['VpcSecurityGroups']],
                'db_subnet_group': cluster['DBSubnetGroup'],
                'parameter_group': cluster['DBClusterParameterGroup'],
                'endpoint': cluster['Endpoint'],
                'reader_endpoint': cluster.get('ReaderEndpoint'),
                'reader_instances': sum(
                    1 for m in cluster.get('DBClusterMembers', [])
                    if not m.get('IsClusterWriter', True)
                ),
            }

            # Aurora-specific fields
            if db_type == 'aurora_cluster':
                cluster_cfg['engine_lifecycle_support'] = cluster.get(
                    'EngineLifecycleSupport', 'open-source-rds-extended-support-disabled'
                )
                cluster_cfg['global_cluster_identifier'] = cluster.get('GlobalClusterIdentifier')

            # RDS Multi-AZ DB Cluster specific fields
            if db_type == 'rds_multiaz_cluster':
                cluster_cfg['cluster_type'] = 'multi_az_db_cluster'
                cluster_cfg['instance_class'] = cluster.get('DBClusterInstanceClass', 'N/A')

            config_data: Dict[str, Any] = {
                'cluster': cluster_cfg,
                'instances': [],
            }

            for instance in cluster_instances:
                inst_id = instance['DBInstanceIdentifier']
                role = 'writer' if inst_id in writer_set else 'readable_standby'
                instance_data: Dict[str, Any] = {
                    'identifier': inst_id,
                    'class': instance['DBInstanceClass'],
                    'status': instance['DBInstanceStatus'],
                    'availability_zone': instance['AvailabilityZone'],
                    'publicly_accessible': instance['PubliclyAccessible'],
                    'monitoring_interval': instance['MonitoringInterval'],
                    'performance_insights_enabled': instance['PerformanceInsightsEnabled'],
                    'performance_insights_retention_period': instance.get('PerformanceInsightsRetentionPeriod'),
                    'auto_minor_version_upgrade': instance['AutoMinorVersionUpgrade'],
                    'promotion_tier': promotion_tiers.get(inst_id, 1),
                    'endpoint': instance['Endpoint']['Address'] if 'Endpoint' in instance else None,
                    'role': role,
                }
                config_data['instances'].append(instance_data)

            # Writer first for consistent ordering
            config_data['instances'].sort(key=lambda x: (0 if x['role'] == 'writer' else 1))

            return config_data

        except ClientError as e:
            self.logger.error(f"Error collecting cluster configuration for {cluster_id}: {e}")
            raise

    def _collect_aurora_cluster_config(self, cluster_id: str) -> Dict[str, Any]:
        """Collect Aurora cluster configuration details. Delegates to _collect_cluster_config."""
        return self._collect_cluster_config(cluster_id, 'aurora_cluster')

    def _collect_rds_instance_config(self, instance_id: str) -> Dict[str, Any]:
        """Collect RDS instance configuration details."""
        try:
            # For Multi-AZ DB clusters the caller may pass the cluster identifier.
            # Try describe_db_instances first; fall back to describe_db_clusters.
            try:
                instance_response = self.rds_client.describe_db_instances(
                    DBInstanceIdentifier=instance_id
                )
                instance = instance_response['DBInstances'][0]
            except ClientError:
                # Multi-AZ DB cluster — describe at cluster level
                cluster_response = self.rds_client.describe_db_clusters(
                    DBClusterIdentifier=instance_id
                )
                cluster = cluster_response['DBClusters'][0]
                return {
                    'instance': {
                        'identifier': cluster['DBClusterIdentifier'],
                        'engine': cluster['Engine'],
                        'engine_version': cluster['EngineVersion'],
                        'status': cluster['Status'],
                        'instance_class': cluster.get('DBClusterInstanceClass', 'N/A'),
                        'multi_az': cluster['MultiAZ'],
                        'backup_retention_period': cluster['BackupRetentionPeriod'],
                        'preferred_backup_window': cluster['PreferredBackupWindow'],
                        'preferred_maintenance_window': cluster['PreferredMaintenanceWindow'],
                        'encrypted': cluster['StorageEncrypted'],
                        'kms_key_id': cluster.get('KmsKeyId'),
                        'deletion_protection': cluster['DeletionProtection'],
                        'availability_zones': cluster.get('AvailabilityZones', []),
                        'vpc_security_groups': [sg['VpcSecurityGroupId'] for sg in cluster.get('VpcSecurityGroups', [])],
                        'db_subnet_group': cluster.get('DBSubnetGroup'),
                        'parameter_group': cluster.get('DBClusterParameterGroup'),
                        'endpoint': cluster.get('Endpoint'),
                        'port': cluster.get('Port'),
                        'monitoring_interval': 0,
                        'performance_insights_enabled': cluster.get('PerformanceInsightsEnabled', False),
                        'performance_insights_retention_period': cluster.get('PerformanceInsightsRetentionPeriod'),
                        'auto_minor_version_upgrade': cluster.get('AutoMinorVersionUpgrade', False),
                        'read_replica_count': 0,
                        'is_read_replica': False,
                        'cluster_type': 'multi_az_db_cluster'
                    }
                }
            
            config_data = {
                'instance': {
                    'identifier': instance['DBInstanceIdentifier'],
                    'engine': instance['Engine'],
                    'engine_version': instance['EngineVersion'],
                    'status': instance['DBInstanceStatus'],
                    'instance_class': instance['DBInstanceClass'],
                    'multi_az': instance['MultiAZ'],
                    'backup_retention_period': instance['BackupRetentionPeriod'],
                    'preferred_backup_window': instance['PreferredBackupWindow'],
                    'preferred_maintenance_window': instance['PreferredMaintenanceWindow'],
                    'encrypted': instance['StorageEncrypted'],
                    'kms_key_id': instance.get('KmsKeyId'),
                    'deletion_protection': instance['DeletionProtection'],
                    'availability_zone': instance['AvailabilityZone'],
                    'publicly_accessible': instance['PubliclyAccessible'],
                    'vpc_security_groups': [sg['VpcSecurityGroupId'] for sg in instance['VpcSecurityGroups']],
                    'db_subnet_group': instance['DBSubnetGroup'],
                    'parameter_group': instance['DBParameterGroups'][0]['DBParameterGroupName'] if instance['DBParameterGroups'] else None,
                    'endpoint': instance['Endpoint']['Address'] if 'Endpoint' in instance else None,
                    'port': instance['Endpoint']['Port'] if 'Endpoint' in instance else None,
                    'monitoring_interval': instance['MonitoringInterval'],
                    'performance_insights_enabled': instance['PerformanceInsightsEnabled'],
                    'performance_insights_retention_period': instance.get('PerformanceInsightsRetentionPeriod'),
                    'auto_minor_version_upgrade': instance['AutoMinorVersionUpgrade'],
                    'read_replica_count': len(instance.get('ReadReplicaDBInstanceIdentifiers', [])),
                    'is_read_replica': bool(instance.get('ReadReplicaSourceDBInstanceIdentifier'))
                }
            }
            
            return config_data
            
        except ClientError as e:
            self.logger.error(f"Error collecting RDS instance configuration for {instance_id}: {e}")
            raise

    def collect_configuration_parameters(self, database_info: Dict[str, Any]) -> Dict[str, Any]:
        """Collect database configuration parameters via RDS API (no DB connection needed).

        For Aurora clusters: uses DescribeDBClusterParameters on the cluster parameter group.
        For RDS instances: uses DescribeDBParameters on the instance parameter group.

        Returns a structure compatible with the invasive collector's
        ``collect_configuration_parameters()`` output so the UI and agents
        can consume either source transparently.
        """
        db_id = database_info['identifier']
        db_type = database_info['type']
        try:
            self.logger.info(f"Collecting configuration parameters for {db_type}: {db_id}")

            parameters: List[Dict[str, Any]] = []

            if db_type in ('aurora_cluster', 'rds_multiaz_cluster'):
                # Both use a cluster parameter group via describe_db_clusters
                cluster_resp = self.rds_client.describe_db_clusters(
                    DBClusterIdentifier=db_id
                )
                cluster = cluster_resp['DBClusters'][0]
                pg_name = cluster.get('DBClusterParameterGroup', '')

                if pg_name:
                    paginator = self.rds_client.get_paginator('describe_db_cluster_parameters')
                    for page in paginator.paginate(DBClusterParameterGroupName=pg_name):
                        for p in page.get('Parameters', []):
                            parameters.append({
                                'name': p.get('ParameterName', ''),
                                'setting': p.get('ParameterValue', ''),
                                'unit': None,
                                'category': p.get('Description', ''),
                                'short_desc': p.get('Description', ''),
                                'context': 'engine-default' if p.get('Source') == 'engine-default' else 'user',
                                'vartype': p.get('DataType', ''),
                                'source': p.get('Source', ''),
                                'min_val': p.get('MinimumEngineVersion'),
                                'max_val': None,
                                'boot_val': None,
                                'reset_val': None,
                                'apply_type': p.get('ApplyType', ''),
                                'is_modifiable': p.get('IsModifiable', False),
                                'allowed_values': p.get('AllowedValues', ''),
                            })
            else:
                # RDS instance — get instance parameter group name
                inst_resp = self.rds_client.describe_db_instances(
                    DBInstanceIdentifier=db_id
                )
                instance = inst_resp['DBInstances'][0]
                pg_groups = instance.get('DBParameterGroups', [])
                pg_name = pg_groups[0]['DBParameterGroupName'] if pg_groups else ''

                if pg_name:
                    paginator = self.rds_client.get_paginator('describe_db_parameters')
                    for page in paginator.paginate(DBParameterGroupName=pg_name):
                        for p in page.get('Parameters', []):
                            parameters.append({
                                'name': p.get('ParameterName', ''),
                                'setting': p.get('ParameterValue', ''),
                                'unit': None,
                                'category': p.get('Description', ''),
                                'short_desc': p.get('Description', ''),
                                'context': 'engine-default' if p.get('Source') == 'engine-default' else 'user',
                                'vartype': p.get('DataType', ''),
                                'source': p.get('Source', ''),
                                'min_val': p.get('MinimumEngineVersion'),
                                'max_val': None,
                                'boot_val': None,
                                'reset_val': None,
                                'apply_type': p.get('ApplyType', ''),
                                'is_modifiable': p.get('IsModifiable', False),
                                'allowed_values': p.get('AllowedValues', ''),
                            })

            self.logger.info(f"Collected {len(parameters)} configuration parameters for {db_id}")
            return {
                'parameters': parameters,
                'collection_timestamp': datetime.utcnow().isoformat(),
                'source': 'rds_api',
                'parameter_group': pg_name,
            }

        except ClientError as e:
            self.logger.error(f"Error collecting configuration parameters for {db_id}: {e}")
            return {
                'parameters': [],
                'collection_timestamp': datetime.utcnow().isoformat(),
                'source': 'rds_api',
                'error': str(e),
            }

    def collect_cloudwatch_metrics(self, database_info: Dict[str, Any], days: int = 7) -> Dict[str, Any]:
        """Collect CloudWatch metrics for the database."""
        try:
            db_id = database_info['identifier']
            db_type = database_info['type']
            self.logger.info(f"Collecting CloudWatch metrics for {db_type}: {db_id}")
            
            end_time = datetime.utcnow()
            start_time = end_time - timedelta(days=days)
            
            # Choose granularity: 5-min for ≤7 days, 1-hour for ≤30 days, 1-day beyond
            if days <= 7:
                period = 600    # 10-minute: 1008 points for 7 days (limit is 1440)
            elif days <= 30:
                period = 3600   # 1-hour: 720 points max
            else:
                period = 86400  # 1-day
            
            self.logger.info(f"Using period of {period} seconds ({period//60}min) for {days} days of data")
            
            # Aurora CloudWatch metrics
            aurora_metrics = [
                # Performance metrics
                'ACUUtilization', 'CPUUtilization', 'DBLoad', 'DBLoadCPU', 'DBLoadNonCPU', 'DBLoadRelativeToNumVCPUs',
                # Memory metrics
                'FreeableMemory', 'SwapUsage',
                # I/O metrics
                'ReadIOPS', 'WriteIOPS', 'VolumeReadIOPs', 'VolumeWriteIOPs',
                'ReadLatency', 'WriteLatency', 'ReadThroughput', 'WriteThroughput',
                'DiskQueueDepth', 'TempStorageIOPS', 'TempStorageThroughput',
                # Connection metrics
                'DatabaseConnections', 'Deadlocks',
                # Replication metrics
                'AuroraReplicaLag', 'AuroraReplicaLagMaximum', 'AuroraReplicaLagMinimum',
                # Cache metrics
                'BufferCacheHitRatio',
                # Transaction metrics
                'CommitLatency', 'CommitThroughput', 'MaximumUsedTransactionIDs',
                # Storage metrics
                'VolumeBytesUsed', 'BackupRetentionPeriodStorageUsed', 'TotalBackupStorageBilled',
                'TransactionLogsDiskUsage', 'ReplicationSlotDiskUsage', 'OldestReplicationSlotLag',
                # Network metrics
                'NetworkReceiveThroughput', 'NetworkTransmitThroughput', 'NetworkThroughput',
                'StorageNetworkReceiveThroughput', 'StorageNetworkTransmitThroughput', 'StorageNetworkThroughput',
                # System metrics
                'EngineUptime', 'ServerlessDatabaseCapacity'
            ]
            
            # RDS instance metrics
            rds_metrics = [
                'CPUUtilization', 'DatabaseConnections', 'FreeableMemory',
                'ReadLatency', 'WriteLatency', 'ReadThroughput', 'WriteThroughput',
                'ReadIOPS', 'WriteIOPS',
                'BinLogDiskUsage', 'BurstBalance', 'CheckpointLag',
                'MaximumUsedTransactionIDs', 'OldestReplicationSlotLag',
                'ReplicationSlotDiskUsage', 'TransactionLogsDiskUsage', 'TransactionLogsGeneration'
            ]
            
            # Select appropriate metrics and CW dimension based on database type
            if db_type == 'aurora_cluster':
                metrics_to_collect = aurora_metrics
                dimension_name = 'DBClusterIdentifier'
                cw_id = db_id
            elif db_type == 'rds_multiaz_cluster':
                # RDS Multi-AZ DB Cluster: CW metrics are published per-instance, NOT per-cluster.
                # The writer publishes write-path metrics (WriteIOPS, CheckpointLag, TransactionLogs*).
                # Use the writer instance ID resolved from describe_db_clusters → IsClusterWriter.
                metrics_to_collect = rds_metrics
                dimension_name = 'DBInstanceIdentifier'
                writer_id = self._get_writer_instance_id(db_id)
                if writer_id:
                    cw_id = writer_id
                    self.logger.info(
                        f"RDS Multi-AZ cluster: collecting CW metrics from writer {writer_id}"
                    )
                else:
                    cw_id = db_id
                    self.logger.warning(
                        f"Could not resolve writer for {db_id} — using cluster ID (metrics may be empty)"
                    )
            else:
                metrics_to_collect = rds_metrics
                dimension_name = 'DBInstanceIdentifier'
                cw_id = db_id
            
            metrics_data = {}
            
            for metric_name in metrics_to_collect:
                try:
                    response = self.cloudwatch_client.get_metric_statistics(
                        Namespace='AWS/RDS',
                        MetricName=metric_name,
                        Dimensions=[
                            {
                                'Name': dimension_name,
                                'Value': cw_id
                            }
                        ],
                        StartTime=start_time,
                        EndTime=end_time,
                        Period=period,  # Dynamic period based on time range
                        Statistics=['Average', 'Maximum', 'Minimum']
                    )
                    
                    if response['Datapoints']:
                        metrics_data[metric_name] = {
                            'datapoints': sorted(response['Datapoints'], key=lambda x: x['Timestamp']),
                            'unit': response['Datapoints'][0]['Unit']
                        }
                        
                except ClientError as e:
                    self.logger.warning(f"Could not collect metric {metric_name}: {e}")
                    continue
            
            # correlation_categories: type-aware — Aurora metrics differ from RDS
            if db_type == 'aurora_cluster':
                correlation_categories = {
                    'performance': ['ACUUtilization', 'CPUUtilization', 'DBLoad', 'DBLoadCPU', 'DBLoadNonCPU'],
                    'memory': ['FreeableMemory', 'SwapUsage'],
                    'io': ['ReadIOPS', 'WriteIOPS', 'ReadLatency', 'WriteLatency',
                           'ReadThroughput', 'WriteThroughput', 'DiskQueueDepth'],
                    'connections': ['DatabaseConnections', 'Deadlocks'],
                    'replication': ['AuroraReplicaLag', 'AuroraReplicaLagMaximum', 'AuroraReplicaLagMinimum'],
                    'cache': ['BufferCacheHitRatio'],
                    'transactions': ['CommitLatency', 'CommitThroughput', 'MaximumUsedTransactionIDs'],
                    'storage': ['VolumeBytesUsed', 'TransactionLogsDiskUsage', 'ReplicationSlotDiskUsage'],
                    'network': ['NetworkReceiveThroughput', 'NetworkTransmitThroughput', 'NetworkThroughput'],
                }
            else:
                # RDS standalone and RDS Multi-AZ DB Cluster
                correlation_categories = {
                    'performance': ['CPUUtilization'],
                    'memory': ['FreeableMemory'],
                    'io': ['ReadIOPS', 'WriteIOPS', 'ReadLatency', 'WriteLatency',
                           'ReadThroughput', 'WriteThroughput', 'CheckpointLag'],
                    'connections': ['DatabaseConnections'],
                    'replication': ['ReplicaLag', 'OldestReplicationSlotLag', 'ReplicationSlotDiskUsage'],
                    'transactions': ['MaximumUsedTransactionIDs', 'TransactionLogsDiskUsage',
                                     'TransactionLogsGeneration'],
                    'network': ['NetworkReceiveThroughput', 'NetworkTransmitThroughput'],
                }

            return {
                'collection_period': {
                    'start_time': start_time.isoformat(),
                    'end_time': end_time.isoformat(),
                    'days': days
                },
                'metrics': metrics_data,
                'correlation_categories': correlation_categories,
            }
            
        except ClientError as e:
            self.logger.error(f"Error collecting CloudWatch metrics: {e}")
            raise

    def collect_performance_insights(self, database_info: Dict[str, Any], days: int = 7) -> Dict[str, Any]:
        """Collect Performance Insights data."""
        try:
            db_id = database_info['identifier']
            db_type = database_info['type']
            self.logger.info(f"Collecting Performance Insights data for {db_type}: {db_id}")
            
            # Get instances to find PI-enabled ones
            if db_type in ('aurora_cluster', 'rds_multiaz_cluster'):
                # Both cluster types: enumerate member instances via DBClusterIdentifier filter.
                # For rds_multiaz_cluster this correctly finds all 3 nodes by cluster ID,
                # avoiding the describe_db_instances(cluster_id) call which fails for MZ clusters.
                paginator = self.rds_client.get_paginator('describe_db_instances')
                pi_instances = [
                    instance
                    for page in paginator.paginate()
                    for instance in page['DBInstances']
                    if (instance.get('DBClusterIdentifier') == db_id and
                        instance.get('PerformanceInsightsEnabled', False))
                ]
            else:
                # For RDS instance, check if PI is enabled
                instance_response = self.rds_client.describe_db_instances(
                    DBInstanceIdentifier=db_id
                )
                instance = instance_response['DBInstances'][0]
                pi_instances = [instance] if instance.get('PerformanceInsightsEnabled', False) else []
            
            if not pi_instances:
                self.logger.warning(f"No Performance Insights enabled instances found for {db_id}")
                return {}
            
            pi_data = {}
            end_time = datetime.utcnow()
            start_time = end_time - timedelta(days=days)
            
            for instance in pi_instances:
                instance_id = instance['DBInstanceIdentifier']
                resource_id = instance['DbiResourceId']
                
                try:
                    # Define metrics to collect — os.diskIO.auroraStorage.* only available for Aurora
                    common_pi_metrics = [
                        'db.load.avg',
                        'os.cpuUtilization.user.avg', 'os.cpuUtilization.system.avg', 'os.cpuUtilization.wait.avg',
                        'os.cpuUtilization.irq.avg', 'os.cpuUtilization.nice.avg', 'os.cpuUtilization.steal.avg',
                        'os.cpuUtilization.guest.avg', 'os.cpuUtilization.idle.avg', 'os.cpuUtilization.total.avg',
                        'db.SQL.queries_started.avg', 'db.SQL.queries_finished.avg',
                        'db.SQL.tup_inserted.avg', 'db.SQL.tup_updated.avg', 'db.SQL.tup_deleted.avg',
                        'db.SQL.tup_returned.avg', 'db.SQL.tup_fetched.avg',
                        'db.Transactions.xact_commit.avg', 'db.Transactions.xact_rollback.avg',
                        'db.Transactions.active_transactions.avg', 'db.Transactions.blocked_transactions.avg',
                        'db.Transactions.max_used_xact_ids.avg',
                        'db.Cache.blks_hit.avg', 'db.IO.blks_read.avg',
                        'db.User.numbackends.avg',
                        'db.state.idle_in_transaction_count.avg', 'db.state.idle_in_transaction_aborted_count.avg',
                        'db.state.idle_in_transaction_max_time.avg', 'db.state.active_count.avg', 'db.state.idle_count.avg',
                        'db.Concurrency.deadlocks.avg',
                        'os.network.tx.avg', 'os.network.rx.avg'
                    ]
                    aurora_pi_metrics = [
                        'os.diskIO.auroraStorage.readLatency.avg', 'os.diskIO.auroraStorage.writeLatency.avg',
                        'os.diskIO.auroraStorage.readIOsPS.avg', 'os.diskIO.auroraStorage.writeIOsPS.avg',
                        'os.diskIO.auroraStorage.readThroughput.avg', 'os.diskIO.auroraStorage.writeThroughput.avg',
                        'os.diskIO.auroraStorage.diskQueueDepth.avg',
                    ]
                    all_metrics = common_pi_metrics + (aurora_pi_metrics if db_type == 'aurora_cluster' else [])
                    
                    # Split metrics into batches of 15 (API limit)
                    batch_size = 15
                    metrics_response = {'MetricList': []}
                    
                    for i in range(0, len(all_metrics), batch_size):
                        batch = all_metrics[i:i + batch_size]
                        metric_queries = [{'Metric': m} for m in batch]
                        
                        batch_response = self.pi_client.get_resource_metrics(
                            ServiceType='RDS',
                            Identifier=resource_id,
                            MetricQueries=metric_queries,
                            StartTime=start_time,
                            EndTime=end_time,
                            PeriodInSeconds=3600
                        )
                        
                        # Merge batch results
                        if 'MetricList' in batch_response:
                            metrics_response['MetricList'].extend(batch_response['MetricList'])
                    
                    # Top wait events — db.load.avg grouped by wait event, top 10
                    top_waits_response = self.pi_client.get_resource_metrics(
                        ServiceType='RDS',
                        Identifier=resource_id,
                        MetricQueries=[{
                            'Metric': 'db.load.avg',
                            'GroupBy': {'Group': 'db.wait_event', 'Limit': 10}
                        }],
                        StartTime=start_time,
                        EndTime=end_time,
                        PeriodInSeconds=3600
                    )

                    # Top SQL digests — db.load.avg grouped by tokenized SQL, top 10
                    # Includes db.sql_tokenized.id for joining with sql_digest_stats and wait breakdown
                    top_sqls_response = self.pi_client.get_resource_metrics(
                        ServiceType='RDS',
                        Identifier=resource_id,
                        MetricQueries=[{
                            'Metric': 'db.load.avg',
                            'GroupBy': {
                                'Group': 'db.sql_tokenized',
                                'Dimensions': ['db.sql_tokenized.id', 'db.sql_tokenized.statement', 'db.sql_tokenized.db_id'],
                                'Limit': 10
                            }
                        }],
                        StartTime=start_time,
                        EndTime=end_time,
                        PeriodInSeconds=3600
                    )

                    # SQL Digest statistics — describe_dimension_keys with AdditionalMetrics
                    # This gives per-SQL stats for the top 25 queries by DB load.
                    # We collect per_call metrics (normalized per execution) + calls_per_sec
                    # (frequency). per_sec = per_call × calls_per_sec, so collecting both
                    # would be redundant. Exception: total_time_per_sec (AAE) represents
                    # concurrent execution time and can't be derived from per_call alone.
                    # Ref: https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/USER_PerfInsights.UsingDashboard.AnalyzeDBLoad.AdditionalMetrics.PostgreSQL.html
                    sql_stats_metrics = [
                        # Throughput — how often the query runs
                        'db.sql_tokenized.stats.calls_per_sec.avg',
                        # Total DB time (AAE) — overall DB time consumption, not derivable from per_call
                        'db.sql_tokenized.stats.total_time_per_sec.avg',
                        # Per-call efficiency — cost per execution, independent of frequency
                        'db.sql_tokenized.stats.rows_per_call.avg',
                        'db.sql_tokenized.stats.avg_latency_per_call.avg',
                        'db.sql_tokenized.stats.shared_blks_hit_per_call.avg',
                        'db.sql_tokenized.stats.shared_blks_read_per_call.avg',
                        'db.sql_tokenized.stats.shared_blks_written_per_call.avg',
                        'db.sql_tokenized.stats.shared_blks_dirtied_per_call.avg',
                        # I/O timing per call — identifies I/O-bound queries (requires track_io_timing=on)
                        'db.sql_tokenized.stats.blk_read_time_per_call.avg',
                        'db.sql_tokenized.stats.blk_write_time_per_call.avg',
                        # Temp I/O per call — identifies queries spilling to disk
                        'db.sql_tokenized.stats.temp_blks_read_per_call.avg',
                        'db.sql_tokenized.stats.temp_blks_written_per_call.avg',
                    ]
                    try:
                        sql_digest_keys = self.pi_client.describe_dimension_keys(
                            ServiceType='RDS',
                            Identifier=resource_id,
                            StartTime=start_time,
                            EndTime=end_time,
                            Metric='db.load.avg',
                            GroupBy={
                                'Group': 'db.sql_tokenized',
                                'Dimensions': ['db.sql_tokenized.id', 'db.sql_tokenized.statement', 'db.sql_tokenized.db_id'],
                                'Limit': 25
                            },
                            AdditionalMetrics=sql_stats_metrics,
                        )
                    except Exception as e:
                        self.logger.warning(f"Could not get SQL digest stats: {e}")
                        sql_digest_keys = {'Keys': []}

                    # Top databases — db.load.avg grouped by database name, top 10
                    top_dbs_response = self.pi_client.get_resource_metrics(
                        ServiceType='RDS',
                        Identifier=resource_id,
                        MetricQueries=[{
                            'Metric': 'db.load.avg',
                            'GroupBy': {'Group': 'db', 'Limit': 10}
                        }],
                        StartTime=start_time,
                        EndTime=end_time,
                        PeriodInSeconds=3600
                    )

                    # Top applications — db.load.avg grouped by application name, top 10
                    top_apps_response = self.pi_client.get_resource_metrics(
                        ServiceType='RDS',
                        Identifier=resource_id,
                        MetricQueries=[{
                            'Metric': 'db.load.avg',
                            'GroupBy': {'Group': 'db.application', 'Limit': 10}
                        }],
                        StartTime=start_time,
                        EndTime=end_time,
                        PeriodInSeconds=3600
                    )

                    # Per-SQL wait event breakdown — for stacked bar visualization
                    # For each top SQL with an ID, fetch wait events filtered to that SQL
                    sql_wait_breakdown = {}
                    for entry in top_sqls_response.get('MetricList', []):
                        sql_id = entry.get('Key', {}).get('Dimensions', {}).get('db.sql_tokenized.id')
                        if not sql_id:
                            continue
                        try:
                            wait_resp = self.pi_client.get_resource_metrics(
                                ServiceType='RDS',
                                Identifier=resource_id,
                                MetricQueries=[{
                                    'Metric': 'db.load.avg',
                                    'GroupBy': {'Group': 'db.wait_event', 'Limit': 5},
                                    'Filter': {'db.sql_tokenized.id': sql_id}
                                }],
                                StartTime=start_time,
                                EndTime=end_time,
                                PeriodInSeconds=3600
                            )
                            sql_wait_breakdown[sql_id] = wait_resp.get('MetricList', [])
                        except Exception as e:
                            self.logger.warning(f"Could not get wait breakdown for SQL {sql_id[:8]}: {e}")

                    pi_data[instance_id] = {
                        'resource_id': resource_id,
                        'metrics': metrics_response,
                        'top_waits': top_waits_response.get('MetricList', []),
                        'top_sqls': top_sqls_response.get('MetricList', []),
                        'sql_digest_stats': sql_digest_keys.get('Keys', []),
                        'sql_wait_breakdown': sql_wait_breakdown,
                        'top_databases': top_dbs_response.get('MetricList', []),
                        'top_applications': top_apps_response.get('MetricList', []),
                        'correlation_categories': {
                            'performance': ['db.load.avg', 'db.load.cpu.avg', 'db.load.non_cpu.avg'],
                            'cpu': ['os.cpuUtilization.user.avg', 'os.cpuUtilization.system.avg', 'os.cpuUtilization.wait.avg'],
                            'io': ['os.diskIO.auroraStorage.readLatency.avg', 'os.diskIO.auroraStorage.writeLatency.avg', 
                                   'os.diskIO.auroraStorage.readIOsPS.avg', 'os.diskIO.auroraStorage.writeIOsPS.avg'],
                            'transactions': ['db.Transactions.xact_commit.avg', 'db.Transactions.xact_rollback.avg', 
                                           'db.Transactions.active_transactions.avg', 'db.Transactions.blocked_transactions.avg'],
                            'cache': ['db.Cache.blks_hit.avg', 'db.IO.blks_read.avg'],
                            'connections': ['db.User.numbackends.avg', 'db.state.idle_in_transaction_count.avg']
                        }
                    }
                    
                except ClientError as e:
                    self.logger.warning(f"Could not collect PI data for {instance_id}: {e}")
                    continue
            
            return pi_data
            
        except ClientError as e:
            self.logger.error(f"Error collecting Performance Insights data: {e}")
            return {}

    def collect_database_data(self, database_info: Dict[str, Any]) -> Dict[str, Any]:
        """Collect all non-invasive data for a single database."""
        db_id = database_info['identifier']
        db_type = database_info['type']
        self.logger.info(f"Starting non-invasive data collection for {db_type}: {db_id}")
        
        collected_data = {
            'collection_timestamp': datetime.utcnow().isoformat(),
            'database_id': db_id,
            'database_type': db_type,
            'region': self.region,
            'collection_type': 'non_invasive',
            'wal_framework': database_info['wal_framework']
        }
        
        try:
            # Collect database configuration
            collected_data['configuration'] = self.collect_database_configuration(database_info)
            
            # Collect configuration parameters (pg_settings equivalent via RDS API)
            collected_data['configuration_parameters'] = self.collect_configuration_parameters(database_info)
            
            # Collect CloudWatch metrics
            collected_data['cloudwatch_metrics'] = self.collect_cloudwatch_metrics(database_info)
            
            # Collect Performance Insights data
            collected_data['performance_insights'] = self.collect_performance_insights(database_info)
            
            # Apply PII redaction before writing to disk
            if not getattr(self, '_skip_redaction', False):
                try:
                    from utils.pii_redactor import PiiRedactor
                except ImportError:
                    try:
                        # Resolve path when running from symlink or different cwd
                        _script_dir = os.path.dirname(os.path.realpath(__file__))
                        sys.path.insert(0, os.path.dirname(_script_dir))
                        from utils.pii_redactor import PiiRedactor
                    except ImportError:
                        self.logger.warning("PII redaction skipped — utils/pii_redactor.py not found")
                        PiiRedactor = None
                if PiiRedactor:
                    redactor = PiiRedactor()
                    collected_data, _ = redactor.redact(collected_data)
                    self.logger.info("PII redaction applied")
            
            # Save to file
            output_file = os.path.join(self.output_dir, f"{db_id}_non_invasive_data.json")
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(collected_data, f, indent=2, default=str)
            
            self.logger.info(f"Data collection completed for {db_id}. Output saved to {output_file}")
            return collected_data
            
        except Exception as e:
            self.logger.error(f"Error during data collection for {db_id}: {e}")
            raise

    def collect_fleet_data(self, fleet: List[Dict[str, Any]], max_workers: int = 4) -> List[Dict[str, Any]]:
        """Collect data for multiple databases in parallel."""
        self.logger.info(f"Starting fleet data collection for {len(fleet)} databases")
        
        results = []
        failed_databases = []
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Submit all collection tasks
            future_to_db = {
                executor.submit(self.collect_database_data, db_info): db_info 
                for db_info in fleet
            }
            
            # Process completed tasks
            for future in as_completed(future_to_db):
                db_info = future_to_db[future]
                try:
                    result = future.result()
                    results.append(result)
                    self.logger.info(f"✅ Completed collection for {db_info['identifier']}")
                except Exception as e:
                    self.logger.error(f"❌ Failed collection for {db_info['identifier']}: {e}")
                    failed_databases.append(db_info['identifier'])
        
        # Generate fleet summary
        fleet_summary = {
            'collection_timestamp': datetime.utcnow().isoformat(),
            'region': self.region,
            'total_databases': len(fleet),
            'successful_collections': len(results),
            'failed_collections': len(failed_databases),
            'failed_databases': failed_databases,
            'collection_type': 'fleet_non_invasive'
        }
        
        # Save fleet summary
        summary_file = os.path.join(self.output_dir, 'fleet_collection_summary.json')
        with open(summary_file, 'w', encoding='utf-8') as f:
            json.dump(fleet_summary, f, indent=2, default=str)
        
        self.logger.info(f"Fleet collection completed. {len(results)}/{len(fleet)} successful")
        return results


def main():
    parser = argparse.ArgumentParser(description='Non-invasive PostgreSQL data collector')
    
    # Single database options
    parser.add_argument('--database-id', help='Specific database identifier (cluster or instance)')
    parser.add_argument('--database-type', choices=['aurora_cluster', 'rds_instance', 'rds_multiaz_cluster'],
                       help='Type of database (required with --database-id)')
    
    # Fleet options
    parser.add_argument('--fleet', action='store_true', help='Collect data for entire fleet')
    parser.add_argument('--include-aurora', action='store_true', default=True, 
                       help='Include Aurora clusters in fleet discovery')
    parser.add_argument('--include-rds', action='store_true', default=True, 
                       help='Include RDS instances in fleet discovery')
    parser.add_argument('--include-databases', nargs='*', default=[],
                       help='Specific database identifiers to include (whitelist)')
    parser.add_argument('--exclude-databases', nargs='*', default=[], 
                       help='Database identifiers to exclude from collection')
    parser.add_argument('--identifier-pattern', help='Only include databases matching this pattern')
    parser.add_argument('--required-tags', nargs='*', default=[],
                       help='Required tags in key=value format (use key=* for any value)')
    
    # Common options
    parser.add_argument('--region', required=True, help='AWS region')
    parser.add_argument('--output-dir', default='./data', help='Output directory for collected data')
    parser.add_argument('--days', type=int, default=7, help='Number of days of metrics to collect')
    parser.add_argument('--max-workers', type=int, default=4, help='Maximum parallel workers for fleet collection')
    parser.add_argument('--no-redact', action='store_true',
                        help='Skip PII redaction (endpoints, client IPs, KMS ARNs). '
                             'Use only if you need the raw data for internal analysis.')
    
    args = parser.parse_args()
    
    # Validate arguments
    if not args.fleet and not args.database_id:
        print("❌ Either --fleet or --database-id must be specified")
        return 1
    
    if args.database_id and not args.database_type:
        print("❌ --database-type is required when using --database-id")
        return 1
    
    try:
        collector = NonInvasiveCollector(args.region, args.output_dir)
        collector._skip_redaction = args.no_redact
        
        if args.fleet:
            # Fleet collection
            print(f"🔍 Discovering PostgreSQL databases in region {args.region}...")
            
            discovery = FleetDiscovery(args.region)
            fleet = discovery.discover_fleet(
                include_aurora=args.include_aurora,
                include_rds=args.include_rds
            )
            
            if not fleet:
                print("❌ No PostgreSQL databases found in the region")
                return 1
            
            # Apply filters
            filters = {}
            if args.include_databases:
                filters['include_identifiers'] = args.include_databases
            if args.exclude_databases:
                filters['exclude_identifiers'] = args.exclude_databases
            if args.identifier_pattern:
                filters['identifier_pattern'] = args.identifier_pattern
            if args.required_tags:
                # Parse key=value pairs
                tag_dict = {}
                for tag in args.required_tags:
                    if '=' in tag:
                        key, value = tag.split('=', 1)
                        tag_dict[key] = value
                    else:
                        tag_dict[tag] = '*'  # Any value
                filters['required_tags'] = tag_dict
            
            if filters:
                fleet = discovery.filter_fleet(fleet, filters)
            
            if not fleet:
                print("❌ No databases remaining after applying filters")
                return 1
            
            print(f"📊 Starting data collection for {len(fleet)} databases...")
            results = collector.collect_fleet_data(fleet, args.max_workers)
            
            print(f"✅ Fleet data collection completed. {len(results)}/{len(fleet)} successful")
            
        else:
            # Single database collection
            database_info = {
                'identifier': args.database_id,
                'type': args.database_type,
                'wal_framework': (
                    'AuroraPostgreSQL_CustomLens_v1.json'
                    if args.database_type == 'aurora_cluster'
                    else 'RDS_PostgreSQL_CustomLens_v1.json'
                )
            }
            
            collector.collect_database_data(database_info)
            print(f"✅ Data collection completed successfully for {args.database_type}: {args.database_id}")
        
    except NoCredentialsError:
        print("❌ AWS credentials not found. Please configure AWS CLI or set environment variables.")
        return 1
    except Exception as e:
        print(f"❌ Error during data collection: {e}")
        return 1
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
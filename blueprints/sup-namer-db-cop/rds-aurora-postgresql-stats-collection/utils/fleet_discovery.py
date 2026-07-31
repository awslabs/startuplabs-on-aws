#!/usr/bin/env python3
"""
Fleet discovery module for PostgreSQL databases.
Discovers Aurora clusters, RDS Multi-AZ DB Clusters, and standalone RDS instances.
"""

import logging
from typing import Dict, List, Any, Tuple
import boto3
from botocore.exceptions import ClientError


class FleetDiscovery:
    def __init__(self, region: str):
        self.region = region
        self.rds_client = boto3.client('rds', region_name=region)
        self.logger = logging.getLogger(__name__)

    def discover_rds_multiaz_clusters(self) -> List[Dict[str, Any]]:
        """Discover RDS Multi-AZ DB Clusters (engine='postgres', not 'aurora-postgresql').

        These use the same cluster API as Aurora but are standard PostgreSQL with
        synchronous streaming replication to 2 readable standbys. Their member
        instances share a DBClusterIdentifier but must NOT be collected as
        standalone rds_instance entries — the cluster is the top-level entity.
        """
        try:
            self.logger.info("Discovering RDS Multi-AZ DB Clusters...")

            paginator = self.rds_client.get_paginator('describe_db_clusters')
            clusters = []

            for page in paginator.paginate(
                Filters=[{'Name': 'engine', 'Values': ['postgres']}]
            ):
                for cluster in page['DBClusters']:
                    tags = self._get_cluster_tags(cluster['DBClusterArn'])
                    clusters.append({
                        'identifier': cluster['DBClusterIdentifier'],
                        'engine': cluster['Engine'],
                        'engine_version': cluster['EngineVersion'],
                        'status': cluster['Status'],
                        'multi_az': cluster['MultiAZ'],
                        'instance_class': cluster.get('DBClusterInstanceClass', 'N/A'),
                        'type': 'rds_multiaz_cluster',
                        'wal_framework': 'RDS_PostgreSQL_CustomLens_v1.json',
                        'tags': tags,
                        'arn': cluster['DBClusterArn'],
                    })

            self.logger.info(f"Found {len(clusters)} RDS Multi-AZ DB Clusters")
            return clusters

        except ClientError as e:
            self.logger.error(f"Error discovering RDS Multi-AZ clusters: {e}")
            return []

    def discover_aurora_clusters(self) -> List[Dict[str, Any]]:
        """Discover Aurora PostgreSQL clusters."""
        try:
            self.logger.info("Discovering Aurora PostgreSQL clusters...")
            
            paginator = self.rds_client.get_paginator('describe_db_clusters')
            aurora_clusters = []

            for page in paginator.paginate(Filters=[{'Name': 'engine', 'Values': ['aurora-postgresql']}]):
              for cluster in page['DBClusters']:
                tags = self._get_cluster_tags(cluster['DBClusterArn'])
                aurora_clusters.append({
                    'identifier': cluster['DBClusterIdentifier'],
                    'engine': cluster['Engine'],
                    'engine_version': cluster['EngineVersion'],
                    'status': cluster['Status'],
                    'multi_az': cluster['MultiAZ'],
                    'type': 'aurora_cluster',
                    'wal_framework': 'AuroraPostgreSQL_CustomLens_v1.json',
                    'tags': tags,
                    'arn': cluster['DBClusterArn']
                })
            
            self.logger.info(f"Found {len(aurora_clusters)} Aurora PostgreSQL clusters")
            return aurora_clusters
            
        except ClientError as e:
            self.logger.error(f"Error discovering Aurora clusters: {e}")
            return []

    def discover_rds_instances(self) -> List[Dict[str, Any]]:
        """Discover RDS PostgreSQL instances."""
        try:
            self.logger.info("Discovering RDS PostgreSQL instances...")
            
            paginator = self.rds_client.get_paginator('describe_db_instances')
            rds_instances = []

            for page in paginator.paginate(Filters=[{'Name': 'engine', 'Values': ['postgres']}]):
              for instance in page['DBInstances']:
                # Skip instances that belong to an Aurora cluster — those are collected
                # via discover_aurora_clusters.
                # Also skip instances that belong to an RDS Multi-AZ DB Cluster — those
                # are collected via discover_rds_multiaz_clusters as a cluster entity.
                cluster_id = instance.get('DBClusterIdentifier')
                if cluster_id:
                    try:
                        cluster_resp = self.rds_client.describe_db_clusters(
                            DBClusterIdentifier=cluster_id
                        )
                        cluster_engine = cluster_resp['DBClusters'][0].get('Engine', '')
                        # Skip Aurora members AND RDS Multi-AZ DB Cluster members
                        if cluster_engine.startswith('aurora') or cluster_engine == 'postgres':
                            continue
                    except ClientError:
                        pass  # If we can't describe the cluster, include the instance
                tags = self._get_instance_tags(instance['DBInstanceArn'])
                rds_instances.append({
                    'identifier': instance['DBInstanceIdentifier'],
                    'engine': instance['Engine'],
                    'engine_version': instance['EngineVersion'],
                    'status': instance['DBInstanceStatus'],
                    'multi_az': instance['MultiAZ'],
                    'instance_class': instance['DBInstanceClass'],
                    'type': 'rds_instance',
                    'wal_framework': 'RDS_PostgreSQL_CustomLens_v1.json',
                    'tags': tags,
                    'arn': instance['DBInstanceArn']
                })
            
            self.logger.info(f"Found {len(rds_instances)} RDS PostgreSQL instances")
            return rds_instances
            
        except ClientError as e:
            self.logger.error(f"Error discovering RDS instances: {e}")
            return []

    def discover_fleet(self, include_aurora: bool = True, include_rds: bool = True) -> List[Dict[str, Any]]:
        """Discover all PostgreSQL databases in the fleet."""
        fleet = []

        if include_aurora:
            fleet.extend(self.discover_aurora_clusters())

        if include_rds:
            # RDS Multi-AZ DB Clusters first — their member instances are then skipped
            # by discover_rds_instances() to avoid duplicate/disconnected entries.
            fleet.extend(self.discover_rds_multiaz_clusters())
            fleet.extend(self.discover_rds_instances())
        
        # Filter by status (only include available databases)
        active_fleet = [db for db in fleet if db['status'] in ['available', 'backing-up', 'modifying']]
        
        self.logger.info(f"Total active PostgreSQL databases found: {len(active_fleet)}")
        return active_fleet

    def filter_fleet(self, fleet: List[Dict[str, Any]], filters: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Filter fleet based on criteria."""
        filtered_fleet = fleet.copy()
        
        # Filter by specific database identifiers (include list)
        if 'include_identifiers' in filters:
            include_list = filters['include_identifiers']
            filtered_fleet = [db for db in filtered_fleet 
                            if db['identifier'] in include_list]
        
        # Filter by tags
        if 'required_tags' in filters:
            required_tags = filters['required_tags']
            filtered_fleet = [db for db in filtered_fleet 
                            if self._matches_tags(db.get('tags', {}), required_tags)]
        
        # Filter by engine version
        if 'min_engine_version' in filters:
            min_version = filters['min_engine_version']
            filtered_fleet = [db for db in filtered_fleet 
                            if self._compare_versions(db['engine_version'], min_version) >= 0]
        
        # Filter by database type
        if 'database_types' in filters:
            allowed_types = filters['database_types']
            filtered_fleet = [db for db in filtered_fleet if db['type'] in allowed_types]
        
        # Filter by identifier pattern
        if 'identifier_pattern' in filters:
            pattern = filters['identifier_pattern'].lower()
            filtered_fleet = [db for db in filtered_fleet 
                            if pattern in db['identifier'].lower()]
        
        # Exclude specific databases
        if 'exclude_identifiers' in filters:
            exclude_list = filters['exclude_identifiers']
            filtered_fleet = [db for db in filtered_fleet 
                            if db['identifier'] not in exclude_list]
        
        self.logger.info(f"Fleet filtered from {len(fleet)} to {len(filtered_fleet)} databases")
        return filtered_fleet

    def _compare_versions(self, version1: str, version2: str) -> int:
        """Compare two version strings. Returns -1, 0, or 1."""
        def version_tuple(v):
            return tuple(map(int, (v.split("."))))
        
        try:
            v1_tuple = version_tuple(version1)
            v2_tuple = version_tuple(version2)
            
            if v1_tuple < v2_tuple:
                return -1
            elif v1_tuple > v2_tuple:
                return 1
            else:
                return 0
        except ValueError:
            # If version parsing fails, assume they're equal
            return 0

    def get_database_details(self, database: Dict[str, Any]) -> Dict[str, Any]:
        """Get detailed information about a specific database."""
        try:
            if database['type'] == 'aurora_cluster':
                response = self.rds_client.describe_db_clusters(
                    DBClusterIdentifier=database['identifier']
                )
                return response['DBClusters'][0]
            else:
                response = self.rds_client.describe_db_instances(
                    DBInstanceIdentifier=database['identifier']
                )
                return response['DBInstances'][0]
                
        except ClientError as e:
            self.logger.error(f"Error getting details for {database['identifier']}: {e}")
            return {}

    def _get_cluster_tags(self, cluster_arn: str) -> Dict[str, str]:
        """Get tags for Aurora cluster."""
        try:
            response = self.rds_client.list_tags_for_resource(ResourceName=cluster_arn)
            return {tag['Key']: tag['Value'] for tag in response.get('TagList', [])}
        except ClientError as e:
            self.logger.warning(f"Could not get tags for cluster {cluster_arn}: {e}")
            return {}

    def _get_instance_tags(self, instance_arn: str) -> Dict[str, str]:
        """Get tags for RDS instance."""
        try:
            response = self.rds_client.list_tags_for_resource(ResourceName=instance_arn)
            return {tag['Key']: tag['Value'] for tag in response.get('TagList', [])}
        except ClientError as e:
            self.logger.warning(f"Could not get tags for instance {instance_arn}: {e}")
            return {}

    def _matches_tags(self, db_tags: Dict[str, str], required_tags: Dict[str, str]) -> bool:
        """Check if database tags match required tags."""
        for key, value in required_tags.items():
            if key not in db_tags:
                return False
            if value != '*' and db_tags[key] != value:
                return False
        return True
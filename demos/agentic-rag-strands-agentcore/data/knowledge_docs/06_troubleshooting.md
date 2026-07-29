# NovaTech Troubleshooting Guide

## Common Issues & Solutions

### Training Jobs

#### Issue: Training job stuck in PENDING state

**Symptoms:** Job status remains PENDING for more than 15 minutes.

**Root Causes:**
1. Insufficient GPU capacity in the selected region
2. IAM permissions missing for the training role
3. Dataset not accessible from the training instance

**Solutions:**
1. Check region capacity: Navigate to Settings → Regions → Capacity Dashboard
2. Verify IAM role: Ensure the training role has `s3:GetObject` on the dataset bucket
3. Try alternative instance types or regions
4. Contact support if issue persists beyond 1 hour

#### Issue: Out of Memory (OOM) during training

**Symptoms:** Job fails with `CUDA out of memory` or `RuntimeError: DataLoader worker killed`

**Solutions:**
1. Reduce batch size (try halving it)
2. Enable gradient accumulation: set `gradient_accumulation_steps: 4`
3. Use mixed-precision training: set `fp16: true`
4. Upgrade to a larger GPU instance (e.g., g5.2xlarge → g5.4xlarge)
5. Enable gradient checkpointing for transformer models

#### Issue: Training metrics not appearing in dashboard

**Symptoms:** Training is running but metrics tab shows "No data available"

**Solutions:**
1. Verify your training script calls `nova.log_metrics()` or uses the MLflow callback
2. Check that the metrics reporting interval isn't too long (default: every 100 steps)
3. Ensure network connectivity from training instance to the metrics service
4. Review training logs for metric reporting errors

---

### Deployments

#### Issue: Model endpoint returning 503 errors

**Symptoms:** Intermittent or consistent 503 Service Unavailable responses

**Root Causes:**
1. Instances are scaling (cold start during scale-out)
2. Model loading failure on new instances
3. Health check failures

**Solutions:**
1. Check scaling events in the deployment dashboard
2. Review instance logs for model loading errors
3. Increase `min_instances` to avoid cold starts
4. Verify model artifact integrity in the registry
5. Adjust health check timeout if model loading is slow (set `health_check_timeout: 120`)

#### Issue: High inference latency (> 500ms p99)

**Symptoms:** Prediction responses consistently exceed latency targets

**Solutions:**
1. Enable model compilation (TorchScript or ONNX)
2. Use batched inference for throughput-optimized workloads
3. Upgrade instance type (consider Inferentia for transformer models)
4. Enable response caching for repeated inputs
5. Review input preprocessing — move heavy transforms to feature store
6. Profile the model: `nova predict --profile --endpoint your-endpoint`

---

### Feature Store

#### Issue: Feature freshness lag exceeding SLA

**Symptoms:** Online features are stale (last_updated timestamp older than expected)

**Solutions:**
1. Check feature pipeline execution logs
2. Verify source data is being updated
3. Review CDC (Change Data Capture) connector status
4. Manually trigger feature refresh: `nova features refresh --group your-group`
5. Check Redis cluster health for online store issues

#### Issue: Point-in-time join producing NULL values

**Symptoms:** Training dataset has unexpected NULL values in feature columns

**Solutions:**
1. Verify event timestamp column is correctly configured
2. Check that feature computation timestamps align with the training window
3. Ensure backfill has completed for the required time range
4. Review feature pipeline for filtering logic that may exclude records

---

### Data & Connectivity

#### Issue: Connector failing to sync

**Symptoms:** Data source shows "Sync Failed" status

**Solutions by connector type:**

| Connector | Common Fix |
|-----------|-----------|
| Snowflake | Verify warehouse is not suspended; check IP allowlist |
| Redshift | Confirm VPC peering is active; check security group |
| PostgreSQL | Verify SSL certificate is valid; check max_connections |
| S3 | Check IAM role trust policy; verify bucket policy |
| Kafka | Confirm consumer group isn't stuck; check topic permissions |

#### Issue: Schema drift detected

**Symptoms:** Alert: "Schema drift detected in source: column_x type changed"

**Solutions:**
1. Review the schema change in the source system
2. If intentional: Update the connector schema mapping in Settings → Connectors
3. If unintentional: Coordinate with the source data team
4. Enable automatic schema evolution if appropriate for your use case

---

## Getting Help

### Support Tiers

| Tier | Response Time | Channels |
|------|--------------|----------|
| Standard | < 24 hours | Email, Portal |
| Premium | < 4 hours | Email, Portal, Slack |
| Enterprise | < 1 hour | Email, Portal, Slack, Phone |

### Contact

- **Support Portal**: https://support.novatech.io
- **Email**: support@novatech.io
- **Status Page**: https://status.novatech.io
- **Community Forum**: https://community.novatech.io

# NovaTech Release Notes

## NovaPlatform v3.5.0 (June 2025)

### New Features
- **Streaming Inference**: Real-time token streaming for deployed models via Server-Sent Events (SSE)
- **Multi-Region Deployments**: Deploy model endpoints across multiple AWS regions with automatic geo-routing
- **Custom Metrics**: Define and track custom business metrics alongside standard ML performance metrics
- **Dataset Versioning**: Full dataset version control with branching and tagging support

### Improvements
- Training job scheduling now supports cron expressions for recurring jobs
- Feature Store offline queries are 40% faster with optimized Parquet scanning
- Model Registry UI redesigned with improved lineage visualization
- Reduced cold start time for GPU instances by 35%

### Bug Fixes
- Fixed race condition in canary deployment rollback logic
- Resolved memory leak in real-time inference endpoint health checks
- Fixed incorrect token count in usage reports for batched requests
- Resolved issue where training metrics would lag by 2-3 minutes in the dashboard

---

## NovaInsight v2.2.0 (May 2025)

### New Features
- **Chart Annotations**: Add bookmarks and notes to any visualization
- **Custom Dashboards**: Build multi-widget dashboards with drag-and-drop
- **Databricks SQL Connector**: New beta connector for Databricks SQL Warehouse
- **Query History**: Full query history with replay and sharing capabilities

### Improvements
- Natural language query accuracy improved by 15% for complex multi-join queries
- Added support for window functions in generated SQL
- Semantic layer now supports calculated dimensions
- PDF report export quality improved with vector graphics

### Bug Fixes
- Fixed timezone handling for scheduled reports in non-UTC timezones
- Resolved issue where follow-up queries lost context after 5+ messages
- Fixed embedding error for very short queries (< 3 words)
- Corrected data source sync status indicator showing stale information

---

## NovaGuard v1.4.0 (April 2025)

### New Features
- **EU AI Act Compliance Module**: Pre-built assessment templates for the EU AI Act
- **Automated Bias Remediation**: Suggested mitigation strategies with one-click implementation
- **Multi-Model Comparison**: Compare fairness metrics across multiple model versions
- **Natural Language Audit Queries**: Ask questions about audit results in plain English

### Improvements
- SHAP computation is 3x faster with parallel execution
- Added support for multi-class classification fairness metrics
- Compliance report generation time reduced from 10 minutes to under 2 minutes
- Improved counterfactual explanation generation for tabular data

### Bug Fixes
- Fixed SHAP value calculation for models with > 100 features
- Resolved timeout issue in batch auditing for large datasets (> 1M rows)
- Fixed PDF export layout issue with wide tables in compliance reports
- Corrected equalized odds calculation for multi-class scenarios

---

## Platform Updates (All Products)

### Security
- SOC 2 Type II certification renewed (May 2025)
- Added support for AWS PrivateLink across all services
- Implemented MFA enforcement for API key management
- Audit logs now retained for 7 years (up from 2 years)

### Performance
- Global CDN for UI assets — 50% faster page loads worldwide
- Database connection pooling improvements — 30% fewer connection errors under load
- Upgraded to Python 3.12 runtime for all serverless workloads

### Deprecations
- NovaPlatform v2 API endpoints will be sunset on December 31, 2025
- Legacy CSV data import (use Parquet or connector-based ingestion instead)
- Basic auth for API access (migrate to OAuth 2.0 client credentials)

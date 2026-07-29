# NovaInsight - Natural Language BI Platform

## Overview

NovaInsight enables business users to query enterprise data using natural language. Instead of writing SQL or navigating complex dashboards, users simply ask questions like "What was our Q3 revenue by region?" and receive instant visualizations and data summaries.

## How It Works

### Query Pipeline

1. **Natural Language Understanding (NLU)**: User questions are parsed using a fine-tuned LLM that understands business terminology, metric definitions, and temporal references.
2. **Schema Mapping**: The NLU output is mapped to the connected data warehouse schema using a semantic layer that defines business metrics, dimensions, and relationships.
3. **Query Generation**: SQL/query language is generated based on the mapped schema and optimized for the target database engine.
4. **Execution & Caching**: Queries are executed against the data warehouse with intelligent caching to serve repeated patterns instantly.
5. **Visualization**: Results are automatically visualized using the most appropriate chart type (bar, line, pie, table, etc.) based on the data structure.

### Semantic Layer

The semantic layer is the heart of NovaInsight. It defines:

- **Metrics**: Calculated measures (e.g., Revenue = SUM(order_amount), Churn Rate = COUNT(churned) / COUNT(total))
- **Dimensions**: Categorical fields for slicing data (e.g., region, product_category, customer_segment)
- **Time Grains**: Supported temporal aggregations (daily, weekly, monthly, quarterly, yearly)
- **Relationships**: Join paths between tables for multi-table queries
- **Synonyms**: Alternative names users might use (e.g., "sales" → "revenue", "clients" → "customers")

### Supported Data Sources

| Source | Connector Status | Real-time Sync |
|--------|-----------------|----------------|
| Snowflake | GA | Yes |
| Amazon Redshift | GA | Yes |
| Google BigQuery | GA | Yes |
| PostgreSQL | GA | Yes (CDC) |
| MySQL | GA | Yes (CDC) |
| Databricks SQL | Beta | Yes |
| Microsoft Fabric | Beta | No |
| MongoDB Atlas | GA | Yes |

## Features

### Conversational Follow-ups
Users can refine queries conversationally:
- "Show me Q3 revenue" → chart appears
- "Break that down by region" → chart updates
- "Just the top 5 regions" → filters applied
- "Compare with Q2" → comparison view

### Scheduled Reports
- Automated report generation on daily/weekly/monthly cadence
- Email delivery with PDF/CSV attachments
- Slack/Teams integration for scheduled insights

### Data Governance
- Row-level security aligned with your data warehouse permissions
- Audit logging of all queries
- PII masking for sensitive fields
- SOC 2 Type II certified

### Embedding
NovaInsight can be embedded into your own applications:
```html
<iframe
  src="https://insight.novatech.io/embed/dashboard/d-123"
  width="100%"
  height="600"
  allow="clipboard-write"
></iframe>
```

## Pricing

| Plan | Users | Queries/Month | Price |
|------|-------|---------------|-------|
| Team | Up to 25 | 10,000 | $1,500/mo |
| Business | Up to 100 | 50,000 | $5,000/mo |
| Enterprise | Unlimited | Unlimited | Custom |

## Getting Started

1. Connect your data warehouse (5-minute setup wizard)
2. Define your semantic layer (or use AI-assisted auto-detection)
3. Invite your team and start asking questions

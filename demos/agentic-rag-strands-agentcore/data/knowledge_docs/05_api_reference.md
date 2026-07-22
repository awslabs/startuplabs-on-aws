# NovaTech API Reference

## Authentication

All API requests require authentication using Bearer tokens. Obtain tokens via the OAuth 2.0 client credentials flow.

```
POST /auth/token
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials&client_id=YOUR_CLIENT_ID&client_secret=YOUR_CLIENT_SECRET
```

**Response:**
```json
{
  "access_token": "eyJhbGciOiJSUzI1NiIs...",
  "token_type": "Bearer",
  "expires_in": 3600,
  "scope": "platform:read platform:write"
}
```

## Rate Limits

| Plan | Requests/Minute | Burst Limit |
|------|----------------|-------------|
| Starter | 100 | 150 |
| Professional | 500 | 750 |
| Enterprise | 2000 | 3000 |

Rate limit headers are included in all responses:
- `X-RateLimit-Limit`: Maximum requests per window
- `X-RateLimit-Remaining`: Remaining requests in current window
- `X-RateLimit-Reset`: Unix timestamp when the window resets

## Error Handling

All errors follow RFC 7807 Problem Details format:

```json
{
  "type": "https://api.novatech.io/errors/validation-failed",
  "title": "Validation Failed",
  "status": 422,
  "detail": "The 'learning_rate' parameter must be between 0.0001 and 1.0",
  "instance": "/api/v2/training/jobs/tj-789xyz",
  "errors": [
    {
      "field": "hyperparameters.learning_rate",
      "message": "Value 5.0 exceeds maximum of 1.0"
    }
  ]
}
```

## Common Error Codes

| Status | Type | Description |
|--------|------|-------------|
| 400 | bad-request | Malformed request body or invalid parameters |
| 401 | unauthorized | Missing or invalid authentication token |
| 403 | forbidden | Insufficient permissions for the requested resource |
| 404 | not-found | Resource does not exist |
| 409 | conflict | Resource state conflict (e.g., deploying already-deployed model) |
| 422 | validation-failed | Request body validation errors |
| 429 | rate-limited | Too many requests |
| 500 | internal-error | Unexpected server error |
| 503 | service-unavailable | Service temporarily unavailable |

## Webhooks

NovaTech supports webhooks for asynchronous event notifications.

### Supported Events

- `training.started` — Training job has begun execution
- `training.completed` — Training job finished successfully
- `training.failed` — Training job encountered an error
- `deployment.active` — Model endpoint is serving traffic
- `deployment.scaled` — Auto-scaling event occurred
- `drift.detected` — Data or model drift exceeds threshold
- `audit.completed` — NovaGuard audit report is ready

### Webhook Payload

```json
{
  "id": "evt-abc123",
  "type": "training.completed",
  "timestamp": "2025-06-01T14:30:00Z",
  "data": {
    "job_id": "tj-789xyz",
    "model_id": "model-456def",
    "metrics": {
      "accuracy": 0.94,
      "f1_score": 0.91,
      "auc_roc": 0.97
    },
    "duration_seconds": 8100
  }
}
```

### Webhook Security

All webhook payloads include an HMAC-SHA256 signature in the `X-NovaTech-Signature` header. Verify this signature using your webhook secret to ensure authenticity.

## SDK Libraries

Official SDK libraries are available for:

| Language | Package | Version |
|----------|---------|---------|
| Python | `novatech-sdk` | 3.2.1 |
| TypeScript | `@novatech/sdk` | 3.2.0 |
| Go | `github.com/novatech/sdk-go` | v3.1.0 |
| Java | `io.novatech:sdk` | 3.2.1 |

### Python SDK Quick Start

```python
from novatech import NovaTechClient

client = NovaTechClient(
    api_key="your-api-key",
    region="us-east-1"
)

# List models
models = client.models.list(status="deployed")
for model in models:
    print(f"{model.name} - {model.endpoint_url}")

# Get predictions
result = client.predictions.create(
    endpoint="churn-predictor-prod",
    payload={"customer_id": "cust-123", "features": {...}}
)
```

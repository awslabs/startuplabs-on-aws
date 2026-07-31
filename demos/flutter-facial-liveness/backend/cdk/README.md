# Backend — Rekognition Proxy (CDK)

API Gateway (REST + API key + throttling) -> Lambda (Python 3.12, arm64) -> Amazon Rekognition.

The mobile app never carries AWS credentials. The Lambda execution role holds
**only** `rekognition:DetectFaces`, `rekognition:DetectLabels`, and the
Face Liveness actions (`rekognition:CreateFaceLivenessSession`,
`rekognition:GetFaceLivenessSessionResults`).

## Endpoints

- `POST /detect-faces` — body `{ "image": "<base64>" }` -> `{ "FaceDetails": [...] }`
- `POST /detect-labels` — body `{ "image": "<base64>" }` -> `{ "Labels": [...] }`
- `POST /liveness/create-session` -> `{ "sessionId": "..." }`
- `GET  /liveness/session/{id}/result` -> `{ "status": "...", "confidence": <n> }`

All endpoints require the `x-api-key` header.

## Deploy

```bash
npm install
npx cdk bootstrap          # first time per account/region
npm run deploy             # or: npx cdk deploy --all

# optional: restrict CORS to your frontend domain
npx cdk deploy -c allowedOrigin=https://preview.yourdomain.com
```

After deploy, the stack outputs `ApiUrl` and `ApiKeyId`. Retrieve the key value:

```bash
aws apigateway get-api-key --api-key <ApiKeyId> --include-value --query value --output text
```

Use `ApiUrl` and the key value in the Flutter app's `--dart-define` flags.

## Cost

You are responsible for the cost of the AWS services used while running this
sample. There is no additional cost for using the sample itself. For full
details, see the pricing pages for each AWS service used (Amazon Rekognition,
Amazon API Gateway, AWS Lambda, Amazon Cognito). Prices are subject to change.

Cost guardrails baked into this stack:

- Throttling: 10 req/s (burst 20).
- Usage plan with a daily quota of 10,000 requests.
- 5 MB per-image limit in the Lambda.

## Cleanup

```bash
npm run destroy
```

## Security note

`Access-Control-Allow-Origin` in the handler is set to `*` to keep the sample
simple. In production, restrict it to your frontend domain (pass
`-c allowedOrigin=...` at deploy time, which also configures API Gateway CORS).

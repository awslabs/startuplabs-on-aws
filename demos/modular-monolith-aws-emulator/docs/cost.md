# Cost

You are responsible for the cost of the AWS services you deploy with `just rollout`. Run `just destroy` when you are done; it deletes both stacks and the log groups CloudFormation does not own.

Two estimates for us-east-1, generated on 2026-11-12 with the AWS Pricing Calculator. Prices change; open the links for current numbers.

| Scenario | Monthly | AWS Pricing Calculator |
|---|---|---|
| Dev stage: light use, Aurora paused most of the day | **8.10 USD** | [estimate](https://calculator.aws/#/estimate?id=a292e0e9435356ea38e4dcc862b3990bb06f4128) |
| Reference production: 20,000 MAU, 3M API requests/month | **299.54 USD** | [estimate](https://calculator.aws/#/estimate?id=90405d40ef89d43204b1147c939bfd78f329a0f0) |

The inputs are in [pricing/pricing-dev.json](pricing/pricing-dev.json) and [pricing/pricing-prod.json](pricing/pricing-prod.json): one entry per calculator service, with the values entered on its form. Change them to match your load and re-enter them in the calculator.

## Where the money goes

| Service | Dev | Prod | Driver |
|---|---|---|---|
| Amazon Cognito (Essentials) | 0.00 | 150.00 | MAU above the 10,000 included: 0.015 USD each |
| Aurora PostgreSQL Serverless v2 | 5.42 | 103.57 | average ACU x 730 h x 0.12 USD, storage 0.10 USD/GB, I/O 0.20 USD per million |
| AWS Secrets Manager | 0.90 | 15.40 | 1 secret plus API calls; assumes one call per SQL statement through the Data API |
| Amazon CloudWatch | 0.50 | 15.14 | log ingestion, 0.50 USD/GB |
| Amazon S3 | 0.13 | 5.00 | storage and requests |
| AWS Lambda (arm64) | 0.15 | 3.67 | GB-seconds and requests, free tier not applied |
| Amazon API Gateway (HTTP API) | 0.10 | 3.00 | 1.00 USD per million requests |
| Amazon DynamoDB (on-demand) | 0.27 | 2.26 | reads, writes, storage |
| AWS X-Ray | 0.03 | 0.90 | 5% of requests traced |
| AWS Cloud Map | 0.60 | 0.60 | private DNS namespace (hosted zone) |
| Amazon SNS | 0.00 | 0.00 | alarm emails |
| **Total** | **8.10** | **299.54** | |

Not in the estimate: four CloudWatch alarms (0.40 USD), data transfer out (negligible at these volumes).

## Choices that keep the idle cost low

- Aurora Serverless v2 with a 0 ACU minimum and auto-pause after 10 minutes. With a 0.5 ACU minimum the dev stage would cost about 43.80 USD/month for capacity alone. The trade-off is a few seconds of extra latency on the first request after a pause.
- No NAT gateway: Aurora sits in isolated subnets and the API reaches it through the Data API.
- Both Lambda functions on arm64.

At production scale, Cognito is about half of the bill. Check which Essentials features you use before choosing a plan.

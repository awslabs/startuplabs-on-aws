# 0005 Rollout with CDK and a per-service dashboard

Status: accepted, 2026-11-12

## Decision

- CDK in Python (`infra/`), two stacks per environment: `platform` (stateful) and `api` (stateless). Names follow `<project>-<stage>-<component>`.
- The API runs on Lambda behind an HTTP API. The Lambda is not in a VPC: Aurora through the Data API, everything else through public AWS endpoints with IAM. No NAT and no fixed ALB cost.
- Aurora Serverless v2 with a 0 ACU minimum and auto-pause. The adapter retries calls for up to 25 s on `DatabaseResumingException`.
- `just rollout` deploys the same `cdk.out` used by `cdk diff`, so what was reviewed is what goes up.
- The dashboard reads CloudFormation events and groups them by resource type (`infra/tracker.py`). After the deploy, checks run against the real resources (`infra/checks.py`).
- cdk-nag v3 (`Validations.of(...).acknowledge`) runs at synth time. Every accepted finding has a written reason in the code; none is accepted at stack level.
- mypy skips following the CDK packages (`follow_imports = "skip"`): following them costs about 2.4 GB of memory and hung a 1 h CI job.

## Exposure

Public API with a Cognito JWT authorizer in API Gateway on every route except the five a user without an account needs (`register`, `confirm`, `login`, `refresh`, `healthz`). The app validates the JWT again. The rollout fails if a request with no token, or with a forged one, does not get the API Gateway 401.

## Consequences

- The first request after Aurora pauses takes a few extra seconds.
- Pinned versions: `aws-cdk-lib` < 2.276 and CLI `aws-cdk@2.1148.1`, because lib 2.276 needs a CLI that has not been published yet.

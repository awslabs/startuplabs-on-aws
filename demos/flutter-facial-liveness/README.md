# Flutter Facial Liveness on AWS

A Flutter (iOS and Android) demo that proves a **live person** is present using
[Amazon Rekognition Face Liveness](https://docs.aws.amazon.com/rekognition/latest/dg/face-liveness.html),
plus image analysis (face/label detection) — all without ever placing AWS
credentials in the mobile app.

It shows two patterns SAs are frequently asked about:

1. **How to structure a Flutter app + AWS wrapper** so it is testable and never
   leaks AWS credentials.
2. **How to embed a native AWS SDK view** (Amplify's SwiftUI
   `FaceLivenessDetectorView` on iOS, Compose `FaceLivenessDetector` on Android)
   inside Flutter through a type-safe Platform View bridge.

> This is sample code for demonstration and learning. It is **not
> production-ready** and must be reviewed, tested, and hardened for your own
> security and compliance requirements before any production use.

## Architecture

Region: `us-east-1`. Two independent paths from the app — a proxied image
analysis path and a native Face Liveness path.

```mermaid
flowchart TB
    user([User])

    subgraph client["Flutter app (iOS / Android)"]
        dart["Dart<br/>LivenessDetectorWidget"]
        pigeon["Pigeon<br/>type-safe channel"]
        swift["Swift / Kotlin<br/>FaceLivenessPlatformView"]
        amplify["Amplify UI Liveness<br/>SwiftUI / Compose detector"]
        client_wrap["rekognition_client<br/>(pure-Dart proxy wrapper)"]
    end

    subgraph aws["AWS Cloud"]
        cognito["Amazon Cognito<br/>Identity Pool — scoped guest role"]
        apigw["Amazon API Gateway<br/>REST + API key + throttling"]
        lambda["AWS Lambda<br/>proxy / liveness sessions"]
        rekognition["Amazon Rekognition<br/>Face Liveness + DetectFaces/Labels"]
        cw["Amazon CloudWatch<br/>logs & metrics"]
    end

    user -->|"1 - use the app"| dart
    dart -->|"2 - setCredentials"| pigeon
    pigeon -->|"3 - bridge"| swift
    swift -->|"4 - hosts native view"| amplify
    dart -->|"5 - fetch guest creds"| cognito
    amplify -->|"6 - stream video (signed WebSocket)"| rekognition
    dart -.->|"onComplete / onError"| dart

    client_wrap -->|"7 - DetectFaces/Labels (API key)"| apigw
    dart -->|"8 - create session / read result"| apigw
    apigw -->|"9 - invoke"| lambda
    lambda -->|"IAM: least-privilege"| rekognition
    lambda -.-> cw
```

<details>
<summary>Text version</summary>

```
Flutter app (iOS / Android)
  ├── rekognition_client (pure-Dart wrapper)  ──► API Gateway + Lambda (proxy)  ──► Amazon Rekognition
  │                                                (least-privilege IAM role)        DetectFaces / DetectLabels
  │
  └── rekognition_liveness (native plugin)    ──► Cognito Identity (guest creds, scoped)
        Amplify UI Liveness (SwiftUI/Compose)  ──► streams video over signed WebSocket ──► Amazon Rekognition
                                                                                            Face Liveness
```

</details>

Two independent paths:

- **Image analysis** goes through a backend **proxy** so the app carries no AWS
  credentials. The Lambda holds a least-privilege role limited to
  `DetectFaces` / `DetectLabels`.
- **Face Liveness** uses Amplify's native view, which needs short-lived Cognito
  guest credentials scoped to only `rekognition:StartFaceLivenessSession`. The
  authoritative verdict is fetched back through the proxy.

## AWS services used

| Service | Purpose |
|---------|---------|
| Amazon Rekognition | Face/label detection and Face Liveness |
| AWS Lambda | Proxy that calls Rekognition (512 MB, arm64, Python 3.12) |
| Amazon API Gateway | REST endpoint + API key + throttling / daily quota |
| Amazon Cognito | Short-lived, scoped guest credentials for the native liveness view |
| Amazon CloudWatch | Lambda and API Gateway logs |

## Repository structure

| Path | What it is |
|------|------------|
| [`backend/cdk/`](backend/cdk/) | API Gateway + Lambda + least-privilege IAM (AWS CDK) |
| [`packages/rekognition_client/`](packages/rekognition_client/) | Pure-Dart wrapper: interface, HTTP impl, models, tests |
| [`packages/rekognition_liveness/`](packages/rekognition_liveness/) | Flutter plugin embedding the native Amplify liveness view (iOS and Android) |
| [`app_example/`](app_example/) | Flutter app (feature-first + Riverpod) that consumes both |

## How to run

1. **Backend** — deploy the proxy and note the outputs:
   ```bash
   cd backend/cdk
   npm install
   npx cdk deploy
   ```
   Record `ApiUrl` and retrieve the API key value:
   ```bash
   aws apigateway get-api-key --api-key <ApiKeyId> --include-value --query value --output text
   ```

2. **App** — run against those values (never hardcode them; see `.env.example`):
   ```bash
   cd app_example
   flutter pub get
   flutter run \
     --dart-define=REKOGNITION_API_URL=<ApiUrl> \
     --dart-define=REKOGNITION_API_KEY=<key> \
     --dart-define=IDENTITY_POOL_ID=<region>:<uuid> \
     --dart-define=AWS_REGION=us-east-1
   ```

   On Android, `app_example/android` already carries the two settings any host
   app needs for the liveness plugin (core library desugaring and
   `android.uniquePackageNames=false`). See the
   [plugin README](packages/rekognition_liveness/README.md#android) if you embed
   the plugin in another app.

## Security

- The app **never** carries AWS credentials. Only the Lambda talks to
  Rekognition for image analysis, with a least-privilege role.
- The API URL and key are injected at build time via `--dart-define`, never
  committed to source.
- API key + throttling + daily quota on API Gateway limit abuse and cost.
- The native liveness path uses **short-lived Cognito guest credentials**
  scoped to a single Rekognition action.

## Cost

You are responsible for the cost of the AWS services used while running this
sample. There is no additional cost for the sample itself. See the pricing pages
for each service used. Prices are subject to change.

## Platform support

| Platform | Native liveness component | Validation |
|----------|---------------------------|------------|
| iOS 14+ | `amplify-ui-swift-liveness` 1.4.2 | Full challenge on a physical iPhone with plugin 0.2.0; the 0.3.0 changes compile for the simulator but were not re-run on a device |
| Android API 24+ (Android 7.0) | `com.amplifyframework.ui:liveness` 1.11.0 | Emulators only, Android 7.0 (API 24) and Android 16 (API 36), see below |

Android was validated on emulators at both ends of the supported range, Android
7.0 (API 24) and Android 16 (API 36): the detector opens inside the platform
view, the runtime camera permission prompt works (deny and allow), CameraX binds
to the view lifecycle, the camera is released when the app goes to the
background and when the view is disposed, and typed errors reach Dart (for
example `userCancelled`). The emulated camera produces no face, so the
full challenge and the signed WebSocket stream to Rekognition have **not** been
exercised on Android yet. Run it on a physical device before relying on it.

Image analysis (via the proxy) is platform-agnostic.

## License

Apache-2.0. See the repository root [LICENSE](../../LICENSE).

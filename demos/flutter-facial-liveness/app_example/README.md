# rekognition_app_example

Example Flutter app (Android/iOS) that captures an image and analyzes it with
Amazon Rekognition through the `rekognition_client` wrapper, and runs the
Face Liveness flow through the `rekognition_liveness` native plugin (iOS).

## Architecture

Feature-first + lightweight clean architecture, DI with Riverpod:

```
lib/
  core/config.dart                          # config injected via --dart-define
  features/face_analysis/
    domain/face_analysis_repository.dart     # interface (contract)
    data/face_analysis_repository_impl.dart  # adapts the wrapper
    presentation/
      providers.dart                         # DI (Riverpod)
      face_analysis_controller.dart          # StateNotifier + immutable state
      face_analysis_screen.dart              # UI
  features/liveness/                         # Face Liveness flow (iOS native view)
```

The UI depends on `FaceAnalysisRepository` (abstraction), never on the
implementation — so it can be swapped for a fake in tests.

## Run

```bash
flutter pub get
flutter run \
  --dart-define=REKOGNITION_API_URL=https://<id>.execute-api.us-east-1.amazonaws.com/prod/ \
  --dart-define=REKOGNITION_API_KEY=<key> \
  --dart-define=IDENTITY_POOL_ID=<region>:<uuid> \
  --dart-define=AWS_REGION=us-east-1
```

## Build

```bash
# Android
flutter build apk --release --dart-define=REKOGNITION_API_URL=... --dart-define=REKOGNITION_API_KEY=...

# iOS (requires an Apple Developer account + provisioning profile)
flutter build ipa --dart-define=REKOGNITION_API_URL=... --dart-define=REKOGNITION_API_KEY=...
```

> Security: the API key and URL never live in source code — they are injected
> at build time via `--dart-define`. AWS credentials never enter the app; the
> backend is the only component that talks to Rekognition.

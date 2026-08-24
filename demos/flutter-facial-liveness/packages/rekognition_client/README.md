# rekognition_client

A transport-agnostic Dart wrapper for an Amazon Rekognition proxy backend.

It exposes a clean contract (`RekognitionApi`) and hides HTTP, serialization,
and retries. **No AWS credentials live in the app** — the backend
(API Gateway + Lambda) holds the least-privilege role that calls Rekognition.

## Usage

```dart
import 'package:rekognition_client/rekognition_client.dart';

final RekognitionApi api = RekognitionApiImpl(
  baseUrl: Uri.parse('https://<api-id>.execute-api.us-east-1.amazonaws.com/prod/'),
  apiKey: const String.fromEnvironment('REKOGNITION_API_KEY'),
);

final labels = await api.detectLabels(imageBytes);
final faces = await api.detectFaces(imageBytes);
```

`imageBytes` is a `Uint8List` — the JPEG/PNG bytes of the image (ideally already
compressed).

## Why an abstract interface

The presentation layer depends on `RekognitionApi`, never on
`RekognitionApiImpl`. This lets you:

- Swap in a fake for tests (see `test/`).
- Swap the transport (gRPC, Cognito-direct) without touching the UI.

## Tests

```bash
dart pub get
dart test
dart analyze
```

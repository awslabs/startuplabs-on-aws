// Pigeon schema for the rekognition_liveness Flutter<->native bridge.
//
// Run codegen from the package root with:
//   dart run pigeon --input pigeons/messages.dart
//
// The generated files (Messages.g.*) are checked in; do not edit them by hand.
// Because each platform view instance owns its own channel, the generated APIs
// are constructed with a `messageChannelSuffix` (the platform view id) so
// multiple liveness views can coexist without cross-talk.

import 'package:pigeon/pigeon.dart';

@ConfigurePigeon(
  PigeonOptions(
    dartOut: 'lib/src/messages.g.dart',
    dartOptions: DartOptions(),
    swiftOut:
        'ios/rekognition_liveness/Sources/rekognition_liveness/Messages.g.swift',
    swiftOptions: SwiftOptions(),
    kotlinOut:
        'android/src/main/kotlin/dev/aws/jvtsa/rekognition_liveness/Messages.g.kt',
    kotlinOptions: KotlinOptions(
      package: 'dev.aws.jvtsa.rekognition_liveness',
    ),
    dartPackageName: 'rekognition_liveness',
  ),
)

/// Temporary AWS credentials handed to the native liveness SDK, which signs the
/// WebSocket to Amazon Rekognition directly.
class LivenessCredentialsMessage {
  LivenessCredentialsMessage({
    required this.accessKeyId,
    required this.secretAccessKey,
    required this.sessionToken,
  });

  final String accessKeyId;
  final String secretAccessKey;
  final String sessionToken;
}

/// Result of a completed liveness check, surfaced by the native SDK.
class LivenessResultMessage {
  LivenessResultMessage({
    required this.sessionId,
    required this.isLive,
    required this.confidence,
    this.referenceImage,
  });

  final String sessionId;
  final bool isLive;
  final double confidence;
  final Uint8List? referenceImage;
}

/// A native-side failure (SDK error, cancellation, or missing dependency).
class LivenessErrorMessage {
  LivenessErrorMessage({required this.code, required this.message});

  final String code;
  final String message;
}

/// Dart -> native. Called once, after the platform view is created and Cognito
/// credentials have been fetched. Receiving credentials is the signal for the
/// native side to present the FaceLivenessDetectorView.
@HostApi()
abstract class LivenessHostApi {
  void setCredentials(LivenessCredentialsMessage credentials);
}

/// Native -> Dart. Terminal callbacks; exactly one fires per session.
@FlutterApi()
abstract class LivenessFlutterApi {
  void onComplete(LivenessResultMessage result);
  void onError(LivenessErrorMessage error);
}

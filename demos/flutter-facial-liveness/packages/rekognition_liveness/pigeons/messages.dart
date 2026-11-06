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
/// WebSocket to Amazon Rekognition directly. The SDK fetches them once per
/// session and never refreshes, so the real expiration must travel with them.
class LivenessCredentialsMessage {
  LivenessCredentialsMessage({
    required this.accessKeyId,
    required this.secretAccessKey,
    required this.sessionToken,
    required this.expirationEpochSeconds,
  });

  final String accessKeyId;
  final String secretAccessKey;
  final String sessionToken;
  final int expirationEpochSeconds;
}

/// The native SDK finished streaming. It carries no verdict on either
/// platform: the backend reads the result with GetFaceLivenessSessionResults.
class LivenessCompletionMessage {
  LivenessCompletionMessage({required this.sessionId});

  final String sessionId;
}

/// Stable error codes shared by iOS and Android, so Dart can decide between
/// retry, step-up or a user message without comparing strings.
enum LivenessErrorCode {
  unknown,
  sessionNotFound,
  accessDenied,
  cameraPermissionDenied,
  cameraNotAvailable,
  sessionInterrupted,
  sessionTimedOut,
  faceCheckFailed,
  unsupportedChallenge,
  userCancelled,
  videoEncoding,
  serviceError,
  sdkNotLinked,
  // Raised on the Dart side only.
  credentialsUnavailable,
  platformNotSupported,
}

/// A native-side failure (SDK error, cancellation, or missing dependency).
class LivenessErrorMessage {
  LivenessErrorMessage({required this.code, required this.message});

  final LivenessErrorCode code;
  final String message;
}

/// Dart -> native. Called once, after the platform view is created and
/// credentials have been fetched. Receiving credentials is the signal for the
/// native side to present the liveness detector.
@HostApi()
abstract class LivenessHostApi {
  void setCredentials(LivenessCredentialsMessage credentials);
}

/// Dart -> native, app-wide (no channel suffix). The Android SDK does not ask
/// for the camera permission itself, so the app asks before showing the view.
@HostApi()
abstract class LivenessPermissionApi {
  @async
  bool requestCameraPermission();
}

/// Native -> Dart. Terminal callbacks; exactly one fires per session.
@FlutterApi()
abstract class LivenessFlutterApi {
  void onComplete(LivenessCompletionMessage completion);
  void onError(LivenessErrorMessage error);
}

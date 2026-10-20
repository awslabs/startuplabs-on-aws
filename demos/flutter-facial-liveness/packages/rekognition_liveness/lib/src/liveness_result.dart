import 'package:flutter/foundation.dart';

import 'messages.g.dart';

/// The native detector finished streaming the challenge for [sessionId].
///
/// It carries no verdict: neither Amplify UI component returns a score. Ask the
/// backend for the result (GetFaceLivenessSessionResults) and decide there.
@immutable
class LivenessCompletion {
  const LivenessCompletion({required this.sessionId});

  final String sessionId;

  @override
  String toString() => 'LivenessCompletion(sessionId: $sessionId)';
}

/// A verdict computed by the backend from GetFaceLivenessSessionResults.
/// The plugin never produces this; apps build it from their backend response.
@immutable
class LivenessResult {
  const LivenessResult({
    required this.sessionId,
    required this.isLive,
    required this.confidence,
    this.referenceImageBytes,
  });

  final String sessionId;
  final bool isLive;
  final double confidence;
  final List<int>? referenceImageBytes;

  @override
  String toString() =>
      'LivenessResult(sessionId: $sessionId, isLive: $isLive, confidence: $confidence)';
}

@immutable
class LivenessError {
  const LivenessError({required this.code, required this.message});

  final LivenessErrorCode code;
  final String message;

  /// Worth a new session without user action beyond trying again.
  bool get isRetryable => switch (code) {
        LivenessErrorCode.sessionInterrupted ||
        LivenessErrorCode.sessionTimedOut ||
        LivenessErrorCode.faceCheckFailed ||
        LivenessErrorCode.serviceError ||
        LivenessErrorCode.unknown =>
          true,
        _ => false,
      };

  @override
  String toString() => 'LivenessError(code: ${code.name}, message: $message)';
}

/// Camera used by the challenge. [back] only applies to FaceMovementChallenge;
/// FaceMovementAndLightChallenge always uses the front camera.
enum LivenessCamera { front, back }

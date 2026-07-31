import 'package:flutter/foundation.dart';

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

  final String code;
  final String message;

  @override
  String toString() => 'LivenessError(code: $code, message: $message)';
}

import 'package:meta/meta.dart';

import 'bounding_box.dart';

/// A single face detected in an image, with its location and confidence.
@immutable
class DetectedFace {
  const DetectedFace({
    required this.boundingBox,
    required this.confidence,
    this.ageLow,
    this.ageHigh,
  });

  final BoundingBox boundingBox;

  /// Detection confidence, 0–100.
  final double confidence;

  final int? ageLow;
  final int? ageHigh;

  factory DetectedFace.fromJson(Map<String, dynamic> json) {
    final ageRange = json['AgeRange'] as Map<String, dynamic>?;
    return DetectedFace(
      boundingBox:
          BoundingBox.fromJson(json['BoundingBox'] as Map<String, dynamic>),
      confidence: (json['Confidence'] as num).toDouble(),
      ageLow: (ageRange?['Low'] as num?)?.toInt(),
      ageHigh: (ageRange?['High'] as num?)?.toInt(),
    );
  }
}

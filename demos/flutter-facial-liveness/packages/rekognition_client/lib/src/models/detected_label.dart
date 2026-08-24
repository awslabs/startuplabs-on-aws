import 'package:meta/meta.dart';

/// A single label (object, scene or concept) detected in an image.
@immutable
class DetectedLabel {
  const DetectedLabel({
    required this.name,
    required this.confidence,
  });

  final String name;

  /// Detection confidence, 0–100.
  final double confidence;

  factory DetectedLabel.fromJson(Map<String, dynamic> json) {
    return DetectedLabel(
      name: json['Name'] as String,
      confidence: (json['Confidence'] as num).toDouble(),
    );
  }
}

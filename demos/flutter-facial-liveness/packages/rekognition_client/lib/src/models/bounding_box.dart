import 'package:meta/meta.dart';

/// A normalized bounding box, with all values expressed as ratios (0.0–1.0)
/// of the source image dimensions — exactly as Rekognition returns them.
@immutable
class BoundingBox {
  const BoundingBox({
    required this.left,
    required this.top,
    required this.width,
    required this.height,
  });

  final double left;
  final double top;
  final double width;
  final double height;

  factory BoundingBox.fromJson(Map<String, dynamic> json) {
    return BoundingBox(
      left: (json['Left'] as num).toDouble(),
      top: (json['Top'] as num).toDouble(),
      width: (json['Width'] as num).toDouble(),
      height: (json['Height'] as num).toDouble(),
    );
  }

  @override
  bool operator ==(Object other) =>
      other is BoundingBox &&
      other.left == left &&
      other.top == top &&
      other.width == width &&
      other.height == height;

  @override
  int get hashCode => Object.hash(left, top, width, height);
}

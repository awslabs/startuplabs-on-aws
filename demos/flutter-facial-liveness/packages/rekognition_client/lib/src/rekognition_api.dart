import 'dart:typed_data';

import 'models/detected_face.dart';
import 'models/detected_label.dart';

/// Transport-agnostic contract for image analysis backed by Amazon Rekognition.
///
/// The app depends on this interface, never on the concrete implementation.
/// Swap [RekognitionApiImpl] for a fake in tests, or a different transport
/// (gRPC, Cognito-direct) without touching the presentation layer.
abstract interface class RekognitionApi {
  /// Detects objects, scenes and concepts in [image] (raw bytes).
  ///
  /// Throws [RekognitionException] on backend or transport errors.
  Future<List<DetectedLabel>> detectLabels(Uint8List image);

  /// Detects faces in [image] (raw bytes), with location and attributes.
  ///
  /// Throws [RekognitionException] on backend or transport errors.
  Future<List<DetectedFace>> detectFaces(Uint8List image);
}

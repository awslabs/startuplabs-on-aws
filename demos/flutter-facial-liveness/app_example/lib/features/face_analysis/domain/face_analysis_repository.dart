import 'dart:typed_data';

import 'package:rekognition_client/rekognition_client.dart';

/// Domain-facing contract for the face-analysis feature.
///
/// The presentation layer depends on this abstraction, not on the wrapper's
/// [RekognitionApi] directly — keeping the feature independent of the transport
/// package and easy to fake in widget tests.
abstract interface class FaceAnalysisRepository {
  Future<List<DetectedFace>> analyzeFaces(Uint8List image);
  Future<List<DetectedLabel>> analyzeLabels(Uint8List image);
}

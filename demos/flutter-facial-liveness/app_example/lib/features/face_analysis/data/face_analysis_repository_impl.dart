import 'dart:typed_data';

import 'package:rekognition_client/rekognition_client.dart';

import '../domain/face_analysis_repository.dart';

/// Data-layer implementation that delegates to the [RekognitionApi] wrapper.
///
/// This thin adapter is where you would add feature-specific concerns:
/// caching, confidence filtering, telemetry — without leaking them into the UI.
class FaceAnalysisRepositoryImpl implements FaceAnalysisRepository {
  const FaceAnalysisRepositoryImpl(this._api);

  final RekognitionApi _api;

  @override
  Future<List<DetectedFace>> analyzeFaces(Uint8List image) =>
      _api.detectFaces(image);

  @override
  Future<List<DetectedLabel>> analyzeLabels(Uint8List image) =>
      _api.detectLabels(image);
}

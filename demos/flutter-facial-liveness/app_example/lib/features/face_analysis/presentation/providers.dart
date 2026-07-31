import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:rekognition_client/rekognition_client.dart';

import '../../../core/config.dart';
import '../data/face_analysis_repository_impl.dart';
import '../domain/face_analysis_repository.dart';

/// Dependency injection via Riverpod providers.
///
/// Override [rekognitionApiProvider] in tests with a fake to exercise the UI
/// without a network or AWS backend.
final rekognitionApiProvider = Provider<RekognitionApi>((ref) {
  final api = RekognitionApiImpl(
    baseUrl: AppConfig.baseUrl,
    apiKey: AppConfig.apiKey,
  );
  ref.onDispose(api.close);
  return api;
});

final faceAnalysisRepositoryProvider = Provider<FaceAnalysisRepository>((ref) {
  return FaceAnalysisRepositoryImpl(ref.watch(rekognitionApiProvider));
});

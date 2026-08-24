import 'dart:typed_data';

import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:rekognition_client/rekognition_client.dart';

import 'providers.dart';

/// Immutable UI state for the face-analysis screen.
sealed class FaceAnalysisState {
  const FaceAnalysisState();
}

class FaceAnalysisIdle extends FaceAnalysisState {
  const FaceAnalysisIdle();
}

class FaceAnalysisLoading extends FaceAnalysisState {
  const FaceAnalysisLoading();
}

class FaceAnalysisSuccess extends FaceAnalysisState {
  const FaceAnalysisSuccess({required this.faces, required this.labels});
  final List<DetectedFace> faces;
  final List<DetectedLabel> labels;
}

class FaceAnalysisFailure extends FaceAnalysisState {
  const FaceAnalysisFailure(this.message);
  final String message;
}

/// Orchestrates image analysis and exposes immutable state to the UI.
class FaceAnalysisController extends StateNotifier<FaceAnalysisState> {
  FaceAnalysisController(this._ref) : super(const FaceAnalysisIdle());

  final Ref _ref;

  Future<void> analyze(Uint8List image) async {
    state = const FaceAnalysisLoading();
    final repo = _ref.read(faceAnalysisRepositoryProvider);
    try {
      // Run both detections concurrently — they are independent calls.
      final results = await Future.wait([
        repo.analyzeFaces(image),
        repo.analyzeLabels(image),
      ]);
      state = FaceAnalysisSuccess(
        faces: results[0] as List<DetectedFace>,
        labels: results[1] as List<DetectedLabel>,
      );
    } on RekognitionException catch (e) {
      state = FaceAnalysisFailure(e.message);
    } catch (e) {
      state = FaceAnalysisFailure('Unexpected error: $e');
    }
  }

  void reset() => state = const FaceAnalysisIdle();
}

final faceAnalysisControllerProvider =
    StateNotifierProvider<FaceAnalysisController, FaceAnalysisState>((ref) {
  return FaceAnalysisController(ref);
});

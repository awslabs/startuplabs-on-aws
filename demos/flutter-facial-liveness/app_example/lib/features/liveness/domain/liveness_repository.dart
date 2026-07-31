import 'package:rekognition_liveness/rekognition_liveness.dart';

abstract class LivenessRepository {
  Future<String> createSession();
  Future<LivenessResult> getSessionResult(String sessionId);
}

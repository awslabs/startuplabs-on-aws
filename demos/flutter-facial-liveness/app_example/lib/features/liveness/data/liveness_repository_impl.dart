import 'dart:convert';

import 'package:http/http.dart' as http;
import 'package:rekognition_liveness/rekognition_liveness.dart';

import '../../liveness/domain/liveness_repository.dart';
import '../../../core/config.dart';

class LivenessRepositoryImpl implements LivenessRepository {
  LivenessRepositoryImpl({http.Client? client})
      : _client = client ?? http.Client();

  final http.Client _client;

  @override
  Future<String> createSession() async {
    final response = await _client.post(
      AppConfig.baseUrl.resolve('liveness/create-session'),
      headers: {
        'content-type': 'application/json',
        'x-api-key': AppConfig.apiKey,
      },
      body: jsonEncode({'settings': {}}),
    );

    if (response.statusCode != 200) {
      throw Exception('Failed to create liveness session: ${response.body}');
    }

    final body = jsonDecode(response.body) as Map<String, dynamic>;
    return body['sessionId'] as String;
  }

  @override
  Future<LivenessResult> getSessionResult(String sessionId) async {
    final response = await _client.get(
      AppConfig.baseUrl.resolve('liveness/session/$sessionId/result'),
      headers: {'x-api-key': AppConfig.apiKey},
    );

    if (response.statusCode != 200) {
      throw Exception('Failed to get liveness result: ${response.body}');
    }

    final body = jsonDecode(response.body) as Map<String, dynamic>;
    final status = body['status'] as String? ?? '';
    final confidence = (body['confidence'] as num?)?.toDouble() ?? 0.0;

    // Confidence is only meaningful when the session SUCCEEDED
    // (other statuses: CREATED, IN_PROGRESS, FAILED, EXPIRED).
    return LivenessResult(
      sessionId: sessionId,
      isLive: status == 'SUCCEEDED' && confidence >= 90.0,
      confidence: confidence,
    );
  }
}

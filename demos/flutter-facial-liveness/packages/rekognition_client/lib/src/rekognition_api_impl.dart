import 'dart:convert';
import 'dart:typed_data';

import 'package:http/http.dart' as http;

import 'models/detected_face.dart';
import 'models/detected_label.dart';
import 'rekognition_api.dart';
import 'rekognition_exception.dart';

/// HTTP implementation of [RekognitionApi] that talks to the backend proxy
/// (API Gateway + Lambda). No AWS credentials ever live in the app — the
/// backend holds the least-privilege role that calls Rekognition.
class RekognitionApiImpl implements RekognitionApi {
  RekognitionApiImpl({
    required Uri baseUrl,
    required String apiKey,
    http.Client? client,
  })  : _baseUrl = baseUrl,
        _apiKey = apiKey,
        _client = client ?? http.Client();

  final Uri _baseUrl;
  final String _apiKey;
  final http.Client _client;

  @override
  Future<List<DetectedLabel>> detectLabels(Uint8List image) async {
    final json = await _post('detect-labels', image);
    final labels = (json['Labels'] as List<dynamic>? ?? const [])
        .cast<Map<String, dynamic>>();
    return labels.map(DetectedLabel.fromJson).toList(growable: false);
  }

  @override
  Future<List<DetectedFace>> detectFaces(Uint8List image) async {
    final json = await _post('detect-faces', image);
    final faces = (json['FaceDetails'] as List<dynamic>? ?? const [])
        .cast<Map<String, dynamic>>();
    return faces.map(DetectedFace.fromJson).toList(growable: false);
  }

  Future<Map<String, dynamic>> _post(String path, Uint8List image) async {
    final uri = _baseUrl.resolve(path);
    late final http.Response res;
    try {
      res = await _client.post(
        uri,
        headers: {
          'content-type': 'application/json',
          'x-api-key': _apiKey,
        },
        body: jsonEncode({'image': base64Encode(image)}),
      );
    } on Exception catch (e) {
      throw RekognitionException('Transport error: $e');
    }

    if (res.statusCode < 200 || res.statusCode >= 300) {
      throw RekognitionException(
        'Backend returned an error: ${res.body}',
        statusCode: res.statusCode,
      );
    }

    try {
      return jsonDecode(res.body) as Map<String, dynamic>;
    } on FormatException catch (e) {
      throw RekognitionException('Malformed response: $e');
    }
  }

  /// Releases the underlying HTTP client. Call when the app shuts down.
  void close() => _client.close();
}

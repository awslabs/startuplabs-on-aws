import 'dart:convert';
import 'dart:typed_data';

import 'package:http/http.dart' as http;
import 'package:mocktail/mocktail.dart';
import 'package:rekognition_client/rekognition_client.dart';
import 'package:test/test.dart';

class _MockClient extends Mock implements http.Client {}

class _FakeUri extends Fake implements Uri {}

void main() {
  late _MockClient client;
  late RekognitionApiImpl api;
  final image = Uint8List.fromList([1, 2, 3, 4]);

  setUpAll(() {
    registerFallbackValue(_FakeUri());
  });

  setUp(() {
    client = _MockClient();
    api = RekognitionApiImpl(
      baseUrl: Uri.parse('https://api.example.com/'),
      apiKey: 'test-key', // pragma: allowlist secret
      client: client,
    );
  });

  http.Response ok(Map<String, dynamic> body) =>
      http.Response(jsonEncode(body), 200);

  test('detectLabels parses labels from the backend response', () async {
    when(() => client.post(any(),
            headers: any(named: 'headers'), body: any(named: 'body')))
        .thenAnswer((_) async => ok({
              'Labels': [
                {'Name': 'Person', 'Confidence': 99.5},
                {'Name': 'Car', 'Confidence': 80.1},
              ],
            }));

    final labels = await api.detectLabels(image);

    expect(labels, hasLength(2));
    expect(labels.first.name, 'Person');
    expect(labels.first.confidence, closeTo(99.5, 0.001));
  });

  test('detectFaces parses face details and age range', () async {
    when(() => client.post(any(),
            headers: any(named: 'headers'), body: any(named: 'body')))
        .thenAnswer((_) async => ok({
              'FaceDetails': [
                {
                  'BoundingBox': {
                    'Left': 0.1,
                    'Top': 0.2,
                    'Width': 0.3,
                    'Height': 0.4,
                  },
                  'Confidence': 98.0,
                  'AgeRange': {'Low': 20, 'High': 30},
                },
              ],
            }));

    final faces = await api.detectFaces(image);

    expect(faces, hasLength(1));
    expect(faces.first.boundingBox.left, closeTo(0.1, 0.001));
    expect(faces.first.ageLow, 20);
    expect(faces.first.ageHigh, 30);
  });

  test('sends the api key header and base64 body', () async {
    when(() => client.post(any(),
            headers: any(named: 'headers'), body: any(named: 'body')))
        .thenAnswer((_) async => ok({'Labels': []}));

    await api.detectLabels(image);

    final captured = verify(() => client.post(captureAny(),
        headers: captureAny(named: 'headers'),
        body: captureAny(named: 'body'))).captured;

    final uri = captured[0] as Uri;
    final headers = captured[1] as Map<String, String>;
    final body = jsonDecode(captured[2] as String) as Map<String, dynamic>;

    expect(uri.toString(), 'https://api.example.com/detect-labels');
    expect(headers['x-api-key'], 'test-key');
    expect(body['image'], base64Encode(image));
  });

  test('throws RekognitionException on non-2xx status', () async {
    when(() => client.post(any(),
            headers: any(named: 'headers'), body: any(named: 'body')))
        .thenAnswer((_) async => http.Response('{"message":"forbidden"}', 403));

    expect(
      () => api.detectLabels(image),
      throwsA(isA<RekognitionException>()
          .having((e) => e.statusCode, 'statusCode', 403)),
    );
  });

  test('wraps transport errors in RekognitionException', () async {
    when(() => client.post(any(),
            headers: any(named: 'headers'), body: any(named: 'body')))
        .thenThrow(const _SocketLikeException());

    expect(
      () => api.detectFaces(image),
      throwsA(isA<RekognitionException>()),
    );
  });
}

class _SocketLikeException implements Exception {
  const _SocketLikeException();
}

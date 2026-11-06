import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:flutter/widgets.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:rekognition_liveness/rekognition_liveness.dart';

class _FakeCredentials implements LivenessCredentialsProvider {
  _FakeCredentials({this.fail = false});

  final bool fail;

  @override
  Future<LivenessCredentials> fetchCredentials() async {
    if (fail) throw Exception('pool unreachable');
    return LivenessCredentials(
      accessKeyId: 'test-access-key', // pragma: allowlist secret
      secretAccessKey: 'test-secret', // pragma: allowlist secret
      sessionToken: 'test-token', // pragma: allowlist secret
      expiration: DateTime.utc(2030),
    );
  }
}

Widget _host(Widget child) =>
    Directionality(textDirection: TextDirection.ltr, child: child);

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  group('LivenessError.isRetryable', () {
    test('retries transient failures only', () {
      bool retryable(LivenessErrorCode c) =>
          LivenessError(code: c, message: '').isRetryable;

      expect(retryable(LivenessErrorCode.sessionTimedOut), isTrue);
      expect(retryable(LivenessErrorCode.sessionInterrupted), isTrue);
      expect(retryable(LivenessErrorCode.faceCheckFailed), isTrue);
      expect(retryable(LivenessErrorCode.serviceError), isTrue);
      expect(retryable(LivenessErrorCode.cameraPermissionDenied), isFalse);
      expect(retryable(LivenessErrorCode.accessDenied), isFalse);
      expect(retryable(LivenessErrorCode.userCancelled), isFalse);
      expect(retryable(LivenessErrorCode.platformNotSupported), isFalse);
    });
  });

  testWidgets('unsupported platform reports platformNotSupported once',
      (tester) async {
    debugDefaultTargetPlatformOverride = TargetPlatform.linux;
    final errors = <LivenessError>[];

    await tester.pumpWidget(_host(LivenessDetectorWidget(
      sessionId: 's1',
      region: 'sa-east-1',
      credentialsProvider: _FakeCredentials(),
      onComplete: (_) => fail('must not complete'),
      onError: errors.add,
    )));
    await tester.pump();

    expect(errors, hasLength(1));
    expect(errors.single.code, LivenessErrorCode.platformNotSupported);
    debugDefaultTargetPlatformOverride = null;
  });

  group('Android', () {
    late List<MethodCall> viewCalls;
    late List<String> pigeonChannels;

    setUp(() {
      viewCalls = [];
      pigeonChannels = [];
      final messenger =
          TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;
      messenger.setMockMethodCallHandler(SystemChannels.platform_views,
          (call) async {
        viewCalls.add(call);
        // Texture id for the texture-layer platform view.
        return call.method == 'create' ? 0 : null;
      });
    });

    tearDown(() {
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
          .setMockMethodCallHandler(SystemChannels.platform_views, null);
    });

    void mockHostApi(int viewId) {
      final channel =
          'dev.flutter.pigeon.rekognition_liveness.LivenessHostApi.setCredentials.$viewId';
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
          .setMockMessageHandler(channel, (message) async {
        pigeonChannels.add(channel);
        // Pigeon success reply for a void method: [null].
        return const StandardMessageCodec().encodeMessage(<Object?>[null]);
      });
    }

    testWidgets('creates the view with the shared view type and params',
        (tester) async {
      await tester.pumpWidget(_host(LivenessDetectorWidget(
        sessionId: 's1',
        region: 'sa-east-1',
        camera: LivenessCamera.back,
        credentialsProvider: _FakeCredentials(),
        onComplete: (_) {},
        onError: (_) {},
      )));
      await tester.pumpAndSettle();

      final create = viewCalls.firstWhere((c) => c.method == 'create');
      final args = create.arguments as Map;
      expect(args['viewType'], 'dev.aws.jvtsa/face_liveness_view');
      final params = const StandardMessageCodec()
          .decodeMessage(ByteData.sublistView(args['params'] as Uint8List))
          as Map;
      expect(params['sessionId'], 's1');
      expect(params['region'], 'sa-east-1');
      expect(params['camera'], 'back');
    });

    testWidgets('delivers credentials on the per-view channel',
        (tester) async {
      // View ids grow across tests; cover the next few.
      for (var i = 0; i < 16; i++) {
        mockHostApi(i);
      }
      await tester.pumpWidget(_host(LivenessDetectorWidget(
        sessionId: 's1',
        region: 'sa-east-1',
        credentialsProvider: _FakeCredentials(),
        onComplete: (_) {},
        onError: (_) {},
      )));
      await tester.pumpAndSettle();
      final id = (viewCalls.firstWhere((c) => c.method == 'create').arguments
          as Map)['id'] as int;

      expect(pigeonChannels, [
        'dev.flutter.pigeon.rekognition_liveness.LivenessHostApi.setCredentials.$id',
      ]);
    });

    testWidgets('credential failure reports credentialsUnavailable once',
        (tester) async {
      final errors = <LivenessError>[];
      await tester.pumpWidget(_host(LivenessDetectorWidget(
        sessionId: 's1',
        region: 'sa-east-1',
        credentialsProvider: _FakeCredentials(fail: true),
        onComplete: (_) => fail('must not complete'),
        onError: errors.add,
      )));
      await tester.pumpAndSettle();

      expect(errors, hasLength(1));
      expect(errors.single.code, LivenessErrorCode.credentialsUnavailable);
    });
  });
}

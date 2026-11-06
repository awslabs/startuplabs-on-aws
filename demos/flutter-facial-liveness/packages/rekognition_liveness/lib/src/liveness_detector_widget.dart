import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/gestures.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';
import 'package:flutter/widgets.dart';

import 'liveness_result.dart';
import 'messages.g.dart';

typedef LivenessCompleteCallback = void Function(LivenessCompletion completion);
typedef LivenessErrorCallback = void Function(LivenessError error);

/// Must match the view type registered by the iOS and Android plugins.
const String _viewType = 'dev.aws.jvtsa/face_liveness_view';

class LivenessDetectorWidget extends StatefulWidget {
  const LivenessDetectorWidget({
    super.key,
    required this.sessionId,
    required this.region,
    required this.credentialsProvider,
    required this.onComplete,
    required this.onError,
    this.disableStartView = false,
    this.camera = LivenessCamera.front,
    this.androidHybridComposition = false,
  });

  final String sessionId;

  /// Must be the region where the backend created the session.
  final String region;
  final LivenessCredentialsProvider credentialsProvider;

  /// Streaming finished. Fetch the verdict from the backend.
  final LivenessCompleteCallback onComplete;
  final LivenessErrorCallback onError;
  final bool disableStartView;
  final LivenessCamera camera;

  /// Android only. Off by default: the camera preview is a TextureView, which
  /// works in the default texture-layer mode, and Hybrid Composition copies
  /// every frame through main memory on Android 8 and 9. Turn it on only if a
  /// device shows a black or frozen preview.
  final bool androidHybridComposition;

  @override
  State<LivenessDetectorWidget> createState() => _LivenessDetectorWidgetState();
}

class _LivenessDetectorWidgetState extends State<LivenessDetectorWidget> {
  // The Pigeon-generated bridge. `_suffix` is the platform view id, which keys
  // both the host and flutter channels so multiple liveness views never
  // cross-talk (Pigeon's messageChannelSuffix / multi-instance support).
  LivenessHostApi? _hostApi;
  String? _suffix;
  bool _terminal = false;

  bool get _supported =>
      defaultTargetPlatform == TargetPlatform.iOS ||
      defaultTargetPlatform == TargetPlatform.android;

  @override
  void initState() {
    super.initState();
    if (!_supported) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        _handleError(LivenessErrorMessage(
          code: LivenessErrorCode.platformNotSupported,
          message: 'Face Liveness runs on iOS and Android only.',
        ));
      });
    }
  }

  @override
  void dispose() {
    // Tear down the incoming (native -> Dart) handler for this view instance.
    if (_suffix != null) {
      LivenessFlutterApi.setUp(null, messageChannelSuffix: _suffix!);
    }
    super.dispose();
  }

  Map<String, Object?> get _creationParams => <String, Object?>{
        'sessionId': widget.sessionId,
        'region': widget.region,
        'disableStartView': widget.disableStartView,
        'camera': widget.camera.name,
      };

  @override
  Widget build(BuildContext context) {
    switch (defaultTargetPlatform) {
      case TargetPlatform.iOS:
        return UiKitView(
          viewType: _viewType,
          creationParams: _creationParams,
          creationParamsCodec: const StandardMessageCodec(),
          onPlatformViewCreated: _onPlatformViewCreated,
        );
      case TargetPlatform.android:
        return widget.androidHybridComposition
            ? _hybridCompositionView()
            : AndroidView(
                viewType: _viewType,
                layoutDirection: TextDirection.ltr,
                creationParams: _creationParams,
                creationParamsCodec: const StandardMessageCodec(),
                onPlatformViewCreated: _onPlatformViewCreated,
              );
      default:
        return const SizedBox.shrink();
    }
  }

  Widget _hybridCompositionView() {
    return PlatformViewLink(
      viewType: _viewType,
      surfaceFactory: (context, controller) => AndroidViewSurface(
        controller: controller as AndroidViewController,
        gestureRecognizers: const <Factory<OneSequenceGestureRecognizer>>{},
        hitTestBehavior: PlatformViewHitTestBehavior.opaque,
      ),
      onCreatePlatformView: (params) {
        return PlatformViewsService.initExpensiveAndroidView(
          id: params.id,
          viewType: _viewType,
          layoutDirection: TextDirection.ltr,
          creationParams: _creationParams,
          creationParamsCodec: const StandardMessageCodec(),
          onFocus: () => params.onFocusChanged(true),
        )
          ..addOnPlatformViewCreatedListener(params.onPlatformViewCreated)
          ..addOnPlatformViewCreatedListener(_onPlatformViewCreated)
          ..create();
      },
    );
  }

  void _onPlatformViewCreated(int viewId) {
    final suffix = viewId.toString();
    _suffix = suffix;

    // Native -> Dart: register terminal callbacks for this view instance.
    LivenessFlutterApi.setUp(
      _LivenessFlutterApiHandler(
        onCompleteResult: _handleComplete,
        onErrorResult: _handleError,
      ),
      messageChannelSuffix: suffix,
    );

    // Dart -> native: client for the host API on this view instance.
    _hostApi = LivenessHostApi(messageChannelSuffix: suffix);

    _provideCredentials();
  }

  Future<void> _provideCredentials() async {
    final hostApi = _hostApi;
    if (hostApi == null) return;
    try {
      final creds = await widget.credentialsProvider.fetchCredentials();
      if (!mounted) return;
      await hostApi.setCredentials(LivenessCredentialsMessage(
        accessKeyId: creds.accessKeyId,
        secretAccessKey: creds.secretAccessKey,
        sessionToken: creds.sessionToken,
        expirationEpochSeconds:
            creds.expiration.toUtc().millisecondsSinceEpoch ~/ 1000,
      ));
    } catch (e) {
      _handleError(LivenessErrorMessage(
        code: LivenessErrorCode.credentialsUnavailable,
        message: e.toString(),
      ));
    }
  }

  // Exactly one terminal callback reaches the app, whatever the native side does.
  void _handleComplete(LivenessCompletionMessage completion) {
    if (_terminal || !mounted) return;
    _terminal = true;
    widget.onComplete(LivenessCompletion(sessionId: completion.sessionId));
  }

  void _handleError(LivenessErrorMessage error) {
    if (_terminal || !mounted) return;
    _terminal = true;
    widget.onError(LivenessError(code: error.code, message: error.message));
  }
}

/// Adapts the generated [LivenessFlutterApi] interface to plain callbacks so the
/// State can stay in control of widget lifecycle.
class _LivenessFlutterApiHandler implements LivenessFlutterApi {
  _LivenessFlutterApiHandler({
    required this.onCompleteResult,
    required this.onErrorResult,
  });

  final void Function(LivenessCompletionMessage) onCompleteResult;
  final void Function(LivenessErrorMessage) onErrorResult;

  @override
  void onComplete(LivenessCompletionMessage completion) =>
      onCompleteResult(completion);

  @override
  void onError(LivenessErrorMessage error) => onErrorResult(error);
}

@immutable
class LivenessCredentials {
  const LivenessCredentials({
    required this.accessKeyId,
    required this.secretAccessKey,
    required this.sessionToken,
    required this.expiration,
  });

  final String accessKeyId;
  final String secretAccessKey;
  final String sessionToken;

  /// When the temporary credentials expire. The native SDKs read them once per
  /// session and never refresh, so this must be the real expiration.
  final DateTime expiration;
}

abstract class LivenessCredentialsProvider {
  Future<LivenessCredentials> fetchCredentials();
}

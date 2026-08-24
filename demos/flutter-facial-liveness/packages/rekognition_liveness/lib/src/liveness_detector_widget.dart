import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:flutter/widgets.dart';

import 'liveness_result.dart';
import 'messages.g.dart';

typedef LivenessSuccessCallback = void Function(LivenessResult result);
typedef LivenessErrorCallback = void Function(LivenessError error);

class LivenessDetectorWidget extends StatefulWidget {
  const LivenessDetectorWidget({
    super.key,
    required this.sessionId,
    required this.region,
    required this.credentialsProvider,
    required this.onComplete,
    required this.onError,
    this.disableStartView = false,
  });

  final String sessionId;
  final String region;
  final LivenessCredentialsProvider credentialsProvider;
  final LivenessSuccessCallback onComplete;
  final LivenessErrorCallback onError;
  final bool disableStartView;

  @override
  State<LivenessDetectorWidget> createState() => _LivenessDetectorWidgetState();
}

class _LivenessDetectorWidgetState extends State<LivenessDetectorWidget> {
  // The Pigeon-generated bridge. `_suffix` is the platform view id, which keys
  // both the host and flutter channels so multiple liveness views never
  // cross-talk (Pigeon's messageChannelSuffix / multi-instance support).
  LivenessHostApi? _hostApi;
  String? _suffix;

  @override
  void initState() {
    super.initState();
    // Only the iOS platform view is implemented so far. Fail fast with a
    // clear error instead of crashing on an unregistered platform view.
    if (defaultTargetPlatform != TargetPlatform.iOS) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        widget.onError(const LivenessError(
          code: 'PLATFORM_NOT_SUPPORTED',
          message: 'Face Liveness is only implemented on iOS in this PoC. '
              'The Android platform view (Amplify UI Android) is documented '
              'but not yet wired up.',
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

  @override
  Widget build(BuildContext context) {
    if (defaultTargetPlatform != TargetPlatform.iOS) {
      return const SizedBox.shrink();
    }

    return UiKitView(
      viewType: 'dev.aws.jvtsa/face_liveness_view',
      creationParams: <String, dynamic>{
        'sessionId': widget.sessionId,
        'region': widget.region,
        'disableStartView': widget.disableStartView,
      },
      creationParamsCodec: const StandardMessageCodec(),
      onPlatformViewCreated: _onPlatformViewCreated,
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
      await hostApi.setCredentials(LivenessCredentialsMessage(
        accessKeyId: creds.accessKeyId,
        secretAccessKey: creds.secretAccessKey,
        sessionToken: creds.sessionToken,
      ));
    } catch (e) {
      widget.onError(LivenessError(
        code: 'CREDENTIALS_ERROR',
        message: e.toString(),
      ));
    }
  }

  void _handleComplete(LivenessResultMessage result) {
    widget.onComplete(LivenessResult(
      sessionId: result.sessionId,
      isLive: result.isLive,
      confidence: result.confidence,
      referenceImageBytes: result.referenceImage,
    ));
  }

  void _handleError(LivenessErrorMessage error) {
    widget.onError(LivenessError(
      code: error.code,
      message: error.message,
    ));
  }
}

/// Adapts the generated [LivenessFlutterApi] interface to plain callbacks so the
/// State can stay in control of widget lifecycle.
class _LivenessFlutterApiHandler implements LivenessFlutterApi {
  _LivenessFlutterApiHandler({
    required this.onCompleteResult,
    required this.onErrorResult,
  });

  final void Function(LivenessResultMessage) onCompleteResult;
  final void Function(LivenessErrorMessage) onErrorResult;

  @override
  void onComplete(LivenessResultMessage result) => onCompleteResult(result);

  @override
  void onError(LivenessErrorMessage error) => onErrorResult(error);
}

@immutable
class LivenessCredentials {
  const LivenessCredentials({
    required this.accessKeyId,
    required this.secretAccessKey,
    required this.sessionToken,
  });

  final String accessKeyId;
  final String secretAccessKey;
  final String sessionToken;
}

abstract class LivenessCredentialsProvider {
  Future<LivenessCredentials> fetchCredentials();
}

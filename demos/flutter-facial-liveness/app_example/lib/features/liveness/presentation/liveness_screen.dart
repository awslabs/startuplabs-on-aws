import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:rekognition_liveness/rekognition_liveness.dart';

import '../../../core/config.dart';
import 'providers.dart';

class LivenessScreen extends ConsumerStatefulWidget {
  const LivenessScreen({super.key});

  @override
  ConsumerState<LivenessScreen> createState() => _LivenessScreenState();
}

class _LivenessScreenState extends ConsumerState<LivenessScreen> {
  String? _sessionId;
  bool _showDetector = false;
  String _status = 'Tap the button to start a liveness check';
  bool _loading = false;
  LivenessResult? _result;

  Future<void> _startSession() async {
    setState(() => _loading = true);
    // Ask before creating the session: the Android detector fails without it,
    // and on iOS the prompt would interrupt the challenge.
    final granted = await LivenessPermissions.requestCamera();
    if (!granted) {
      setState(() {
        _status = 'Camera permission is required for the liveness check';
        _loading = false;
      });
      return;
    }
    try {
      final repo = ref.read(livenessRepositoryProvider);
      final sessionId = await repo.createSession();
      setState(() {
        _sessionId = sessionId;
        _showDetector = true;
        _loading = false;
      });
    } catch (e) {
      setState(() {
        _status = 'Failed to create session: $e';
        _loading = false;
      });
    }
  }

  Future<void> _fetchResult() async {
    if (_sessionId == null) return;
    setState(() => _loading = true);
    try {
      final repo = ref.read(livenessRepositoryProvider);
      final result = await repo.getSessionResult(_sessionId!);
      setState(() {
        _result = result;
        _status = result.isLive
            ? 'LIVE (${result.confidence.toStringAsFixed(1)}% confidence)'
            : 'NOT LIVE (${result.confidence.toStringAsFixed(1)}% confidence)';
        _loading = false;
      });
    } catch (e) {
      setState(() {
        _status = 'Failed to get result: $e';
        _loading = false;
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    if (_showDetector && _sessionId != null) {
      final credProvider = ref.read(cognitoCredentialsProvider);
      return Scaffold(
        appBar: AppBar(title: const Text('Face Liveness')),
        body: LivenessDetectorWidget(
          sessionId: _sessionId!,
          region: AppConfig.region,
          credentialsProvider: credProvider,
          onComplete: (_) {
            // The native detector returns no verdict; the backend decides.
            setState(() => _showDetector = false);
            _fetchResult();
          },
          onError: (error) {
            setState(() {
              _showDetector = false;
              _status = 'Error (${error.code.name}): ${error.message}';
            });
          },
        ),
      );
    }

    final theme = Theme.of(context);

    return Scaffold(
      appBar: AppBar(title: const Text('Face Liveness')),
      body: Padding(
        padding: const EdgeInsets.all(24),
        child: Center(
          child: Column(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              Icon(
                _result != null && _result!.isLive
                    ? Icons.verified_user
                    : Icons.face,
                size: 80,
                color: _result != null && _result!.isLive
                    ? Colors.green
                    : theme.colorScheme.primary,
              ),
              const SizedBox(height: 24),
              Text(
                _status,
                style: theme.textTheme.titleMedium,
                textAlign: TextAlign.center,
              ),
              const SizedBox(height: 32),
              if (_loading)
                const CircularProgressIndicator()
              else
                FilledButton.icon(
                  onPressed: _startSession,
                  icon: const Icon(Icons.play_arrow),
                  label: const Text('Start Liveness Check'),
                ),
            ],
          ),
        ),
      ),
    );
  }
}

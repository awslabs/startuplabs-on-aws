import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:flutter_image_compress/flutter_image_compress.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:image_picker/image_picker.dart';

import 'face_analysis_controller.dart';

class FaceAnalysisScreen extends ConsumerStatefulWidget {
  const FaceAnalysisScreen({super.key});

  @override
  ConsumerState<FaceAnalysisScreen> createState() => _FaceAnalysisScreenState();
}

class _FaceAnalysisScreenState extends ConsumerState<FaceAnalysisScreen> {
  final _picker = ImagePicker();
  Uint8List? _preview;

  Future<void> _capture(ImageSource source) async {
    final picked = await _picker.pickImage(
      source: source,
      maxWidth: 1280, // downscale client-side: less cost and latency
      imageQuality: 85,
    );
    if (picked == null) return;

    // Compress before upload — Rekognition bills per image, smaller is cheaper.
    final bytes = await picked.readAsBytes();
    final compressed = await FlutterImageCompress.compressWithList(
      bytes,
      quality: 80,
      minWidth: 1024,
      minHeight: 1024,
    );

    setState(() => _preview = compressed);
    await ref.read(faceAnalysisControllerProvider.notifier).analyze(compressed);
  }

  @override
  Widget build(BuildContext context) {
    final state = ref.watch(faceAnalysisControllerProvider);

    return Scaffold(
      appBar: AppBar(title: const Text('Rekognition Demo')),
      body: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            if (_preview != null)
              SizedBox(height: 240, child: Image.memory(_preview!)),
            const SizedBox(height: 16),
            Row(
              children: [
                Expanded(
                  child: FilledButton.icon(
                    onPressed: () => _capture(ImageSource.camera),
                    icon: const Icon(Icons.camera_alt),
                    label: const Text('Camera'),
                  ),
                ),
                const SizedBox(width: 12),
                Expanded(
                  child: OutlinedButton.icon(
                    onPressed: () => _capture(ImageSource.gallery),
                    icon: const Icon(Icons.photo_library),
                    label: const Text('Gallery'),
                  ),
                ),
              ],
            ),
            const SizedBox(height: 24),
            Expanded(child: _Results(state: state)),
          ],
        ),
      ),
    );
  }
}

class _Results extends StatelessWidget {
  const _Results({required this.state});
  final FaceAnalysisState state;

  @override
  Widget build(BuildContext context) {
    return switch (state) {
      FaceAnalysisIdle() =>
        const Center(child: Text('Capture an image to analyze.')),
      FaceAnalysisLoading() =>
        const Center(child: CircularProgressIndicator()),
      FaceAnalysisFailure(:final message) =>
        Center(child: Text('Error: $message')),
      FaceAnalysisSuccess(:final faces, :final labels) => ListView(
          children: [
            Text('Faces: ${faces.length}',
                style: Theme.of(context).textTheme.titleMedium),
            for (final f in faces)
              Text('  confidence ${f.confidence.toStringAsFixed(1)}%'
                  '${f.ageLow != null ? ' · ${f.ageLow}-${f.ageHigh} yrs' : ''}'),
            const Divider(),
            Text('Labels', style: Theme.of(context).textTheme.titleMedium),
            for (final l in labels)
              Text('  ${l.name} — ${l.confidence.toStringAsFixed(1)}%'),
          ],
        ),
    };
  }
}

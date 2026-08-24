/// A transport-agnostic Dart wrapper around an Amazon Rekognition backend.
///
/// Public surface only — everything under `src/` is private. Consumers program
/// against [RekognitionApi] and never see HTTP, JSON or retry logic.
library rekognition_client;

export 'src/models/bounding_box.dart';
export 'src/models/detected_face.dart';
export 'src/models/detected_label.dart';
export 'src/rekognition_api.dart';
export 'src/rekognition_api_impl.dart';
export 'src/rekognition_exception.dart';

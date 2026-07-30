/// Thrown when the Rekognition backend returns an error or is unreachable.
///
/// Transport details (HTTP status, socket errors) are normalized into this
/// single type so consumers never depend on the underlying HTTP client.
class RekognitionException implements Exception {
  const RekognitionException(this.message, {this.statusCode});

  final String message;
  final int? statusCode;

  @override
  String toString() => 'RekognitionException($statusCode): $message';
}

import 'messages.g.dart';

/// Camera permission for the liveness check.
///
/// Call [requestCamera] before showing [LivenessDetectorWidget], after your own
/// rationale screen. On Android the detector fails with
/// `cameraPermissionDenied` when the permission is missing; on iOS the system
/// prompt would otherwise appear in the middle of the challenge.
abstract final class LivenessPermissions {
  static final LivenessPermissionApi _api = LivenessPermissionApi();

  /// Returns true when the camera permission is granted (asking if needed).
  static Future<bool> requestCamera() => _api.requestCameraPermission();
}

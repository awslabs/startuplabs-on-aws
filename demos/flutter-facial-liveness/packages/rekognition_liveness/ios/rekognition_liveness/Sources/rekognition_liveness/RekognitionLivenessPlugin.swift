import AVFoundation
import Flutter
import UIKit

public class RekognitionLivenessPlugin: NSObject, FlutterPlugin {
    public static func register(with registrar: FlutterPluginRegistrar) {
        let factory = FaceLivenessViewFactory(messenger: registrar.messenger())
        registrar.register(factory, withId: "dev.aws.jvtsa/face_liveness_view")
        LivenessPermissionApiSetup.setUp(
            binaryMessenger: registrar.messenger(),
            api: CameraPermissionHandler()
        )
    }
}

/// Asks for the camera before the challenge, so the system prompt never
/// appears in the middle of it.
final class CameraPermissionHandler: LivenessPermissionApi {
    func requestCameraPermission(completion: @escaping (Result<Bool, Error>) -> Void) {
        switch AVCaptureDevice.authorizationStatus(for: .video) {
        case .authorized:
            completion(.success(true))
        case .notDetermined:
            AVCaptureDevice.requestAccess(for: .video) { granted in
                DispatchQueue.main.async { completion(.success(granted)) }
            }
        default:
            completion(.success(false))
        }
    }
}

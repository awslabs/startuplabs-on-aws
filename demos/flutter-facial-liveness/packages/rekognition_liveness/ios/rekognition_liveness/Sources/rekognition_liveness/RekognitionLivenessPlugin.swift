import Flutter
import UIKit

public class RekognitionLivenessPlugin: NSObject, FlutterPlugin {
    public static func register(with registrar: FlutterPluginRegistrar) {
        let factory = FaceLivenessViewFactory(messenger: registrar.messenger())
        registrar.register(factory, withId: "dev.aws.jvtsa/face_liveness_view")
    }
}

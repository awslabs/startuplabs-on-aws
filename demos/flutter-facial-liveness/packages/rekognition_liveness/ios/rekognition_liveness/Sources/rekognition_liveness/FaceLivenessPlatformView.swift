import Flutter
import UIKit
import SwiftUI

// The Amplify liveness SDK is Swift Package Manager only. When the plugin is
// consumed via SPM (Package.swift), `FaceLiveness` imports and the full
// implementation compiles. When consumed via CocoaPods without the app adding
// the Amplify SPM package to its Runner target, the module is absent, so the
// view degrades to a clear runtime error instead of failing to compile.
//
// The Pigeon-generated bridge (Messages.g.swift) is compiled in both cases; the
// channel contract (LivenessHostApi / LivenessFlutterApi) is identical either
// way. Each platform view keys its APIs by the view id (messageChannelSuffix)
// so multiple liveness views never cross-talk.
#if canImport(FaceLiveness) && canImport(AWSPluginsCore)
import FaceLiveness
import AWSPluginsCore

class FaceLivenessPlatformView: NSObject, FlutterPlatformView, LivenessHostApi {
    private let containerView: UIView
    private let messenger: FlutterBinaryMessenger
    private let suffix: String
    private let flutterApi: LivenessFlutterApi
    private let sessionId: String
    private let region: String
    private let disableStartView: Bool
    private let camera: LivenessCamera
    private var hostingController: UIHostingController<AnyView>?
    private var presented = false
    private var terminal = false

    init(frame: CGRect, viewId: Int64, messenger: FlutterBinaryMessenger, args: [String: Any]) {
        self.containerView = UIView(frame: frame)
        self.messenger = messenger
        self.sessionId = args["sessionId"] as? String ?? ""
        self.region = args["region"] as? String ?? ""
        self.disableStartView = args["disableStartView"] as? Bool ?? false
        self.camera = (args["camera"] as? String) == "back" ? .back : .front

        self.suffix = String(viewId)
        self.flutterApi = LivenessFlutterApi(
            binaryMessenger: messenger,
            messageChannelSuffix: suffix
        )

        super.init()

        // Dart -> native: receive credentials on this view instance's channel.
        LivenessHostApiSetup.setUp(
            binaryMessenger: messenger,
            api: self,
            messageChannelSuffix: suffix
        )
    }

    deinit {
        LivenessHostApiSetup.setUp(binaryMessenger: messenger, api: nil, messageChannelSuffix: suffix)
    }

    func view() -> UIView {
        return containerView
    }

    // MARK: - LivenessHostApi

    func setCredentials(credentials: LivenessCredentialsMessage) throws {
        // The detector is presented once per view; later calls are ignored.
        guard !presented else { return }
        presented = true
        presentLivenessView(provider: FlutterLivenessCredentialsProvider(message: credentials))
    }

    private func presentLivenessView(provider: FlutterLivenessCredentialsProvider) {
        let livenessView = FaceLivenessDetectorView(
            sessionID: sessionId,
            credentialsProvider: provider,
            region: region,
            disableStartView: disableStartView,
            // Back camera only applies to FaceMovementChallenge; the backend
            // picks the challenge type when it creates the session.
            challengeOptions: ChallengeOptions(
                faceMovementChallengeOption: FaceMovementChallengeOption(camera: camera)
            ),
            isPresented: .constant(true),
            onCompletion: { [weak self] result in
                self?.handleLivenessCompletion(result)
            }
        )

        let hosting = UIHostingController(rootView: AnyView(livenessView))
        hosting.view.frame = containerView.bounds
        hosting.view.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        containerView.addSubview(hosting.view)
        hostingController = hosting
    }

    // Exactly one terminal callback per session (the SDK fired onCompletion
    // twice before 1.4.3).
    private func handleLivenessCompletion(_ result: Result<Void, FaceLivenessDetectionError>) {
        guard !terminal else { return }
        terminal = true
        switch result {
        case .success:
            flutterApi.onComplete(
                completion: LivenessCompletionMessage(sessionId: sessionId)
            ) { _ in }
        case .failure(let error):
            flutterApi.onError(
                error: LivenessErrorMessage(code: error.livenessErrorCode, message: error.message)
            ) { _ in }
        }
    }
}

extension FaceLivenessDetectionError {
    /// Maps the SDK errors to the codes shared with Android.
    var livenessErrorCode: LivenessErrorCode {
        switch self {
        case .sessionNotFound: return .sessionNotFound
        case .accessDenied, .invalidSignature: return .accessDenied
        case .cameraPermissionDenied: return .cameraPermissionDenied
        case .cameraNotAvailable: return .cameraNotAvailable
        case .socketClosed: return .sessionInterrupted
        case .sessionTimedOut, .faceInOvalMatchExceededTimeLimitError: return .sessionTimedOut
        case .countdownFaceTooClose, .countdownMultipleFaces, .countdownNoFace: return .faceCheckFailed
        case .userCancelled: return .userCancelled
        case .invalidRegion, .validation, .internalServer, .throttling,
             .serviceQuotaExceeded, .serviceUnavailable:
            return .serviceError
        default: return .unknown
        }
    }
}

#else

// Fallback: the Amplify liveness SDK is not linked (CocoaPods consumer that has
// not added the Amplify Swift Package to the Runner target). The platform view
// still instantiates so `pod install` and the build succeed, but any attempt to
// run a check reports a clear, actionable error over the same Pigeon contract.
class FaceLivenessPlatformView: NSObject, FlutterPlatformView {
    private let containerView: UIView
    private let flutterApi: LivenessFlutterApi

    init(frame: CGRect, viewId: Int64, messenger: FlutterBinaryMessenger, args: [String: Any]) {
        self.containerView = UIView(frame: frame)
        self.flutterApi = LivenessFlutterApi(
            binaryMessenger: messenger,
            messageChannelSuffix: String(viewId)
        )
        super.init()
        self.flutterApi.onError(
            error: LivenessErrorMessage(
                code: .sdkNotLinked,
                message: "The Amplify Face Liveness SDK is not linked. Add the "
                    + "amplify-ui-swift-liveness Swift Package to the Runner target, "
                    + "or consume this plugin via Swift Package Manager."
            )
        ) { _ in }
    }

    func view() -> UIView {
        return containerView
    }
}

#endif

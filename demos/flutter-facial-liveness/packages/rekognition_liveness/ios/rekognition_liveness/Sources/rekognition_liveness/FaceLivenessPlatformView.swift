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
    private let flutterApi: LivenessFlutterApi
    private let sessionId: String
    private let region: String
    private let disableStartView: Bool
    private var hostingController: UIHostingController<AnyView>?
    private var credentialsProvider: FlutterLivenessCredentialsProvider?

    init(frame: CGRect, viewId: Int64, messenger: FlutterBinaryMessenger, args: [String: Any]) {
        self.containerView = UIView(frame: frame)
        self.sessionId = args["sessionId"] as? String ?? ""
        self.region = args["region"] as? String ?? "us-east-1"
        self.disableStartView = args["disableStartView"] as? Bool ?? false

        let suffix = String(viewId)
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

    func view() -> UIView {
        return containerView
    }

    // MARK: - LivenessHostApi

    func setCredentials(credentials: LivenessCredentialsMessage) throws {
        credentialsProvider = FlutterLivenessCredentialsProvider(
            accessKeyId: credentials.accessKeyId,
            secretAccessKey: credentials.secretAccessKey,
            sessionToken: credentials.sessionToken
        )
        presentLivenessView()
    }

    private func presentLivenessView() {
        guard let provider = credentialsProvider else { return }

        let livenessView = FaceLivenessDetectorView(
            sessionID: sessionId,
            credentialsProvider: provider,
            region: region,
            disableStartView: disableStartView,
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

    private func handleLivenessCompletion(_ result: Result<Void, FaceLivenessDetectionError>) {
        switch result {
        case .success:
            flutterApi.onComplete(
                result: LivenessResultMessage(
                    sessionId: sessionId,
                    isLive: true,
                    confidence: 0.0
                )
            ) { _ in }
        case .failure(let error):
            flutterApi.onError(
                error: LivenessErrorMessage(
                    code: "LIVENESS_FAILED",
                    message: error.localizedDescription
                )
            ) { _ in }
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
                code: "AMPLIFY_NOT_LINKED",
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

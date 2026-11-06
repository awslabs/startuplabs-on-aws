import Foundation

// Depends on the Amplify SPM-only module; only compiled when it is linked.
#if canImport(AWSPluginsCore)
import AWSPluginsCore

struct FlutterLivenessCredentials: AWSTemporaryCredentials {
    let accessKeyId: String
    let secretAccessKey: String
    let sessionToken: String
    let expiration: Date
}

/// Hands the credentials fetched in Dart to the SDK. The SDK reads them once
/// per session and never refreshes, so the expiration is the real one.
struct FlutterLivenessCredentialsProvider: AWSCredentialsProvider {
    let credentials: FlutterLivenessCredentials

    init(message: LivenessCredentialsMessage) {
        credentials = FlutterLivenessCredentials(
            accessKeyId: message.accessKeyId,
            secretAccessKey: message.secretAccessKey,
            sessionToken: message.sessionToken,
            expiration: Date(timeIntervalSince1970: TimeInterval(message.expirationEpochSeconds))
        )
    }

    func fetchAWSCredentials() async throws -> AWSCredentials {
        credentials
    }
}

#endif

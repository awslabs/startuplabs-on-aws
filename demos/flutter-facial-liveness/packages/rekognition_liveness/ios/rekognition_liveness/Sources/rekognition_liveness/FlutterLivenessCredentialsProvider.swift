import Foundation

// Depends on the Amplify SPM-only module; only compiled when it is linked.
#if canImport(AWSPluginsCore)
import AWSPluginsCore

struct FlutterLivenessCredentials: AWSTemporaryCredentials {
    let accessKeyId: String
    let secretAccessKey: String
    let sessionToken: String
    let expiration: Date

    init(accessKeyId: String, secretAccessKey: String, sessionToken: String) {
        self.accessKeyId = accessKeyId
        self.secretAccessKey = secretAccessKey
        self.sessionToken = sessionToken
        self.expiration = Date().addingTimeInterval(3600)
    }
}

struct FlutterLivenessCredentialsProvider: AWSCredentialsProvider {
    let accessKeyId: String
    let secretAccessKey: String
    let sessionToken: String

    func fetchAWSCredentials() async throws -> AWSCredentials {
        FlutterLivenessCredentials(
            accessKeyId: accessKeyId,
            secretAccessKey: secretAccessKey,
            sessionToken: sessionToken
        )
    }
}

#endif

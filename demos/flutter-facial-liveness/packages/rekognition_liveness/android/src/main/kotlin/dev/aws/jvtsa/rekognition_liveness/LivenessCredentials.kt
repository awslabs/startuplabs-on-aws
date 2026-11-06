package dev.aws.jvtsa.rekognition_liveness

import com.amplifyframework.auth.AWSCredentials
import com.amplifyframework.auth.AWSCredentialsProvider
import com.amplifyframework.auth.AuthException
import com.amplifyframework.core.Consumer

/**
 * Hands the temporary credentials fetched in Dart to the Amplify detector.
 * The detector calls this once per session and never refreshes, and it only
 * accepts temporary credentials, so the real expiration is required.
 */
internal class FlutterTemporaryCredentialsProvider(
    private val message: LivenessCredentialsMessage,
) : AWSCredentialsProvider<AWSCredentials> {

    override fun fetchAWSCredentials(
        onSuccess: Consumer<AWSCredentials>,
        onError: Consumer<AuthException>,
    ) {
        val credentials = AWSCredentials.createAWSCredentials(
            message.accessKeyId,
            message.secretAccessKey,
            message.sessionToken,
            message.expirationEpochSeconds,
        )
        if (credentials != null) {
            onSuccess.accept(credentials)
        } else {
            onError.accept(
                AuthException(
                    "Temporary credentials are incomplete.",
                    "Fetch credentials from the Cognito Identity Pool before starting the check.",
                ),
            )
        }
    }
}

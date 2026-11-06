package dev.aws.jvtsa.rekognition_liveness

import com.amplifyframework.ui.liveness.model.FaceLivenessDetectionException

/**
 * Maps the Amplify detector exceptions (liveness 1.11.0) to the codes shared
 * with iOS. SessionInterruptedException exists only on the unreleased main
 * branch; add it here when the pinned version ships it.
 */
internal fun FaceLivenessDetectionException.toErrorCode(): LivenessErrorCode = when (this) {
    is FaceLivenessDetectionException.SessionNotFoundException -> LivenessErrorCode.SESSION_NOT_FOUND
    is FaceLivenessDetectionException.AccessDeniedException -> LivenessErrorCode.ACCESS_DENIED
    is FaceLivenessDetectionException.CameraPermissionDeniedException ->
        LivenessErrorCode.CAMERA_PERMISSION_DENIED
    is FaceLivenessDetectionException.SessionTimedOutException -> LivenessErrorCode.SESSION_TIMED_OUT
    is FaceLivenessDetectionException.UnsupportedChallengeTypeException ->
        LivenessErrorCode.UNSUPPORTED_CHALLENGE
    is FaceLivenessDetectionException.UserCancelledException -> LivenessErrorCode.USER_CANCELLED
    is FaceLivenessDetectionException.VideoEncodingException,
    is FaceLivenessDetectionException.VideoMuxingException,
    -> LivenessErrorCode.VIDEO_ENCODING
    else -> LivenessErrorCode.UNKNOWN
}

internal fun FaceLivenessDetectionException.toMessage(): LivenessErrorMessage {
    val code = toErrorCode()
    return LivenessErrorMessage(code = code, message = message)
}

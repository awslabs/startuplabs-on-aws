package dev.aws.jvtsa.rekognition_liveness

import com.amplifyframework.ui.liveness.model.FaceLivenessDetectionException
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertTrue

/*
 * JVM tests for the Android bridge logic that has no Android dependency.
 * Run from packages/rekognition_liveness/example/android with:
 *   ./gradlew :rekognition_liveness:testDebugUnitTest
 */
internal class LivenessBridgeTest {

    @Test
    fun session_presentsOnlyOnce() {
        val session = LivenessViewSession()
        assertTrue(session.present())
        assertFalse(session.present())
        assertEquals(LivenessViewSession.State.PRESENTING, session.state)
    }

    @Test
    fun session_terminalEventFiresOnce() {
        val session = LivenessViewSession()
        session.present()
        assertTrue(session.finish())
        assertFalse(session.finish())
        assertEquals(LivenessViewSession.State.FINISHED, session.state)
    }

    @Test
    fun session_noTerminalEventBeforeCredentials() {
        val session = LivenessViewSession()
        assertFalse(session.finish())
        assertEquals(LivenessViewSession.State.AWAITING_CREDENTIALS, session.state)
    }

    @Test
    fun session_ignoresEverythingAfterDispose() {
        val session = LivenessViewSession()
        session.dispose()
        assertFalse(session.present())
        assertFalse(session.finish())
        assertEquals(LivenessViewSession.State.DISPOSED, session.state)
    }

    @Test
    fun params_readCreationArgs() {
        val params = LivenessViewParams.from(
            mapOf(
                "sessionId" to "s1",
                "region" to "sa-east-1",
                "disableStartView" to true,
                "camera" to "back",
            ),
        )
        assertEquals(LivenessViewParams("s1", "sa-east-1", true, true), params)
    }

    @Test
    fun params_defaultToFrontCameraAndStartView() {
        val params = LivenessViewParams.from(emptyMap())
        assertEquals(LivenessViewParams("", "", false, false), params)
    }

    @Test
    fun errors_mapToSharedCodes() {
        val cases = mapOf(
            FaceLivenessDetectionException.SessionNotFoundException() to LivenessErrorCode.SESSION_NOT_FOUND,
            FaceLivenessDetectionException.AccessDeniedException() to LivenessErrorCode.ACCESS_DENIED,
            FaceLivenessDetectionException.CameraPermissionDeniedException() to
                LivenessErrorCode.CAMERA_PERMISSION_DENIED,
            FaceLivenessDetectionException.SessionTimedOutException() to LivenessErrorCode.SESSION_TIMED_OUT,
            FaceLivenessDetectionException.UnsupportedChallengeTypeException() to
                LivenessErrorCode.UNSUPPORTED_CHALLENGE,
            FaceLivenessDetectionException.UserCancelledException() to LivenessErrorCode.USER_CANCELLED,
            FaceLivenessDetectionException.VideoEncodingException() to LivenessErrorCode.VIDEO_ENCODING,
            FaceLivenessDetectionException.VideoMuxingException() to LivenessErrorCode.VIDEO_ENCODING,
            FaceLivenessDetectionException("boom") to LivenessErrorCode.UNKNOWN,
        )
        cases.forEach { (exception, code) -> assertEquals(code, exception.toErrorCode(), exception.message) }
    }

    @Test
    fun errors_keepSdkMessage() {
        val message = FaceLivenessDetectionException.SessionNotFoundException().toMessage()
        assertEquals(LivenessErrorCode.SESSION_NOT_FOUND, message.code)
        assertEquals("Session not found.", message.message)
    }
}

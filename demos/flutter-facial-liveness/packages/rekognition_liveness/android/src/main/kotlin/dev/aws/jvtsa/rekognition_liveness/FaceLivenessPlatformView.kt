package dev.aws.jvtsa.rekognition_liveness

import android.content.Context
import android.os.Handler
import android.os.Looper
import android.view.View
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.Recomposer
import androidx.compose.ui.platform.AndroidUiDispatcher
import androidx.compose.ui.platform.ComposeView
import androidx.compose.ui.platform.ViewCompositionStrategy
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.setViewTreeLifecycleOwner
import androidx.lifecycle.setViewTreeViewModelStoreOwner
import androidx.savedstate.setViewTreeSavedStateRegistryOwner
import com.amplifyframework.auth.AWSCredentials
import com.amplifyframework.auth.AWSCredentialsProvider
import com.amplifyframework.ui.liveness.model.FaceLivenessDetectionException
import com.amplifyframework.ui.liveness.ui.Camera
import com.amplifyframework.ui.liveness.ui.ChallengeOptions
import com.amplifyframework.ui.liveness.ui.FaceLivenessDetector
import com.amplifyframework.ui.liveness.ui.LivenessChallenge
import com.amplifyframework.ui.liveness.ui.LivenessColorScheme
import io.flutter.plugin.common.BinaryMessenger
import io.flutter.plugin.platform.PlatformView
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch

/**
 * One liveness check, hosted as a Flutter platform view.
 *
 * The Amplify detector is a Compose component, so the view is a ComposeView
 * carrying its own ViewTree owners (see [PlatformViewOwner]) and its own
 * Recomposer. Without the Recomposer, Compose installs a window recomposer on
 * the window root (the FlutterView) and looks for a ViewTreeLifecycleOwner
 * there, which FlutterActivity never sets: the app crashes on first measure.
 * The detector is only composed after Dart delivers credentials, mirroring the
 * iOS bridge.
 */
internal class FaceLivenessPlatformView(
    context: Context,
    viewId: Int,
    private val messenger: BinaryMessenger,
    private val params: LivenessViewParams,
    hostLifecycle: Lifecycle?,
) : PlatformView, LivenessHostApi {

    private val suffix = viewId.toString()
    private val owner = PlatformViewOwner(hostLifecycle)
    private val flutterApi = LivenessFlutterApi(messenger, suffix)
    private val mainHandler = Handler(Looper.getMainLooper())
    private val session = LivenessViewSession()

    // AndroidUiDispatcher.CurrentThread carries the main-thread frame clock.
    private val recomposeScope = CoroutineScope(AndroidUiDispatcher.CurrentThread)
    private val recomposer = Recomposer(recomposeScope.coroutineContext)

    private val composeView = ComposeView(context).apply {
        setViewTreeLifecycleOwner(owner)
        setViewTreeSavedStateRegistryOwner(owner)
        setViewTreeViewModelStoreOwner(owner)
        // Disposing the composition runs the detector's onDispose, which unbinds
        // CameraX and restores orientation and screen brightness.
        setViewCompositionStrategy(ViewCompositionStrategy.DisposeOnLifecycleDestroyed(owner))
        setParentCompositionContext(recomposer)
    }

    init {
        recomposeScope.launch { recomposer.runRecomposeAndApplyChanges() }
        LivenessHostApi.setUp(messenger, this, suffix)
    }

    override fun getView(): View = composeView

    override fun setCredentials(credentials: LivenessCredentialsMessage) {
        if (!session.present()) return
        val provider = FlutterTemporaryCredentialsProvider(credentials)
        composeView.setContent {
            LivenessContent(
                params = params,
                credentialsProvider = provider,
                onComplete = ::onDetectorComplete,
                onError = ::onDetectorError,
            )
        }
    }

    private fun onDetectorComplete() {
        if (!session.finish()) return
        // Pigeon Flutter APIs must be called on the platform main thread.
        mainHandler.post {
            flutterApi.onComplete(LivenessCompletionMessage(sessionId = params.sessionId)) {}
        }
    }

    private fun onDetectorError(error: FaceLivenessDetectionException) {
        if (!session.finish()) return
        mainHandler.post { flutterApi.onError(error.toMessage()) {} }
    }

    override fun dispose() {
        session.dispose()
        LivenessHostApi.setUp(messenger, null, suffix)
        // DESTROYED disposes the composition first (camera unbound), then the
        // recomposer that drove it is shut down.
        owner.destroy()
        recomposer.cancel()
        recomposeScope.cancel()
    }
}

@Composable
private fun LivenessContent(
    params: LivenessViewParams,
    credentialsProvider: AWSCredentialsProvider<AWSCredentials>,
    onComplete: () -> Unit,
    onError: (FaceLivenessDetectionException) -> Unit,
) {
    // Back camera only applies to FaceMovementChallenge; the backend picks the
    // challenge type when it creates the session.
    val camera = if (params.backCamera) Camera.Back else Camera.Front
    MaterialTheme(colorScheme = LivenessColorScheme.default()) {
        FaceLivenessDetector(
            sessionId = params.sessionId,
            region = params.region,
            credentialsProvider = credentialsProvider,
            disableStartView = params.disableStartView,
            challengeOptions = ChallengeOptions(faceMovement = LivenessChallenge.FaceMovement(camera)),
            onComplete = { onComplete() },
            onError = { error -> onError(error) },
        )
    }
}

/**
 * State of one view: credentials gate the detector, and exactly one terminal
 * callback reaches Dart. Kept free of Android types so it runs in JVM tests.
 */
internal class LivenessViewSession {
    enum class State { AWAITING_CREDENTIALS, PRESENTING, FINISHED, DISPOSED }

    var state: State = State.AWAITING_CREDENTIALS
        private set

    /** True only for the first credentials delivery on a live view. */
    @Synchronized
    fun present(): Boolean {
        if (state != State.AWAITING_CREDENTIALS) return false
        state = State.PRESENTING
        return true
    }

    /** True only for the first terminal event while presenting. */
    @Synchronized
    fun finish(): Boolean {
        if (state != State.PRESENTING) return false
        state = State.FINISHED
        return true
    }

    @Synchronized
    fun dispose() {
        state = State.DISPOSED
    }
}

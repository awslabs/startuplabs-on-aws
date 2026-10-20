package dev.aws.jvtsa.rekognition_liveness

import android.content.Context
import androidx.lifecycle.Lifecycle
import io.flutter.plugin.common.BinaryMessenger
import io.flutter.plugin.common.StandardMessageCodec
import io.flutter.plugin.platform.PlatformView
import io.flutter.plugin.platform.PlatformViewFactory

/** Creates one isolated liveness view per Flutter platform view id. */
internal class FaceLivenessViewFactory(
    private val messenger: BinaryMessenger,
    private val hostLifecycle: () -> Lifecycle?,
) : PlatformViewFactory(StandardMessageCodec.INSTANCE) {

    override fun create(context: Context, viewId: Int, args: Any?): PlatformView {
        @Suppress("UNCHECKED_CAST")
        val params = args as? Map<String, Any?> ?: emptyMap()
        return FaceLivenessPlatformView(
            context = context,
            viewId = viewId,
            messenger = messenger,
            params = LivenessViewParams.from(params),
            hostLifecycle = hostLifecycle(),
        )
    }
}

/** Creation params sent by the Dart widget. */
internal data class LivenessViewParams(
    val sessionId: String,
    val region: String,
    val disableStartView: Boolean,
    val backCamera: Boolean,
) {
    companion object {
        fun from(args: Map<String, Any?>) = LivenessViewParams(
            sessionId = args["sessionId"] as? String ?: "",
            region = args["region"] as? String ?: "",
            disableStartView = args["disableStartView"] as? Boolean ?: false,
            backCamera = args["camera"] == "back",
        )
    }
}

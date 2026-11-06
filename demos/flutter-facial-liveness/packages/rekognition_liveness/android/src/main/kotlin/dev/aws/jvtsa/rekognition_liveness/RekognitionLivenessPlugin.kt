package dev.aws.jvtsa.rekognition_liveness

import androidx.lifecycle.Lifecycle
import io.flutter.embedding.engine.plugins.FlutterPlugin
import io.flutter.embedding.engine.plugins.activity.ActivityAware
import io.flutter.embedding.engine.plugins.activity.ActivityPluginBinding
import io.flutter.embedding.engine.plugins.lifecycle.FlutterLifecycleAdapter

/**
 * Registers the Face Liveness platform view and the camera permission API.
 *
 * Each platform view owns its own Pigeon channels (suffixed by the view id), so
 * the plugin keeps no per-session state. It only tracks the host Activity, whose
 * lifecycle the views mirror and which the permission request needs.
 */
class RekognitionLivenessPlugin : FlutterPlugin, ActivityAware {
    private var hostLifecycle: Lifecycle? = null
    private val permissions = CameraPermissionHandler()

    override fun onAttachedToEngine(binding: FlutterPlugin.FlutterPluginBinding) {
        binding.platformViewRegistry.registerViewFactory(
            VIEW_TYPE,
            FaceLivenessViewFactory(binding.binaryMessenger) { hostLifecycle },
        )
        LivenessPermissionApi.setUp(binding.binaryMessenger, permissions)
    }

    override fun onDetachedFromEngine(binding: FlutterPlugin.FlutterPluginBinding) {
        LivenessPermissionApi.setUp(binding.binaryMessenger, null)
    }

    override fun onAttachedToActivity(binding: ActivityPluginBinding) {
        // Lifecycle of the host Activity or Fragment (add-to-app).
        hostLifecycle = FlutterLifecycleAdapter.getActivityLifecycle(binding)
        permissions.attach(binding)
    }

    override fun onReattachedToActivityForConfigChanges(binding: ActivityPluginBinding) =
        onAttachedToActivity(binding)

    override fun onDetachedFromActivityForConfigChanges() {
        hostLifecycle = null
        permissions.detach(keepPending = true)
    }

    override fun onDetachedFromActivity() {
        hostLifecycle = null
        permissions.detach()
    }

    companion object {
        // Must match the view type used by the Dart widget and the iOS plugin.
        const val VIEW_TYPE = "dev.aws.jvtsa/face_liveness_view"
    }
}

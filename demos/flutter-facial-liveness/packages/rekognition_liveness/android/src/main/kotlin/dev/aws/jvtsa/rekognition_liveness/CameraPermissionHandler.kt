package dev.aws.jvtsa.rekognition_liveness

import android.Manifest
import android.content.pm.PackageManager
import io.flutter.embedding.engine.plugins.activity.ActivityPluginBinding
import io.flutter.plugin.common.PluginRegistry

/**
 * Camera permission for the liveness check. The Amplify detector does not ask
 * for it and fails with CameraPermissionDeniedException when it is missing.
 * Uses framework APIs (minSdk 24), so no androidx.core dependency.
 */
internal class CameraPermissionHandler :
    LivenessPermissionApi, PluginRegistry.RequestPermissionsResultListener {

    private var binding: ActivityPluginBinding? = null
    private var pending: ((Result<Boolean>) -> Unit)? = null

    fun attach(binding: ActivityPluginBinding) {
        this.binding = binding
        binding.addRequestPermissionsResultListener(this)
    }

    /**
     * On a configuration change the result arrives on the recreated Activity,
     * so a pending request survives; on a real detach it resolves as denied.
     */
    fun detach(keepPending: Boolean = false) {
        binding?.removeRequestPermissionsResultListener(this)
        binding = null
        if (!keepPending) {
            pending?.invoke(Result.success(false))
            pending = null
        }
    }

    override fun requestCameraPermission(callback: (Result<Boolean>) -> Unit) {
        val activity = binding?.activity
        when {
            activity == null -> callback(
                Result.failure(FlutterError("no-activity", "Camera permission needs a foreground Activity.")),
            )
            activity.checkSelfPermission(Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED ->
                callback(Result.success(true))
            pending != null -> callback(
                Result.failure(FlutterError("in-progress", "A camera permission request is already running.")),
            )
            else -> {
                pending = callback
                activity.requestPermissions(arrayOf(Manifest.permission.CAMERA), REQUEST_CODE)
            }
        }
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray,
    ): Boolean {
        if (requestCode != REQUEST_CODE) return false
        val granted = grantResults.firstOrNull() == PackageManager.PERMISSION_GRANTED
        pending?.invoke(Result.success(granted))
        pending = null
        return true
    }

    private companion object {
        const val REQUEST_CODE = 0x4C56 // "LV"
    }
}

package dev.aws.jvtsa.rekognition_liveness

import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.LifecycleOwner
import androidx.lifecycle.LifecycleRegistry
import androidx.lifecycle.ViewModelStore
import androidx.lifecycle.ViewModelStoreOwner
import androidx.savedstate.SavedStateRegistry
import androidx.savedstate.SavedStateRegistryController
import androidx.savedstate.SavedStateRegistryOwner

/**
 * Lifecycle, saved-state and view-model owner scoped to one platform view.
 *
 * FlutterActivity extends android.app.Activity and registers no ViewTree
 * owners, so a ComposeView inside a platform view has none to find. The Amplify
 * detector also binds CameraX to LocalLifecycleOwner, so this owner:
 *  - mirrors the host lifecycle, pausing the camera when the app goes to the
 *    background;
 *  - goes to DESTROYED in [destroy], releasing the camera when the view is
 *    disposed even though the host Activity is still alive.
 */
internal class PlatformViewOwner(private val host: Lifecycle?) :
    LifecycleOwner, SavedStateRegistryOwner, ViewModelStoreOwner {

    private val registry = LifecycleRegistry(this)
    private val savedState = SavedStateRegistryController.create(this)
    private val store = ViewModelStore()

    override val lifecycle: Lifecycle get() = registry
    override val savedStateRegistry: SavedStateRegistry get() = savedState.savedStateRegistry
    override val viewModelStore: ViewModelStore get() = store

    // The host's ON_DESTROY is not forwarded: only destroy() ends this owner,
    // so composition teardown happens in PlatformView.dispose().
    private val mirror = LifecycleEventObserver { _, event ->
        if (!isDestroyed && event != Lifecycle.Event.ON_DESTROY) {
            registry.handleLifecycleEvent(event)
        }
    }

    val isDestroyed: Boolean get() = registry.currentState == Lifecycle.State.DESTROYED

    init {
        savedState.performAttach()
        savedState.performRestore(null)
        registry.currentState = Lifecycle.State.CREATED
        if (host != null) {
            // addObserver replays events up to the host's current state.
            host.addObserver(mirror)
        } else {
            registry.currentState = Lifecycle.State.RESUMED
        }
    }

    fun destroy() {
        if (isDestroyed) return
        host?.removeObserver(mirror)
        registry.currentState = Lifecycle.State.DESTROYED
        store.clear()
    }
}

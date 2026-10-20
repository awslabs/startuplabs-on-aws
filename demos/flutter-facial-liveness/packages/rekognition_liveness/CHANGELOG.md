## 0.3.0

Breaking changes to the Dart API and the Pigeon contract.

* Android implementation: `FaceLivenessDetector` (Amplify UI Liveness 1.11.0)
  hosted in a `ComposeView` platform view. Each view carries its own lifecycle,
  saved-state and view-model owners, because `FlutterActivity` registers none
  and the detector binds CameraX to `LocalLifecycleOwner`. The owner mirrors
  the host Activity and is destroyed with the view, which releases the camera.
* `onComplete` now receives `LivenessCompletion` (session id only). Neither
  Amplify component returns a verdict; read it from the backend. iOS no longer
  reports a fixed `isLive: true, confidence: 0`.
* `LivenessError.code` is a `LivenessErrorCode` enum shared by iOS and
  Android, with `isRetryable`.
* `LivenessCredentials.expiration` is required and travels to native code.
  Both SDKs read credentials once per session and never refresh.
* New `camera` parameter (`LivenessCamera.back` applies to
  FaceMovementChallenge only).
* New `LivenessPermissions.requestCamera()`. The Android detector does not ask
  for the permission and fails without it.
* `androidHybridComposition` opt-in. Default is the texture-layer `AndroidView`
  (the preview is a `TextureView`).
* Exactly one terminal callback per session, enforced in Dart and native code.
* Requires Flutter 3.38+ (`flutter_plugin_android_lifecycle`).

## 0.2.0

* Replace the hand-written MethodChannel bridge with type-safe **Pigeon**
  (`pigeons/messages.dart`). The Dart↔native contract (`LivenessHostApi` /
  `LivenessFlutterApi`) is now generated for Dart, Swift, and Kotlin.
* Per-view channels are now keyed by Pigeon's `messageChannelSuffix` (the
  platform view id) instead of a hand-built `..._$viewId` channel name.
* Public API (`LivenessDetectorWidget`, `LivenessResult`, `LivenessError`,
  `LivenessCredentialsProvider`) is unchanged, so consumers need no changes.

## 0.1.0

* Initial iOS Face Liveness plugin: `LivenessDetectorWidget` embedding Amplify's
  native `FaceLivenessDetectorView` via a UiKitView + per-view MethodChannel.

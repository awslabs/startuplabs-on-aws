## 0.2.0

* Replace the hand-written MethodChannel bridge with type-safe **Pigeon**
  (`pigeons/messages.dart`). The Dart↔native contract (`LivenessHostApi` /
  `LivenessFlutterApi`) is now generated for Dart, Swift, and Kotlin.
* Per-view channels are now keyed by Pigeon's `messageChannelSuffix` (the
  platform view id) instead of a hand-built `..._$viewId` channel name.
* Public API (`LivenessDetectorWidget`, `LivenessResult`, `LivenessError`,
  `LivenessCredentialsProvider`) is unchanged — no consumer changes required.

## 0.1.0

* Initial iOS Face Liveness plugin: `LivenessDetectorWidget` embedding Amplify's
  native `FaceLivenessDetectorView` via a UiKitView + per-view MethodChannel.

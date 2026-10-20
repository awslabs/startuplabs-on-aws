# rekognition_liveness

A Flutter plugin that embeds AWS Amplify's native **FaceLivenessDetector**
(Amazon Rekognition Face Liveness) into a Flutter app through a Platform View
on iOS and Android. It exposes a `LivenessDetectorWidget` on the Dart side and
bridges to the native SwiftUI
`FaceLivenessDetectorView` (iOS) and the Compose `FaceLivenessDetector`
(Android) over a type-safe, per-view [Pigeon](https://pub.dev/packages/pigeon)
channel.

> On other platforms the widget reports `LivenessErrorCode.platformNotSupported`.

## Installation

The plugin **dual-ships**: it provides both a `Package.swift` (Swift Package
Manager) and a `.podspec` (CocoaPods), so it installs under either iOS toolchain.

```yaml
dependencies:
  rekognition_liveness:
    path: ../packages/rekognition_liveness
```

Then run `flutter pub get`.

### The Amplify dependency caveat (read this)

The AWS Amplify liveness SDK (`amplify-ui-swift-liveness` and `amplify-swift`
v2) is **Swift Package Manager only**: it has no CocoaPod. A CocoaPods podspec
**cannot** pull a Swift Package. This shapes how the plugin behaves per toolchain:

| Consumer toolchain | Plugin glue | Amplify liveness SDK | Result |
|--------------------|-------------|----------------------|--------|
| **Swift Package Manager** (Flutter 3.44+ default) | via `Package.swift` | pulled automatically by `Package.swift` | **Full liveness works** |
| **CocoaPods** (`pod install`) | via `.podspec` | *not* pulled (podspec can't) | Plugin installs & compiles; liveness needs the manual step below |

The native code is guarded with `#if canImport(FaceLiveness)`, so it compiles in
both modes. When Amplify is absent (a CocoaPods app that has not added it), the
detector reports `LivenessErrorCode.sdkNotLinked` at runtime rather than failing to
build.

### Enabling liveness in a CocoaPods app

If your app uses CocoaPods and you want the liveness check to actually run, add
the Amplify liveness Swift Package to the **Runner** target (Xcode → *File → Add
Package Dependencies*), pinned to the tested matrix:

- `https://github.com/aws-amplify/amplify-ui-swift-liveness`, exact `1.4.2`
- `https://github.com/aws-amplify/amplify-swift`, exact `2.53.2`

This is the same version matrix `Package.swift` pins for SPM consumers (it
resolves transitively to `aws-sdk-swift` 1.6.7 and `smithy-swift` 0.175.0, iOS
14 minimum). Xcode links the two package products alongside the CocoaPods glue,
`canImport(FaceLiveness)` becomes true, and the full implementation compiles.

> Recommendation: prefer Swift Package Manager. It is the Flutter 3.44+ default,
> it is the only toolchain AWS supports for Amplify Swift v2, and the CocoaPods
> trunk goes read-only at the end of 2026.

## Usage

```dart
// 1. Ask for the camera first (after your own rationale screen).
if (!await LivenessPermissions.requestCamera()) return;

// 2. Show the detector.
LivenessDetectorWidget(
  sessionId: sessionId,          // from your backend CreateFaceLivenessSession
  region: 'sa-east-1',           // region where the session was created
  credentialsProvider: myCredentialsProvider, // temporary creds + expiration
  camera: LivenessCamera.front,  // back applies to FaceMovementChallenge only
  onComplete: (completion) { /* ask the backend for the verdict */ },
  onError: (error) { /* error.code (LivenessErrorCode), error.isRetryable */ },
);
```

`onComplete` is a completion signal, not a verdict. Neither native component
returns a score. Fetch it server-side with `GetFaceLivenessSessionResults` and
decide there.

`LivenessCredentials.expiration` must be the real expiration returned by
Cognito (`GetCredentialsForIdentity`). Both SDKs read the credentials once per
session and never refresh them.

## Android

### Two settings your app must add

The plugin cannot set these for you: they belong to the application module.
Without them the build fails before any of your code runs.

```kotlin
// android/app/build.gradle.kts
android {
    compileOptions {
        // com.amplifyframework:core 2.38.1 still requires it, even though
        // liveness 1.11.0 dropped the requirement from the UI libraries.
        isCoreLibraryDesugaringEnabled = true
    }
}
dependencies {
    coreLibraryDesugaring("com.android.tools:desugar_jdk_libs:2.1.5")
}
```

```properties
# android/gradle.properties
# LiteRT 1.4.1 (via Amplify UI Liveness) ships two AARs that share the
# namespace org.tensorflow.lite.support; AGP 9 rejects that by default.
android.uniquePackageNames=false
```

Symptoms if you skip them: `checkDebugAarMetadata` fails with "requires core
library desugaring to be enabled", or the manifest merger fails with
"Namespace is used in multiple modules and/or libraries".

### Camera requirement and Google Play filtering

The Amplify component's manifest declares
`<uses-feature android:name="android.hardware.camera.any" />`, which defaults to
`android:required="true"`. It is merged into your app, so Google Play hides the
app from devices that have no camera at all.

That is right when the liveness check is the app's purpose. When liveness is one
feature of a larger app, override the flag in your app manifest so the rest of
the app stays installable:

```xml
<manifest xmlns:android="http://schemas.android.com/apk/res/android"
    xmlns:tools="http://schemas.android.com/tools">
    <uses-feature
        android:name="android.hardware.camera.any"
        android:required="false"
        tools:replace="android:required" />
    ...
</manifest>
```

Then check for a camera before opening the flow, for example with
[`device_info_plus`](https://pub.dev/packages/device_info_plus):

```dart
final features = (await DeviceInfoPlugin().androidInfo).systemFeatures;
// FaceMovementAndLightChallenge always uses the front camera.
final hasFrontCamera = features.contains('android.hardware.camera.front');
```

The front camera is the one that matters: `FaceMovementAndLightChallenge` always
uses it, and only `FaceMovementChallenge` can use the back camera.

### How it works

- `com.amplifyframework.ui:liveness:1.11.0`, pinned. 1.8.2 and 1.9.0 are
  deprecated upstream. No `aws-auth-cognito` and no `Amplify.configure`:
  credentials come from Dart.
- The detector is a Compose component. Each platform view hosts a `ComposeView`
  with its own lifecycle, saved-state and view-model owners **and its own
  `Recomposer`**. `FlutterActivity` extends `android.app.Activity` and registers
  no ViewTree owners; without the Recomposer, Compose installs a window
  recomposer on the `FlutterView` and crashes with
  `ViewTreeLifecycleOwner not found`. The owner mirrors the host Activity (the
  camera pauses in background) and is destroyed with the view (the camera is
  released). This works under `FlutterActivity`, `FlutterFragmentActivity` and a
  `FlutterFragment` inside a native app.
- Default mode is the texture-layer `AndroidView`; the camera preview is a
  `TextureView`. Set `androidHybridComposition: true` only if a device shows a
  black or frozen preview. Hybrid Composition copies every frame through main
  memory on Android 8 and 9.
- The detector locks portrait and raises screen brightness on the host
  Activity while it runs, and restores both when the view is disposed.
- Compose BOM 2026.03.00, the same as liveness 1.11.0. Lifecycle is pinned to
  2.8.7 and savedstate to 1.3.0, the floors that BOM already exposes: lifecycle
  2.11 would force compileSdk 37 and AGP 9.1 on your app. Align the BOM with
  the host app when embedding in a native Android app.
- `SessionInterruptedException` does not exist in 1.11.0, so backgrounding the
  app mid-challenge surfaces as `LivenessErrorCode.unknown`.
- JVM tests: from `app_example/android` (after one `flutter build apk` there),
  run `./gradlew :rekognition_liveness:testDebugUnitTest`.

## The native bridge (Pigeon)

The Dart↔native contract is generated by Pigeon from a single schema
([`pigeons/messages.dart`](pigeons/messages.dart)), so there are no hand-matched
channel-name strings or argument maps. The generated files are checked in:

- `lib/src/messages.g.dart`
- `ios/rekognition_liveness/Sources/rekognition_liveness/Messages.g.swift`
- `android/src/main/kotlin/dev/aws/jvtsa/rekognition_liveness/Messages.g.kt`

Per view, keyed by the platform view id via Pigeon's `messageChannelSuffix`
so multiple liveness views never cross-talk:

- **`LivenessHostApi`** (Dart to native): `setCredentials(...)`. Receiving the
  temporary AWS credentials is the signal for the native side to present the
  detector.
- **`LivenessFlutterApi`** (native to Dart): `onComplete(...)` or
  `onError(...)`, exactly once per session.

App-wide:

- **`LivenessPermissionApi`** (Dart to native): `requestCameraPermission()`.

To regenerate after editing the schema, from the package root:

```
dart run pigeon --input pigeons/messages.dart
```

Do not edit the `*.g.*` files by hand.

## Requirements

- Flutter 3.38+
- iOS 14.0+, Swift 5.9+
- Android API 24+ (Amplify UI Liveness minimum), compileSdk 36
- A backend that creates liveness sessions and returns results (see the
  `backend/cdk` stack in this repository), and a Cognito Identity Pool guest role
  scoped to `rekognition:StartFaceLivenessSession`.

#
# rekognition_liveness — Flutter plugin bridging AWS Amplify FaceLivenessDetector.
#
# DUAL-SHIP: this plugin provides BOTH a Package.swift (Swift Package Manager)
# and this .podspec (CocoaPods), pointing at the same Sources/ tree. Consumers
# on either toolchain can install the plugin.
#
# IMPORTANT — the Amplify liveness SDK is Swift Package Manager ONLY. A CocoaPods
# podspec cannot pull a Swift Package, so this podspec ships only the plugin's
# own native glue. The native code is guarded with `#if canImport(FaceLiveness)`:
#   * SPM consumer  -> Package.swift pulls Amplify; full liveness works.
#   * Pods consumer -> plugin installs and compiles via `pod install`; to enable
#                      liveness, add the amplify-ui-swift-liveness Swift Package
#                      to the Runner target (see the plugin README). Without it,
#                      the detector reports an AMPLIFY_NOT_LINKED error at runtime.
#
# Validate with:  pod lib lint rekognition_liveness.podspec
#
Pod::Spec.new do |s|
  s.name             = 'rekognition_liveness'
  s.version          = '0.1.0'
  s.summary          = 'Flutter plugin embedding AWS Amplify FaceLivenessDetector via a Platform View.'
  s.description      = <<-DESC
Bridges the native AWS Amplify Face Liveness UI (FaceLivenessDetectorView) into
Flutter through a UiKitView platform view. The Amplify SDK itself is delivered
via Swift Package Manager; this pod carries the plugin's native glue.
                       DESC
  s.homepage         = 'https://github.com/aws-samples/rekognition-liveness-flutter'
  s.license          = { :file => '../LICENSE' }
  s.author           = { 'AWS SA' => 'noreply@amazon.com' }
  s.source           = { :path => '.' }
  s.source_files     = 'rekognition_liveness/Sources/rekognition_liveness/**/*.swift'
  s.resource_bundles = {
    'rekognition_liveness_privacy' => ['rekognition_liveness/Sources/rekognition_liveness/PrivacyInfo.xcprivacy']
  }
  s.dependency 'Flutter'

  # Must match Package.swift and the Amplify liveness SDK minimum.
  s.platform = :ios, '14.0'

  s.pod_target_xcconfig = {
    'DEFINES_MODULE' => 'YES',
    'EXCLUDED_ARCHS[sdk=iphonesimulator*]' => 'i386',
  }
  s.swift_version = '5.9'
end

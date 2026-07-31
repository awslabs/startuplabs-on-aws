// swift-tools-version: 5.9

import PackageDescription

let package = Package(
    name: "rekognition_liveness",
    platforms: [
        .iOS("14.0")
    ],
    products: [
        .library(name: "rekognition-liveness", targets: ["rekognition_liveness"])
    ],
    dependencies: [
        .package(name: "FlutterFramework", path: "../FlutterFramework"),
        // Version matrix is tight here:
        //  - amplify-swift < 2.5x fails on Xcode 26 (__IPHONE_OS_VERSION_MIN_REQUIRED gone)
        //  - amplify-swift >= 2.54 pulls aws-sdk-swift 1.7+, whose smithy-swift
        //    codegen build plugin does not compile under Flutter's SPM build
        // liveness 1.4.2 allows amplify-swift >= 2.49, so pin 2.53.2 (aws-sdk 1.6.7).
        .package(
            url: "https://github.com/aws-amplify/amplify-ui-swift-liveness.git",
            exact: "1.4.2"
        ),
        // URL must match the one used inside amplify-ui-swift-liveness
        // (no .git suffix) or Xcode drops this constraint when deduping.
        .package(
            url: "https://github.com/aws-amplify/amplify-swift",
            exact: "2.53.2"
        ),
    ],
    targets: [
        .target(
            name: "rekognition_liveness",
            dependencies: [
                .product(name: "FlutterFramework", package: "FlutterFramework"),
                .product(name: "FaceLiveness", package: "amplify-ui-swift-liveness"),
                // Explicit product reference so SPM does not prune the
                // amplify-swift dependency (and with it, our exact pin).
                .product(name: "AWSPluginsCore", package: "amplify-swift"),
            ]
        )
    ]
)

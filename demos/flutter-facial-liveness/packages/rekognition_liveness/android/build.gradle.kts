group = "dev.aws.jvtsa.rekognition_liveness"
version = "1.0-SNAPSHOT"

buildscript {
    val kotlinVersion = "2.3.20"
    repositories {
        google()
        mavenCentral()
    }

    dependencies {
        classpath("com.android.tools.build:gradle:9.0.1")
        classpath("org.jetbrains.kotlin:kotlin-gradle-plugin:$kotlinVersion")
        classpath("org.jetbrains.kotlin:compose-compiler-gradle-plugin:$kotlinVersion")
    }
}

allprojects {
    repositories {
        google()
        mavenCentral()
    }
}

plugins {
    id("com.android.library")
}

// Kotlin 2.x ships the Compose compiler as a Gradle plugin. Applied from the
// buildscript classpath so host apps need no extra plugin declaration.
apply(plugin = "org.jetbrains.kotlin.plugin.compose")

android {
    namespace = "dev.aws.jvtsa.rekognition_liveness"

    compileSdk = 36

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    sourceSets {
        getByName("main") {
            java.srcDirs("src/main/kotlin")
        }
        getByName("test") {
            java.srcDirs("src/test/kotlin")
        }
    }

    defaultConfig {
        // Amplify UI Liveness minimum.
        minSdk = 24
    }

    buildFeatures {
        compose = true
    }

    testOptions {
        unitTests {
            isIncludeAndroidResources = true
            all {
                it.useJUnitPlatform()

                it.outputs.upToDateWhen { false }

                it.testLogging {
                    events("passed", "skipped", "failed", "standardOut", "standardError")
                    showStandardStreams = true
                }
            }
        }
    }
}

kotlin {
    compilerOptions {
        jvmTarget = org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17
    }
}

dependencies {
    // Exact pin: 1.8.2 and 1.9.0 are deprecated; 1.11.0 drops Amplify.API and
    // the desugaring requirement. No aws-auth-cognito: credentials come from Dart.
    implementation("com.amplifyframework.ui:liveness:1.11.0")

    // Same BOM as liveness 1.11.0, to avoid pulling a second Compose version
    // into the host app.
    implementation(platform("androidx.compose:compose-bom:2026.03.00"))
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.material3:material3")

    // ViewTree owners for the ComposeView (FlutterActivity provides none).
    // Floors match what Compose UI 1.10.5 (BOM 2026.03.00) already exposes;
    // lifecycle 2.11 would force compileSdk 37 and AGP 9.1 on the host app.
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.7")
    implementation("androidx.lifecycle:lifecycle-viewmodel-ktx:2.8.7")
    implementation("androidx.savedstate:savedstate-ktx:1.3.0")

    testImplementation("org.jetbrains.kotlin:kotlin-test")
    testImplementation("org.mockito:mockito-core:5.0.0")
}

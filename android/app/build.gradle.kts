plugins {
    id("com.android.application")
}

android {
    namespace = "es.fvegas.healthhubbridge"
    compileSdk = 37

    defaultConfig {
        applicationId = "es.fvegas.healthhubbridge"
        minSdk = 28
        targetSdk = 35
        versionCode = 1
        versionName = "0.1.0"
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.15.0")
    implementation("androidx.activity:activity-ktx:1.10.0")
    implementation("androidx.work:work-runtime-ktx:2.10.0")
    implementation("androidx.health.connect:connect-client:1.1.0")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
}

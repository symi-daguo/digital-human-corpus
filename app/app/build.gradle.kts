plugins {
    alias(libs.plugins.android.application)
    // 不声明 org.jetbrains.kotlin.android：AGP 9 内置 Kotlin，
    // 重复声明会报 "Cannot add extension with name 'kotlin'"。
    alias(libs.plugins.compose.compiler)
    alias(libs.plugins.kotlin.serialization)
    alias(libs.plugins.ksp)
}

// 发布签名配置（不入库）：keystore.properties 存在则启用，缺失时回退 debug 签名
val keystoreProps: Map<String, String> = rootProject.file("keystore.properties").let { f ->
    if (!f.exists()) emptyMap()
    else f.readLines()
        .filter { it.isNotBlank() && !it.trimStart().startsWith("#") }
        .mapNotNull { line ->
            val i = line.indexOf('=')
            if (i > 0) line.substring(0, i).trim() to line.substring(i + 1).trim() else null
        }
        .toMap()
}

// 出厂默认服务端主机（**不入库**）：与 keystore.properties 同一套路。
// 内网地址属于「本机部署事实」——写进仓库既会随网络变化而失效，也没有必要外泄。
// 读取 app/dh.properties 的 defaultMacHost；文件缺失时退回空串，空串在
// MacClient / GuardService 里都是被显式处理的（提示「还没填 Mac IP 地址」），
// 不会静默失败。字段含义见 app/dh.properties.example。
val dhProps: Map<String, String> = rootProject.file("dh.properties").let { f ->
    if (!f.exists()) emptyMap()
    else f.readLines()
        .filter { it.isNotBlank() && !it.trimStart().startsWith("#") }
        .mapNotNull { line ->
            val i = line.indexOf('=')
            if (i > 0) line.substring(0, i).trim() to line.substring(i + 1).trim() else null
        }
        .toMap()
}
val defaultMacHost: String = dhProps["defaultMacHost"].orEmpty().replace("\"", "")

android {
    namespace = "com.symi.digitalhuman"
    compileSdk = 37

    defaultConfig {
        applicationId = "com.symi.digitalhuman"
        // 28 = Android 9。面向低端机顶盒/电视盒（Android 9、2GB 内存级别）向下兼容；
        // 代码侧已用 lintRelease 验证：minSdk=28 时 NewApi 类问题为 0。
        // ⚠️ 改这一行后必须重新构建：release 开 isMinifyEnabled 时 R8 会按 minSdk
        // 折叠 SDK_INT 判断，拿 minSdk=29 编出的包在 Android 9 上会 NoSuchMethodError。
        minSdk = 28
        // 刻意保持 36：Play 强制要求为 36，升到 37 会引入 Android 17 行为变更，
        // 属于独立变更，不与本次工具链升级混做。
        targetSdk = 36
        // versionCode 每次发新包必须 +1（同签名可覆盖安装、不丢数据）。
        // 3 → 4：1.5.16 起日报页支持「打开即同步服务端处理结果」。
        versionCode = 20
        versionName = "1.0.1"
        // 出厂默认服务端地址：取自不入库的 dh.properties（见文件顶部说明），
        // 缺文件时为空串 —— 仓库里不出现任何内网/公网地址字面量。
        buildConfigField("String", "DEFAULT_MAC_HOST", "\"$defaultMacHost\"")
    }

    // 发布签名：keystore.properties 存在则用它；不存在回退 debug 签名
    // （保证他人 clone 仓库后仍能构建，密钥与密码文件均不入库）
    signingConfigs {
        if (keystoreProps.isNotEmpty()) {
            create("release") {
                storeFile = rootProject.file(keystoreProps.getValue("storeFile"))
                storePassword = keystoreProps.getValue("storePassword")
                keyAlias = keystoreProps.getValue("keyAlias")
                keyPassword = keystoreProps.getValue("keyPassword")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
            signingConfig = signingConfigs.findByName("release") ?: signingConfigs.getByName("debug")
        }
    }

    // ABI 拆分：真机一般用 arm64-v8a，体积可从 191MB 降到 ~50-60MB；
    // universal 兜底包同步生成，给模拟器/特殊设备用。
    splits {
        abi {
            isEnable = true
            reset()
            include("arm64-v8a", "armeabi-v7a", "x86_64")
            isUniversalApk = true
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    buildFeatures {
        compose = true
        buildConfig = true
    }
    packaging {
        resources.excludes += "META-INF/{AL2.0,LGPL2.1}"
    }
}

// AGP 9 内置 Kotlin 后，android.kotlinOptions{} 已移除，改用 kotlin.compilerOptions{}
kotlin {
    compilerOptions {
        jvmTarget = org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17
    }
}

dependencies {
    implementation(platform(libs.compose.bom))

    // Compose UI（版本全部由 BOM 决定）
    implementation(libs.compose.ui)
    implementation(libs.compose.ui.tooling.preview)
    implementation(libs.compose.material3)
    // 响应式布局（WindowSizeClass：手机/平板/横屏自适应）
    implementation(libs.compose.material3.windowsize)
    implementation(libs.compose.material.icons.extended)
    implementation(libs.activity.compose)
    implementation(libs.lifecycle.viewmodel.compose)
    implementation(libs.lifecycle.runtime.compose)
    implementation(libs.navigation.compose)
    debugImplementation(libs.compose.ui.tooling)

    // Data
    implementation(libs.room.runtime)
    implementation(libs.room.ktx)
    ksp(libs.room.compiler)
    implementation(libs.datastore.preferences)
    implementation(libs.serialization.json)

    // Work & service
    implementation(libs.work.runtime.ktx)
    implementation(libs.core.ktx)
    implementation(libs.core.splashscreen)

    // Network
    implementation(libs.okhttp)
    implementation(libs.okhttp.logging.interceptor)

    // VAD（sherpa-onnx 提供 Silero VAD Kotlin API；官方以 GitHub Releases AAR 分发）
    implementation(files("libs/sherpa-onnx-android.aar"))

    // Test
    testImplementation(libs.junit)
    testImplementation(libs.coroutines.test)
    androidTestImplementation(libs.androidx.junit)
}

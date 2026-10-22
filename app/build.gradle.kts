// 说明：AGP 9.0 起内置 Kotlin 编译，默认自带 KGP 2.2.10 / KSP 2.2.10-2.0.2。
// 要用更高的 KGP / KSP，必须在 buildscript classpath 里显式声明（Google 官方升级方式）。
// buildscript 不读 settings 的 pluginManagement 仓库，需要自己声明。
buildscript {
    repositories {
        maven("https://maven.aliyun.com/repository/google")
        maven("https://maven.aliyun.com/repository/public")
        google()
        mavenCentral()
        gradlePluginPortal()
    }
    dependencies {
        classpath(libs.kotlin.gradle.plugin)
        classpath(libs.ksp.gradle.plugin)
    }
}

plugins {
    alias(libs.plugins.android.application) apply false
    // 注意：这里故意没有 org.jetbrains.kotlin.android —— AGP 9 内置 Kotlin，
    // 再声明会报 "Cannot add extension with name 'kotlin'"。
    alias(libs.plugins.compose.compiler) apply false
    alias(libs.plugins.kotlin.serialization) apply false
    alias(libs.plugins.ksp) apply false
}

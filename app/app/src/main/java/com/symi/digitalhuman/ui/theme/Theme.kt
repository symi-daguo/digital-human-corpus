package com.symi.digitalhuman.ui.theme

import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.compositionLocalOf
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

/**
 * 设计 Token 单一事实源。
 *
 * 2026 大厂审美的关键调整（相对旧版）：
 *  - 颜色从「硬编码 Glass.X」改为 M3 调色板 + surfaceContainer 分层（Surface0/1/2/3），
 *    让"卡片层级"靠 tonal elevation 而非描边透明度，弱化"每张卡都同款"模板感。
 *  - 圆角统一阶梯：6 / 10 / 14 / 20 / 28（按钮 / 输入 / 卡片 / 弹层 / Hero），避免 20 一统天下。
 *  - 字体走 M3 Typography 五档（display/headline/title/body/label），不再 sp 散落。
 *  - 间距全部 8dp 网格（4/8/12/16/20/24/32/48）。
 */
object Glass {
    // 背景 / 表面（M3 surfaceContainer 体系）
    val Bg           = Color(0xFF0B0F1A)
    val Surface0     = Color(0xFF0F1424)   // 凹陷区 / 整页底
    val Surface1     = Color(0xFF161D33)   // 主卡（raised 1）
    val Surface2     = Color(0xFF1E2740)   // 子区 / 选中态（raised 2）
    val Surface3     = Color(0xFF283356)   // 浮层（raised 3）
    // 描边
    val Stroke       = Color(0xFF1F2A47)   // 卡片默认描边
    val StrokeStrong = Color(0xFF2E3B5E)   // 高对比描边 / 选中描边
    // 文字
    val Text         = Color(0xFFECF1FA)   // 主文字
    val TextDim      = Color(0xFF95A2BD)   // 次级文字 / hint
    val TextFaint    = Color(0xFF5C6A87)   // 弱化文字 / 时间戳
    // 语义色：仅用于"状态/主操作"，不再作为装饰色滥用
    val Teal         = Color(0xFF5EEAD4)   // 主操作 / 运行中
    val Indigo       = Color(0xFF818CF8)   // 信息 / 回放
    val Amber        = Color(0xFFFBBF24)   // 暂停 / 待办
    val Red          = Color(0xFFF87171)   // 危险 / 停止
    val Green        = Color(0xFF4ADE80)   // 成功 / 已同步
    val OnAccent     = Color(0xFF06211C)   // 强调色按钮上的文字
}

private val DarkScheme = darkColorScheme(
    primary = Glass.Teal,
    onPrimary = Glass.OnAccent,
    primaryContainer = Glass.Teal.copy(alpha = 0.16f),
    onPrimaryContainer = Glass.Teal,
    secondary = Glass.Indigo,
    onSecondary = Glass.OnAccent,
    secondaryContainer = Glass.Indigo.copy(alpha = 0.16f),
    onSecondaryContainer = Glass.Indigo,
    tertiary = Glass.Amber,
    onTertiary = Glass.OnAccent,
    tertiaryContainer = Glass.Amber.copy(alpha = 0.16f),
    onTertiaryContainer = Glass.Amber,
    background = Glass.Bg,
    onBackground = Glass.Text,
    surface = Glass.Surface1,
    onSurface = Glass.Text,
    surfaceVariant = Glass.Surface2,
    onSurfaceVariant = Glass.TextDim,
    surfaceContainerLowest = Glass.Bg,
    surfaceContainerLow = Glass.Surface0,
    surfaceContainer = Glass.Surface1,
    surfaceContainerHigh = Glass.Surface2,
    surfaceContainerHighest = Glass.Surface3,
    outline = Glass.Stroke,
    outlineVariant = Glass.StrokeStrong,
    error = Glass.Red,
    onError = Glass.OnAccent,
    scrim = Color(0xCC000000),
    inverseSurface = Glass.Surface3,
    inverseOnSurface = Glass.Text,
    inversePrimary = Glass.Teal,
)

private val AppTypography = Typography(
    displaySmall  = TextStyle(fontSize = 32.sp, lineHeight = 40.sp, fontWeight = FontWeight.Bold,    letterSpacing = (-0.5).sp),
    headlineLarge = TextStyle(fontSize = 28.sp, lineHeight = 36.sp, fontWeight = FontWeight.Bold,    letterSpacing = (-0.25).sp),
    headlineMedium= TextStyle(fontSize = 22.sp, lineHeight = 28.sp, fontWeight = FontWeight.SemiBold),
    headlineSmall = TextStyle(fontSize = 18.sp, lineHeight = 24.sp, fontWeight = FontWeight.SemiBold),
    titleLarge    = TextStyle(fontSize = 18.sp, lineHeight = 24.sp, fontWeight = FontWeight.SemiBold),
    titleMedium   = TextStyle(fontSize = 15.sp, lineHeight = 22.sp, fontWeight = FontWeight.SemiBold),
    titleSmall    = TextStyle(fontSize = 13.sp, lineHeight = 20.sp, fontWeight = FontWeight.SemiBold, letterSpacing = 0.1.sp),
    bodyLarge     = TextStyle(fontSize = 16.sp, lineHeight = 24.sp, fontWeight = FontWeight.Normal),
    bodyMedium    = TextStyle(fontSize = 14.sp, lineHeight = 22.sp, fontWeight = FontWeight.Normal),
    bodySmall     = TextStyle(fontSize = 12.sp, lineHeight = 18.sp, fontWeight = FontWeight.Normal),
    labelLarge    = TextStyle(fontSize = 14.sp, lineHeight = 20.sp, fontWeight = FontWeight.Medium,  letterSpacing = 0.1.sp),
    labelMedium   = TextStyle(fontSize = 12.sp, lineHeight = 16.sp, fontWeight = FontWeight.Medium,  letterSpacing = 0.5.sp),
    labelSmall    = TextStyle(fontSize = 11.sp, lineHeight = 16.sp, fontWeight = FontWeight.Medium,  letterSpacing = 0.5.sp),
)

/** 8dp 网格间距（CompositionLocal 注入，避免到处写 .dp 字面量） */
data class Spacing(
    val xs: Dp = 4.dp,   val sm: Dp = 8.dp,   val md: Dp = 12.dp,
    val lg: Dp = 16.dp,  val xl: Dp = 20.dp,  val xxl: Dp = 24.dp,
    val xxxl: Dp = 32.dp, val huge: Dp = 48.dp,
)
val LocalSpacing = staticCompositionLocalOf { Spacing() }

/** 圆角阶梯（5 档） */
data class AppRadius(val xs: Dp = 6.dp, val sm: Dp = 10.dp, val md: Dp = 14.dp, val lg: Dp = 20.dp, val xl: Dp = 28.dp)
val LocalRadius = staticCompositionLocalOf { AppRadius() }

@Composable
fun DigitalHumanTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = DarkScheme,
        typography = AppTypography,
        content = content,
    )
}

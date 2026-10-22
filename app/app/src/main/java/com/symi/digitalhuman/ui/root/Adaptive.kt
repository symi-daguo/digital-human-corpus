package com.symi.digitalhuman.ui.root

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.widthIn
import androidx.compose.material3.windowsizeclass.WindowSizeClass
import androidx.compose.material3.windowsizeclass.WindowWidthSizeClass
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp

/**
 * 响应式布局工具（Material 3 自适应规范）：
 * - 宽度 Compact（<600dp，手机竖屏）：底部导航栏 + 内容满宽
 * - 宽度 Medium（600-840dp，平板竖屏 / 手机横屏）：左侧导航栏 + 内容限宽 640dp
 * - 宽度 Expanded（≥840dp，平板横屏 / 折叠展开）：左侧导航栏 + 内容限宽 900dp + 卡片双列
 *
 * 内容限宽避免卡片被拉伸、行长过长导致可读性下降。
 */
@Composable
fun AdaptiveContent(
    windowSizeClass: WindowSizeClass,
    modifier: Modifier = Modifier,
    content: @Composable () -> Unit,
) {
    val maxWidth = when (windowSizeClass.widthSizeClass) {
        WindowWidthSizeClass.Compact -> 0.dp      // 0 = 不限宽
        WindowWidthSizeClass.Medium -> 640.dp
        else -> 900.dp
    }
    Box(modifier = modifier.fillMaxSize(), contentAlignment = Alignment.TopCenter) {
        val inner = if (maxWidth == 0.dp) Modifier.fillMaxWidth()
        else Modifier.widthIn(max = maxWidth).fillMaxWidth()
        Box(modifier = inner) { content() }
    }
}

/** 卡片列表：展开屏（≥840dp）自动双列，其余单列。 */
@Composable
fun AdaptiveCards(
    windowSizeClass: WindowSizeClass,
    cards: List<@Composable () -> Unit>,
) {
    when (windowSizeClass.widthSizeClass) {
        WindowWidthSizeClass.Compact, WindowWidthSizeClass.Medium -> {
            Column(verticalArrangement = Arrangement.spacedBy(14.dp)) {
                cards.forEach { card -> card() }
            }
        }

        else -> {
            val half = (cards.size + 1) / 2
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(14.dp),
            ) {
                Column(
                    modifier = Modifier.weight(1f),
                    verticalArrangement = Arrangement.spacedBy(14.dp),
                ) { cards.take(half).forEach { card -> card() } }
                Column(
                    modifier = Modifier.weight(1f),
                    verticalArrangement = Arrangement.spacedBy(14.dp),
                ) { cards.drop(half).forEach { card -> card() } }
            }
        }
    }
}

/** 是否使用左侧导航栏（NavigationRail）：宽度 ≥600dp。 */
fun useNavRail(windowSizeClass: WindowSizeClass): Boolean =
    windowSizeClass.widthSizeClass != WindowWidthSizeClass.Compact

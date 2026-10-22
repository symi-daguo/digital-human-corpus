package com.symi.digitalhuman.ui.theme

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp

/**
 * 统一组件库（2026 重设计）：
 *  - 取消 3dp 彩色竖条 / 渐变描边玻璃模板，改为 M3 surfaceContainer 分层。
 *  - 取消 11sp 散落 / 13sp 散落；全部走 MaterialTheme.typography。
 *  - 强调色仅作为"语义"使用（运行中=Teal / 已停止=Red / 待办=Amber），不再作为装饰。
 *  - 所有点击目标 ≥48dp（M3 规范）。
 */

/** 主卡：M3 raised surface，1dp 描边 + tonal 背景，去掉渐变玻璃模板。 */
@Composable
fun GlassCard(
    modifier: Modifier = Modifier,
    contentPadding: PaddingValues = PaddingValues(16.dp),
    elevated: Boolean = false,
    content: @Composable ColumnScope.() -> Unit,
) {
    val shape = RoundedCornerShape(14.dp)
    Column(
        modifier = modifier
            .fillMaxWidth()
            .clip(shape)
            .background(if (elevated) Glass.Surface2 else Glass.Surface1)
            .border(1.dp, if (elevated) Glass.StrokeStrong else Glass.Stroke, shape)
            .padding(contentPadding),
        content = content,
    )
}

/** 子区卡：凹陷感（Surface0），用于卡片内的分组、列表区段。 */
@Composable
fun InsetCard(
    modifier: Modifier = Modifier,
    contentPadding: PaddingValues = PaddingValues(horizontal = 14.dp, vertical = 12.dp),
    content: @Composable ColumnScope.() -> Unit,
) {
    val shape = RoundedCornerShape(10.dp)
    Column(
        modifier = modifier
            .fillMaxWidth()
            .clip(shape)
            .background(Glass.Surface0)
            .border(1.dp, Glass.Stroke.copy(alpha = 0.5f), shape)
            .padding(contentPadding),
        content = content,
    )
}

/** 区块标题：去掉 AI 装饰的"3dp 彩色竖条"，改为 6dp 圆点 + 标题（与正文层级分开的标题色）。
 * 不再用 section color 装饰，标题色统一 Glass.Text，配上小圆点表示「这一类信息」。
 */
@Composable
fun PageHeader(
    title: String,
    subtitle: String? = null,
    modifier: Modifier = Modifier,
    trailing: (@Composable () -> Unit)? = null,
) {
    Row(
        modifier = modifier.fillMaxWidth(),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Column(Modifier.weight(1f)) {
            Text(
                title,
                style = MaterialTheme.typography.headlineMedium,
                color = Glass.Text,
            )
            if (!subtitle.isNullOrBlank()) {
                Spacer(Modifier.height(4.dp))
                Text(
                    subtitle,
                    style = MaterialTheme.typography.bodySmall,
                    color = Glass.TextDim,
                )
            }
        }
        if (trailing != null) trailing()
    }
}

/**
 * 区块标题：去掉 AI 装饰的"3dp 彩色竖条"，改为 6dp 圆点 + 标题（与正文层级分开的标题色）。
 * 不再用 section color 装饰，标题色统一 Glass.Text，配上小圆点表示「这一类信息」。
 */
@Composable
fun SectionTitle(
    title: String,
    modifier: Modifier = Modifier,
    subtitle: String? = null,
    accent: Color = Glass.Teal,
    trailing: (@Composable () -> Unit)? = null,
) {
    Row(
        modifier = modifier.fillMaxWidth(),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Box(
            Modifier
                .size(6.dp)
                .clip(CircleShape)
                .background(accent)
        )
        Spacer(Modifier.width(8.dp))
        Text(
            title,
            style = MaterialTheme.typography.titleMedium,
            color = Glass.Text,
        )
        if (!subtitle.isNullOrBlank()) {
            Spacer(Modifier.width(8.dp))
            Text(
                subtitle,
                style = MaterialTheme.typography.bodySmall,
                color = Glass.TextDim,
                maxLines = 1,
            )
        }
        if (trailing != null) {
            Spacer(Modifier.weight(1f))
            trailing()
        }
    }
}

/** 键值行：左 label（小字 dim） + 右 value（中等字 main），可附 helper 副行。 */
@Composable
fun StatRow(
    label: String,
    value: String,
    accent: Color = Glass.Text,
    modifier: Modifier = Modifier,
    helper: String? = null,
) {
    Row(
        modifier = modifier
            .fillMaxWidth()
            .heightIn(min = 36.dp)
            .padding(vertical = 4.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Column(Modifier.weight(1f)) {
            Text(label, style = MaterialTheme.typography.bodyMedium, color = Glass.TextDim)
            if (!helper.isNullOrBlank()) {
                Text(
                    helper,
                    style = MaterialTheme.typography.bodySmall,
                    color = Glass.TextFaint,
                )
            }
        }
        Text(
            value,
            style = MaterialTheme.typography.titleSmall,
            color = accent,
        )
    }
}

/** 状态点+文字（紧凑） */
@Composable
fun StatusLine(
    dot: Color,
    label: String,
    modifier: Modifier = Modifier,
    labelColor: Color = Glass.TextDim,
) {
    Row(modifier = modifier, verticalAlignment = Alignment.CenterVertically) {
        Box(Modifier.size(8.dp).clip(CircleShape).background(dot))
        Spacer(Modifier.width(6.dp))
        Text(label, style = MaterialTheme.typography.bodySmall, color = labelColor)
    }
}

/** 胶囊：圆点 + 文案；tone 控制语义色（不是"装饰色"），不滥用强调色。 */
enum class PillTone { Neutral, Positive, Negative, Info, Pending }

@Composable
fun Pill(
    label: String,
    modifier: Modifier = Modifier,
    tone: PillTone = PillTone.Neutral,
    showDot: Boolean = true,
) {
    val fg: Color
    val dotColor: Color
    val bg: Color
    when (tone) {
        PillTone.Positive -> { fg = Glass.Green;   dotColor = Glass.Green;   bg = Glass.Green.copy(alpha = 0.12f) }
        PillTone.Negative -> { fg = Glass.Red;     dotColor = Glass.Red;     bg = Glass.Red.copy(alpha = 0.12f) }
        PillTone.Info     -> { fg = Glass.Indigo;  dotColor = Glass.Indigo;  bg = Glass.Indigo.copy(alpha = 0.12f) }
        PillTone.Pending  -> { fg = Glass.Amber;   dotColor = Glass.Amber;   bg = Glass.Amber.copy(alpha = 0.14f) }
        PillTone.Neutral  -> { fg = Glass.TextDim; dotColor = Glass.TextDim; bg = Glass.Surface2 }
    }
    Row(
        modifier = modifier
            .clip(RoundedCornerShape(50))
            .background(bg)
            .padding(horizontal = 10.dp, vertical = 5.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        if (showDot) {
            Box(Modifier.size(6.dp).clip(CircleShape).background(dotColor))
            Spacer(Modifier.width(6.dp))
        }
        Text(
            label,
            style = MaterialTheme.typography.labelMedium,
            color = fg,
        )
    }
}

/** 主按钮：实心强调色 + 深色文字，48dp 高度（满足 M3 触摸目标） */
@Composable
fun PrimaryButton(
    text: String,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    enabled: Boolean = true,
    accent: Color = Glass.Teal,
    height: Dp = 48.dp,
) {
    Button(
        onClick = onClick,
        enabled = enabled,
        shape = RoundedCornerShape(12.dp),
        contentPadding = PaddingValues(horizontal = 20.dp, vertical = 0.dp),
        colors = ButtonDefaults.buttonColors(
            containerColor = accent,
            contentColor = Glass.OnAccent,
            disabledContainerColor = Glass.Surface2,
            disabledContentColor = Glass.TextFaint,
        ),
        modifier = modifier.height(height),
    ) {
        Text(text, style = MaterialTheme.typography.titleSmall)
    }
}

/** 次按钮：描边 + 半透明底，48dp 高度 */
@Composable
fun GhostButton(
    text: String,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    enabled: Boolean = true,
    accent: Color = Glass.Teal,
    height: Dp = 48.dp,
) {
    OutlinedButton(
        onClick = onClick,
        enabled = enabled,
        shape = RoundedCornerShape(12.dp),
        contentPadding = PaddingValues(horizontal = 20.dp, vertical = 0.dp),
        colors = ButtonDefaults.outlinedButtonColors(
            containerColor = accent.copy(alpha = 0.08f),
            contentColor = accent,
            disabledContainerColor = Glass.Surface2,
            disabledContentColor = Glass.TextFaint,
        ),
        border = BorderStroke(1.dp, accent.copy(alpha = 0.4f)),
        modifier = modifier.height(height),
    ) {
        Text(text, style = MaterialTheme.typography.titleSmall)
    }
}

/** 圆形按钮：48dp（M3 最小触摸目标） */
@Composable
fun CircleIconButton(
    icon: ImageVector,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    accent: Color = Glass.Teal,
    contentDescription: String? = null,
    enabled: Boolean = true,
) {
    IconButton(
        onClick = onClick,
        enabled = enabled,
        modifier = modifier
            .size(48.dp)
            .clip(CircleShape)
            .background(accent.copy(alpha = if (enabled) 0.12f else 0.04f))
            .border(1.dp, accent.copy(alpha = if (enabled) 0.4f else 0.1f), CircleShape),
    ) {
        Icon(
            icon,
            contentDescription = contentDescription,
            tint = if (enabled) accent else Glass.TextFaint,
            modifier = Modifier.size(22.dp),
        )
    }
}

/** 可选中的列表行（圆角行 + 左侧 accent 条） */
@Composable
fun ListRow(
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    selected: Boolean = false,
    accent: Color = Glass.Teal,
    content: @Composable () -> Unit,
) {
    val bg = if (selected) accent.copy(alpha = 0.10f) else Glass.Surface0
    val border = if (selected) accent.copy(alpha = 0.45f) else Glass.Stroke.copy(alpha = 0.5f)
    Row(
        modifier = modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(10.dp))
            .background(bg)
            .border(1.dp, border, RoundedCornerShape(10.dp))
            .clickable(onClick = onClick)
            .padding(horizontal = 14.dp, vertical = 12.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) { content() }
}

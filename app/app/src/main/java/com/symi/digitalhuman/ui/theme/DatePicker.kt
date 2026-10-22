package com.symi.digitalhuman.ui.theme

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.CalendarMonth
import androidx.compose.material3.DatePicker
import androidx.compose.material3.DatePickerDefaults
import androidx.compose.material3.DatePickerDialog
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.SelectableDates
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.rememberDatePickerState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.unit.dp
import java.text.SimpleDateFormat
import java.util.Calendar
import java.util.Date
import java.util.Locale

private val isoDay = SimpleDateFormat("yyyy-MM-dd", Locale.US)
private val cnDay = SimpleDateFormat("yyyy年M月d日 EEEE", Locale.CHINA)

/** "2026-09-03" → 2026年9月3日 星期四 */
fun formatCnDate(date: String): String =
    runCatching { cnDay.format(isoDay.parse(date) ?: Date()) }.getOrDefault(date)

fun todayIso(): String = isoDay.format(Date())

fun parseIsoMillis(date: String): Long? = runCatching { isoDay.parse(date)?.time }.getOrNull()

/**
 * 统一日期选择入口（日报页 / 录音回放共用）。
 *
 * 用官方 Material3 DatePicker（Apache 2.0，可商用、无障碍已内建、跟随 M3 主题），
 * 不再自己拼横向胶囊：自造的胶囊选择器只能左右滑，跨月就没法跳，
 * 而且没有「哪天有数据」的可见提示。这里两个都补上：
 *  - 日历里**只有有数据的日期可选**，其它日期置灰（[onlyDates]）；
 *  - 展示当前选中日期 + 打开日历，跨月跳转交给系统组件。
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun DateField(
    currentDate: String,
    availableDates: Set<String>,
    onSelect: (String) -> Unit,
    modifier: Modifier = Modifier,
    hint: String? = null,
) {
    var open by remember { mutableStateOf(false) }
    val shape = RoundedCornerShape(12.dp)
    Row(
        modifier = modifier
            .fillMaxWidth()
            .clip(shape)
            .background(Glass.Surface2)
            .border(1.dp, Glass.Stroke, shape)
            .clickable { open = true }
            .padding(horizontal = 14.dp, vertical = 12.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Icon(
            Icons.Filled.CalendarMonth,
            contentDescription = null,
            tint = Glass.Teal,
            modifier = Modifier.size(20.dp),
        )
        Spacer(Modifier.width(10.dp))
        Row(Modifier.weight(1f), verticalAlignment = Alignment.CenterVertically) {
            Text(
                formatCnDate(currentDate),
                style = MaterialTheme.typography.titleSmall,
                color = Glass.Text,
            )
            if (!hint.isNullOrBlank()) {
                Spacer(Modifier.width(8.dp))
                Text(hint, style = MaterialTheme.typography.labelSmall, color = Glass.TextFaint)
            }
        }
        Text(
            "选择",
            style = MaterialTheme.typography.labelLarge,
            color = Glass.Teal,
        )
    }

    if (open) {
        AppDatePickerDialog(
            initialDate = currentDate,
            availableDates = availableDates,
            onConfirm = { onSelect(it); open = false },
            onDismiss = { open = false },
        )
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun AppDatePickerDialog(
    initialDate: String,
    availableDates: Set<String>,
    onConfirm: (String) -> Unit,
    onDismiss: () -> Unit,
) {
    // 只有有数据的日期可选。UTC 午夜对齐：DatePicker 用的是 UTC 时间戳，
    // 直接用本地时区解析会让选中的日期整体偏移一天（东八区尤其明显）。
    val allowedUtcMidnights = remember(availableDates) {
        availableDates.mapNotNull { toUtcMidnightOrNull(it) }.toSet()
    }
    val selectable = remember(allowedUtcMidnights) {
        object : SelectableDates {
            override fun isSelectableDate(utcTimeMillis: Long): Boolean =
                allowedUtcMidnights.isEmpty() || utcTimeMillis in allowedUtcMidnights

            override fun isSelectableYear(year: Int): Boolean = true
        }
    }
    val state = rememberDatePickerState(
        initialSelectedDateMillis = parseIsoMillis(initialDate)?.let { toUtcMidnight(it) },
        selectableDates = selectable,
    )
    DatePickerDialog(
        onDismissRequest = onDismiss,
        shape = RoundedCornerShape(20.dp),
        colors = DatePickerDefaults.colors(containerColor = Glass.Surface1),
        confirmButton = {
            TextButton(
                onClick = {
                    state.selectedDateMillis?.let { onConfirm(isoFromUtcMidnight(it)) }
                        ?: onDismiss()
                },
            ) { Text("确定", color = Glass.Teal) }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) { Text("取消", color = Glass.TextDim) }
        },
    ) {
        DatePicker(state = state, colors = pickerColors())
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun pickerColors() = DatePickerDefaults.colors(
    containerColor = Glass.Surface1,
    titleContentColor = Glass.Text,
    headlineContentColor = Glass.Text,
    weekdayContentColor = Glass.TextDim,
    subheadContentColor = Glass.TextDim,
    yearContentColor = Glass.TextDim,
    currentYearContentColor = Glass.Text,
    selectedYearContentColor = Glass.OnAccent,
    selectedYearContainerColor = Glass.Teal,
    dayContentColor = Glass.Text,
    disabledDayContentColor = Glass.TextFaint,
    selectedDayContentColor = Glass.OnAccent,
    selectedDayContainerColor = Glass.Teal,
    todayContentColor = Glass.Teal,
    todayDateBorderColor = Glass.Teal,
    navigationContentColor = Glass.Text,
)

private fun toUtcMidnightOrNull(date: String): Long? =
    parseIsoMillis(date)?.let { toUtcMidnight(it) }

/** 本地日期毫秒 → 该日期对应的 UTC 午夜毫秒（DatePicker 内部按 UTC 记日期） */
private fun toUtcMidnight(localMillis: Long): Long {
    val c = Calendar.getInstance().apply { timeInMillis = localMillis }
    c.set(Calendar.HOUR_OF_DAY, 0)
    c.set(Calendar.MINUTE, 0)
    c.set(Calendar.SECOND, 0)
    c.set(Calendar.MILLISECOND, 0)
    return c.timeInMillis - c.timeZone.getOffset(c.timeInMillis)
}

private fun isoFromUtcMidnight(utcMillis: Long): String = isoDay.format(Date(utcMillis))

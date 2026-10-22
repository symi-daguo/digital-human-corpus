package com.symi.digitalhuman.ui.root

import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.BarChart
import androidx.compose.material.icons.filled.Description
import androidx.compose.material.icons.filled.GraphicEq
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.NavigationBarItemDefaults
import androidx.compose.material3.NavigationRail
import androidx.compose.material3.NavigationRailItem
import androidx.compose.material3.NavigationRailItemDefaults
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.windowsizeclass.ExperimentalMaterial3WindowSizeClassApi
import androidx.compose.material3.windowsizeclass.WindowSizeClass
import androidx.compose.material3.windowsizeclass.calculateWindowSizeClass
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalContext
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.compose.rememberNavController
import com.symi.digitalhuman.ui.MainActivity
import com.symi.digitalhuman.ui.pages.ReportPage
import com.symi.digitalhuman.ui.pages.SettingsPage
import com.symi.digitalhuman.ui.pages.StatsPage
import com.symi.digitalhuman.ui.pages.StatusPage
import com.symi.digitalhuman.ui.theme.Glass

sealed class Dest(
    val route: String,
    val label: String,
    val icon: ImageVector,
) {
    data object Status   : Dest("status",   "状态", Icons.Filled.GraphicEq)
    data object Stats    : Dest("stats",    "统计", Icons.Filled.BarChart)
    data object Report   : Dest("report",   "日报", Icons.Filled.Description)
    data object Settings : Dest("settings", "设置", Icons.Filled.Settings)
}

@OptIn(ExperimentalMaterial3WindowSizeClassApi::class)
@Composable
fun AppRoot() {
    val windowSizeClass: WindowSizeClass = calculateWindowSizeClass(LocalContext.current as MainActivity)
    val nav = rememberNavController()
    val backStack by nav.currentBackStackEntryAsState()
    val current = backStack?.destination?.route ?: Dest.Status.route
    val destinations = listOf(Dest.Status, Dest.Stats, Dest.Report, Dest.Settings)
    val useRail = useNavRail(windowSizeClass)

    val onNavigate: (Dest) -> Unit = { dest ->
        nav.navigate(dest.route) {
            popUpTo(Dest.Status.route) { saveState = true }
            launchSingleTop = true
            restoreState = true
        }
    }

    Scaffold(
        containerColor = Glass.Bg,
        contentColor = Glass.Text,
        bottomBar = {
            if (!useRail) {
                NavigationBar(
                    containerColor = Glass.Surface0,
                    contentColor = Glass.Text,
                ) {
                    destinations.forEach { dest ->
                        val selected = current == dest.route
                        NavigationBarItem(
                            selected = selected,
                            onClick = { onNavigate(dest) },
                            icon = { Icon(dest.icon, contentDescription = null) },
                            label = {
                                Text(
                                    dest.label,
                                    style = MaterialTheme.typography.labelMedium,
                                )
                            },
                            alwaysShowLabel = true,
                            colors = NavigationBarItemDefaults.colors(
                                selectedIconColor = Glass.Teal,
                                selectedTextColor = Glass.Teal,
                                indicatorColor = Glass.Teal.copy(alpha = 0.14f),
                                unselectedIconColor = Glass.TextDim,
                                unselectedTextColor = Glass.TextDim,
                            ),
                        )
                    }
                }
            }
        },
    ) { padding ->
        Row(modifier = Modifier.fillMaxSize().padding(padding)) {
            if (useRail) {
                NavigationRail(
                    containerColor = Glass.Surface0,
                    contentColor = Glass.Text,
                    header = {
                        Text(
                            "语料采集",
                            color = Glass.Teal,
                            style = MaterialTheme.typography.labelLarge,
                        )
                    },
                ) {
                    destinations.forEach { dest ->
                        val selected = current == dest.route
                        NavigationRailItem(
                            selected = selected,
                            onClick = { onNavigate(dest) },
                            icon = { Icon(dest.icon, contentDescription = dest.label) },
                            label = { Text(dest.label, style = MaterialTheme.typography.labelMedium) },
                            colors = NavigationRailItemDefaults.colors(
                                selectedIconColor = Glass.Teal,
                                selectedTextColor = Glass.Teal,
                                indicatorColor = Glass.Teal.copy(alpha = 0.14f),
                                unselectedIconColor = Glass.TextDim,
                                unselectedTextColor = Glass.TextDim,
                            ),
                        )
                    }
                }
            }

            AdaptiveContent(windowSizeClass = windowSizeClass) {
                NavHost(
                    navController = nav,
                    startDestination = Dest.Status.route,
                    modifier = Modifier.fillMaxSize(),
                ) {
                    composable(Dest.Status.route)   { StatusPage(windowSizeClass) }
                    composable(Dest.Stats.route)    { StatsPage(windowSizeClass) }
                    composable(Dest.Report.route)   { ReportPage(windowSizeClass) }
                    composable(Dest.Settings.route) { SettingsPage(windowSizeClass) }
                }
            }
        }
    }
}

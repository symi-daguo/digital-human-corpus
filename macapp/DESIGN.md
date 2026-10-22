# 数字人服务 Mac 客户端设计规范

> 配色单一事实源：手机端 `app/.../ui/theme/Theme.kt`（Glass object）。
> 本文档是 Mac 壳（macapp/）与其保持统一的落地规范。
> 层次手法参考 Raycast for macOS（玻璃质感桌面标杆）。

## 1. 设计原则

1. **同一套 token，两端一致**：手机 APK 与 Mac 壳共用同一份色板、圆角阶梯与间距网格，用户在两端看到的是同一个产品。
2. **层次靠色阶，不靠描边堆叠**：卡片用 Surface0→Surface1 的 tonal elevation 区分层级，描边只做 1px 的弱分隔（Raycast 双环手法：外环弱描边 + 内侧 1px 顶部高光）。
3. **控件原生，容器自有**：按钮、TextField、ProgressView、菜单保持 macOS 原生（可用性、VoiceOver、字号缩放不破坏）；只对背景、卡片、文字、强调色做品牌化。
4. **亮暗双主题**：默认跟随系统，可在工具条手动指定浅色/深色；所有品牌色在两种模式下分别定义（不是简单 opacity 变换）。

## 2. 色板（Glass token）

深色列与 APK 完全一致；亮色列按同色相推导（饱和加深保证对比度 ≥ 4.5:1）。

| Token | 深色（APK 原值） | 亮色（推导） | 用途 |
|---|---|---|---|
| Bg | `#0B0F1A` | `#F5F7FB` | 详情区背景 |
| Surface0 | `#0F1424` | `#FFFFFF` | 卡片底层 |
| Surface1 | `#161D33` | `#FFFFFF` + 投影 | 悬浮卡/当前高亮卡 |
| Stroke | `#1F2A47` | `#E3E8F2` | 卡片 1px 描边 |
| TextPrimary | `#ECF1FA` | `#1A2233` | 标题、数值 |
| TextSecondary | `#95A2BD` | `#5A6B85` | 说明文字 |
| TextTertiary | `#5C6A87` | `#8C99B0` | 弱化提示 |
| Teal（主强调） | `#5EEAD4` | `#0D9488` | 主操作、运行态 |
| Indigo | `#818CF8` | `#6366F1` | 次强调、图表 |
| Amber | `#FBBF24` | `#D97706` | 警告、启动中 |
| Red | `#F87171` | `#DC2626` | 错误、危险操作 |
| Green | `#4ADE80` | `#16A34A` | 成功、完成 |
| OnAccent | `#06211C` | `#FFFFFF` | 强调色上的文字 |

## 3. 圆角与间距

- **圆角阶梯**（与 APK 一致）：`6 / 10 / 14 / 20 / 28`
  - 6：行内小元素、选中行底色
  - 10：内嵌小组件、租户卡片
  - 14：标准卡片（GlassCard 默认）
  - 20：大区块容器（概览 hero）
  - 28：仅手机端 FAB 等特殊元素
- **间距网格（8dp 基）**：`4 / 8 / 12 / 16 / 20 / 24`
  - 卡片内边距 14（视觉修正值，介于 12/16 之间）
  - 卡片间 16，页面水平边距 20
- **页面容器**（`Theme.pageH / pageTop / pageBottom`，v1.5.2 起 7 个页面共用）：
  水平 20 · 底部 20 · **顶部 0**。
  - 顶部为什么是 0：详情区内容从内容区顶端起排，而侧栏首行是从同一个位置开始的，
    再叠任何顶部内边距都会让右边整块往下掉（用户反馈「下面空了一个高度、没有对齐
    左边的菜单栏」）。实测：内容区顶端 = 侧栏首项顶边，所以顶部留 0 才齐。
  - 例外 1：`设置` 页走系统 `Form(.grouped)`，内边距由系统给，不套这套。
  - 例外 2：自带固定头的页（日报 / 日志）只对齐顶部这一个值，其余由头部自己的内边距管。
- **工具栏**：窗口用 `toolbarStyle = .unified`（见 `AppDelegate.makeMainWindow`）。
  macOS 26 默认的浮动工具栏会在自身下方**再拖一条材质带**盖住详情区首卡片，
  并逼出「内容整体下移 40pt」这种必然错位的补丁；unified 让工具栏占独立高度，
  遮挡与错位一起消失。
  工具栏里的**切换器必须用纯 `Text` 选项**（或 `Menu`）：带图标的 `Label` 会被压成
  iconOnly，空间选择器只剩一个空胶囊、主题选择器只剩一个月亮
  （用户反馈「切换空间和主题的文字都没有显示」）。
- **枚举控件（choice / select）一律用 `Menu`，不用 `Picker`**（v1.5.2，实测）：
  `Picker("") + .labelsHidden() + .pickerStyle(.menu)` 在 macOS 27 的表单里
  渲染成**只有一对升降箭头、没有选中项文字**，用户看到的是「右边一个空圈」。
  `Menu` 的 label 是纯 `Text`，显示当前项的中文名，当前项在菜单里打勾。
- **工具栏两个切换器的指示器不同，属预期**（2026-09-16 实测截图）：
  `空间` 用原生 `Picker(.menu)`，指示器是 macOS 弹窗按钮固有的 **⇅**；
  `主题` 用 `Menu`，指示器是 **⌄**。`主题` 之所以必须换 `Menu`，是因为它要在
  菜单里给当前项打勾（`Picker` 在工具栏会连标题一起丢）；`空间` 不需要打勾，
  保留原生控件即可。两个都能正常展开，**别为了「看起来统一」把 `空间` 改成 `Menu`**。
  同理，工具栏最右那颗「带斜杠的圆圈」是 `safari` 图标（罗盘）在小尺寸下的观感，
  不是缺失符号占位。
- **侧栏图标恢复彩色**（macOS 27 Golden Gate）：边栏延伸到窗口边缘之外，
  图标也重新上色。有明确语义的入口给语义色（概览 Teal / 日报 Indigo /
  行业词表 Amber / 角色 Green / 空间管理 Indigo），纯粹的系统工具
  （日志、设置）保持 `textSecondary` 中性 —— 一列彩色里留两处安静的位置，
  扫视更快。**只给图标上色**，标题走系统默认色；整行套 `foregroundStyle`
  会把选中行的白字染色，在蓝色高亮底上直接糊掉。
- **详情区背景用系统 Material**（v1.5.2）：Golden Gate 给 Liquid Glass 加了
  系统级透明度滑块（超透明 ←→ 不透明），深色模式还会自动降透明度。滑块作用的
  就是窗口底材——这里若铺一块不透明的品牌底色等于把滑块整个吃掉，用户把系统
  调成「更透明」时本窗口纹丝不动。做法是 `.regularMaterial` + 45% 的品牌色叠加：
  冷调还在，其余透明度交还系统。

## 4. 层次手法（Raycast 规范摘编）

1. **双环容器**：外环 1px Stroke 弱描边 + 内侧顶部 1px 高光（深色 `#FFFFFF 6%`，亮色 `#FFFFFF 80%`），模拟顶光打在玻璃上的效果。
2. **tonal elevation**：背景 Bg → 卡片 Surface0（亮色下白卡 + 0.06 黑投影）→ 高亮卡 Surface1，靠明度差分层，不加大描边。
3. **hover 过渡用 opacity**：不做位移/缩放，0.15s ease。
4. **强调色克制**：一屏最多一个 Teal 主操作；指标卡的 tint 只用于 11pt 的 Label 图标行，不铺大色块。

## 5. 字号阶梯

沿用 macOS 原生文本风格 + APK M3 Typography 五档映射：

| 用途 | 字号 | 字重 |
|---|---|---|
| 页面大标题 | 15 | semibold |
| 卡片标题 | 12 | semibold |
| 正文/说明 | 11.5 | regular |
| 辅助文字 | 10.5 | regular |
| 指标数值 | 16 | semibold（等宽数字） |

## 6. 组件清单（Theme.swift）

| 组件 | 说明 |
|---|---|
| `ThemeManager` | 单例 ObservableObject；mode = system/light/dark，UserDefaults 持久化，NSApp.appearance 应用 |
| `Theme` (enum) | 全部 token 静态属性；颜色用 NSColor dynamicProvider 双注册，亮暗自动切换 |
| `GlassCard` | 标准卡片：Surface0 底 + 1px Stroke + 顶部高光 + 圆角 14；可选标题行（icon + title） |
| `SectionHeader` | 12pt semibold 卡片标题行 |
| `Theme` 布局 token | `pageH / pageTop / pageBottom` 页面容器三向边距；`space1…space6` 8dp 间距网格；`radiusS/M/L/XL` 圆角阶梯；`textPrimary/Secondary/Tertiary` 文字三级 |

## 7. 不做的事

- 不改按钮/输入框等原生控件的样式（`borderedProminent` 用系统 accent 即可）。
- 不做自定义 blur 层：详情区直接铺 `.regularMaterial`（系统材质，含 Liquid Glass
  的透明度行为），卡片仍是实色玻璃 —— 性能与可读性优先，别自己糊一层。
- 不引入第三方依赖。

### 7.1 本轮对 Golden Gate 三条要项的取舍（2026-09-16）

| 要项 | 是否落地 | 依据 |
|---|---|---|
| 边栏延伸至窗口边缘 | 系统行为，自动获得 | 标准 `NavigationSplitView` + `List(.sidebar)`，未做任何自定义 |
| 边栏图标恢复彩色 | ✅ 已落地 | 见 §3 —— 这是 Golden Gate 少见的「要求 app 主动表达」的一条 |
| 工具栏更整齐、减少菜单图标 | ✅ 早已符合 | 切换器用纯 `Text`、菜单只保留选中勾，无装饰性图标 |
| 所有窗口统一更小圆角 | 不落地 | 讲的是**系统窗口外框**圆角，由系统画，我们改不了也不必改；卡片圆角 14 是内部元素，牵动 APK 同一套阶梯（两端色板/圆角是单一事实源），不动 |
| Liquid Glass 透明度滑块 | ✅ 已验证响应 | 见 §3 的详情区 Material 一条 |

**Android 端不照搬这套**：边栏彩色、edge-to-edge 是 macOS 窗口结构的产物，
Android 侧继续走 Material 3（NavigationBar 自带选中/未选中规范）。
两端共享的只有色板、圆角阶梯、间距网格与文案口径 —— 不是布局结构。

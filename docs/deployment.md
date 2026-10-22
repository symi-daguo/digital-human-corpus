# Windows 部署与使用手册（数字人服务 · v1.5.17）

> 面向：在 Windows 电脑上执行部署的人，或替他执行的智能体。
> 对应仓库版本：`1.5.6`（2026-09-16）。
> 用法：从上往下依次执行。每步都给了【命令】和【判据】。**判据不通过就不要往下走** ——
> 后面每一步都依赖前一步的结果，硬推只会得到「构建全绿但装上去不能用」。

---

## 零、先弄清楚这套东西有哪些件

| 件 | 是什么 | 关键事实 |
| --- | --- | --- |
| 服务端 | Python（FastAPI），监听 `0.0.0.0:8766`（TLS）+ `127.0.0.1:8767`（明文，仅本机） | **对外只走 HTTPS**，证书首次启动自动生成，不用装 openssl |
| 界面 | 工作台页面（服务端渲染） | 壳窗口内是 `http://127.0.0.1:8767/console`；浏览器/手机/局域网是 `https://…:8766/console` |
| 桌面壳 | Tauri 应用「数字人服务」 | 五件事：拉起服务、探活、**在窗口内显示工作台**、托盘常驻、开机自启动。**关窗口不退服务**，退出走托盘 |
| 数据 | 全部在 `%APPDATA%\DigitalHuman` | 升级时整包替换安装目录，不会带走数据 |
| 手机端 | Android 目标设备盒子 | 通过局域网连到本机 8766 上传语料 |

要做的事分两件，本手册都覆盖：

| 任务 | 什么时候做 | 需要什么 |
| --- | --- | --- |
| **A. 构建安装包** | 手上只有源码，要出一个 exe 给客户 | 见 §二 的前置环境 |
| **B. 安装并启动使用** | 拿到 exe 之后 | 只要 exe，**不需要装 Python** |

---

## 一、前置环境（只做任务 A 才需要）

安装包**自带 Python 运行时和全部依赖**，客户机器上不需要装 Python、也不需要有 pip。
但**构建机**需要下面这些：

| 依赖 | 版本 | 怎么装 | 为什么 |
| --- | --- | --- | --- |
| Windows | 10 / 11，x64 | — | 只出 x64 包 |
| Git for Windows | 任意近期版 | https://git-scm.com/download/win | 预置脚本是 bash，靠它带的 Git Bash 跑 |
| Node.js | 20 以上（实测 22.x） | https://nodejs.org/ 或 winget | Tauri CLI 与 `npm ci` |
| Rust stable | 用 rustup 装 | https://rustup.rs/ | 编译桌面壳 |
| VS Build Tools | 「使用 C++ 的桌面开发」工作负载 | https://visualstudio.microsoft.com/visual-cpp-build-tools/ | Rust 在 Windows 上的链接器与 Windows SDK |
| WebView2 Runtime | Evergreen | Win11 自带；Win10 若缺再从微软下载 | Tauri 的界面内核 |
| 磁盘 | 留 3 GB 以上 | — | 内嵌运行时 178 MB + Rust 构建目录约 2 GB |
| 网络 | 能到 github.com / crates.io / registry.npmjs.org | — | 下载运行时与依赖 |

一条命令验证（PowerShell）：

```powershell
git --version
node --version
rustc --version
cargo --version
```

**判据**：四条都打印出版本号，`node` 不低于 `v20`。
任一缺失就先把环境补齐 —— Tauri 报的错（`could not determine executable to run`、`linker not found`）
全都是这两样缺了之后的连锁反应，不要在构建阶段去猜。

---

## 二、任务 A：构建安装包

### 2.1 取源码

```powershell
git clone https://github.com/symi-daguo/digital-human.git
cd digital-human
type VERSION
```

**判据**：输出 `1.5.6`。版本号对不上说明拉到了别的分支，先对齐再继续。

### 2.2 预置内嵌 Python 运行时（最关键的一步）

```bash
bash desktop/Scripts/prepare-python-win.sh
```

在 **Git Bash** 里执行（PowerShell 里用 `bash` 调它也行，前提是 PATH 里有 Git 的 bash）。

这一步会：下载 python-build-standalone 3.13.7（x86_64-pc-windows-msvc）→ 解到
`desktop/src-tauri/.runtime/python/` → 用它自带的 pip 从阿里云镜像装 `server/requirements.txt`。

> **不需要 uv、不需要预装 Python。** Windows 这条路径用内嵌解释器自己的 pip 直接装；
> 只有 macOS/Linux 上交叉预装时才需要 uv（那种场景下 pip 会报 `resolution-too-deep`）。

脚本自己会打印判据，逐条对上才算过：

| 输出 | 期望 |
| --- | --- |
| `依赖条目：` | `46` 左右 |
| `运行时体积：` | `178M` 左右 |
| `关键依赖齐备：fastapi / uvicorn / sherpa_onnx / numpy / cryptography / pydantic` | 必须出现 |
| `⚠️ 意外装上了 mlx_lm` | **必须不出现**（mlx 只支持 Apple Silicon） |

**不通过怎么办**：脚本以 `exit 1` 中止的话，先看它停在哪一步 ——
第 1 步失败是网络到不了 GitHub，第 3 步失败多半是 pip 源或 `requirements.txt` 第 1 行的编码声明被删了
（删掉那行会让 Windows 上一个依赖都装不上，报 `UnicodeDecodeError: 'charmap' codec ...`）。

### 2.3 打包

```powershell
cd desktop
npm ci
npx tauri build --bundles nsis
```

| 项 | 值 |
| --- | --- |
| 耗时 | 首次 10 ~ 20 分钟（Rust 全量编译 + LZMA 压缩约 1 分 43 秒） |
| 产物 | `desktop\src-tauri\target\release\bundle\nsis\数字人服务_<版本>_x64-setup.exe`<br>（`<版本>` 取自仓库根 `VERSION`，当前为 **1.5.17**） |

**判据**：该路径下存在一个 `*_x64-setup.exe`。

> `--bundles nsis` 不能省。`tauri.conf.json` 里的 `bundle.targets` 是跨平台共享的，
> 不显式指定的话会去尝试打 dmg / deb，直接报「不支持的目标」。

### 2.4 产物必检（两道卡口，缺一项都别发出去）

**第一道：体积。** 带运行时的安装包约 **41 MB**；一旦运行时映射失效会掉到 **5 MB** 量级，
而构建依旧全绿。用 25 MB（26214400 B）卡住，中间没有灰区。

**第二道：内容。** 体积对不等于内容对。用 7-Zip 列包内清单断言。

把下面整段粘进 PowerShell 执行（$exe 路径按实际改）：

```powershell
$exe = "desktop\src-tauri\target\release\bundle\nsis\数字人服务_<版本>_x64-setup.exe"
$len = (Get-Item $exe).Length
Write-Host ("安装包 {0:N0} 字节 = {1:N1} MB" -f $len, ($len / 1MB))
if ($len -lt 26214400) { Write-Error "体积不达标，疑似没带内嵌 Python 运行时，停止" }

$7z = "$env:ProgramFiles\7-Zip\7z.exe"
if (-not (Test-Path $7z)) { $7z = (Get-Command 7z -ErrorAction SilentlyContinue).Source }
if (-not $7z) { Write-Warning "本机没有 7-Zip，装一个再跑内容自检（不要跳过）" }
else {
  # 必须把反斜杠归一化：Windows 的 7z 输出 python\python.exe，
  # 不归一化会「全部命中 0」，把好好的包判成缺运行时
  $list = (& $7z l $exe) -replace '\\', '/'
  $nExe = @($list | Select-String -SimpleMatch 'python.exe').Count
  $nFas = @($list | Select-String -SimpleMatch 'site-packages/fastapi').Count
  $nShp = @($list | Select-String -SimpleMatch 'site-packages/sherpa_onnx').Count
  $nMlx = @($list | Select-String -SimpleMatch 'mlx_lm').Count
  Write-Host "包内命中：python.exe=$nExe  fastapi=$nFas  sherpa_onnx=$nShp  mlx_lm=$nMlx（mlx 必须为 0）"
  if ($nExe -lt 1 -or $nFas -lt 1 -or $nShp -lt 1) { Write-Error "包里缺内嵌 Python 运行时或关键依赖，停止" }
  if ($nMlx -ne 0) { Write-Error "包里混进了 mlx，requirements 的平台标记可能失效，停止" }
}
```

**判据**：体积 ≥ 25 MB；`python.exe / fastapi / sherpa_onnx` 三项都 ≥ 1；`mlx_lm` 为 `0`。

> 若三项全是 `0`：那是**匹配失败**（路径分隔符或 7-Zip 版本差异），不是内容缺失 ——
> 真缺内容只会哪一项缺哪一项是 0。

### 2.5 品牌外观必检（v1.5.5 起）

**为什么单列一节**：v1.5.4 及以前，安装器那一层是 **NSIS 自带图标 + MUI 那张蓝色「电脑」欢迎图**。
主程序图标明明是我们的，但用户拿到手先看到的是**下载下来的那个 setup.exe**，所以照样会问
「你的 logo 在哪里」。根因是 Tauri 的 `bundle.windows.nsis` 里
`installerIcon` / `uninstallerIcon` / `headerImage` / `sidebarImage` **默认值都是 `None`**
（详见 `pitfalls.md` P-53）。现已显式指定，下面两条判据把它钉住。

**判据一（必做，肉眼）**：

1. 双击 `数字人服务_<版本>_x64-setup.exe`：向导左侧应是**深蓝底 + 我们的发光头像徽标**，
   下面依次是「数字人服务」「语料采集与处理」，底部「Windows 版」。
   **看到蓝色「电脑」图案就是掉了** —— 那是 MUI 的 `modern-wizard.bmp`。
2. 在资源管理器里看 setup.exe 本身：图标应该是**同一个发光头像**
   （不是那个通用的「安装箱」图标）。

**判据二（可选，抽包比 sha256）**：把向导位图抽出来和仓库源图逐字节比。接着 2.4 的变量（`$exe`、`$7z`）执行：

```powershell
$tmp = Join-Path $env:TEMP "dh-wizard"
Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $tmp | Out-Null

# 用 7z 自己列出的路径去抽，不猜分隔符（win 版 7z 列的是反斜杠，mac 版是正斜杠）
$paths = @(@(& $7z l -slt $exe) | Select-String -Pattern '^Path = .*modern-(wizard|header)\.bmp$' |
           ForEach-Object { $_.Line.Substring(7).Trim() })
Write-Host "包内向导位图 $($paths.Count) 张：$($paths -join '  ')"
if ($paths.Count -ne 2) { Write-Error "期望 2 张，实际 $($paths.Count) 张 —— sidebarImage/headerImage 掉回默认了"; exit 1 }

& $7z e -y "-o$tmp" $exe @paths | Out-Null
$pairs = @{ "modern-wizard.bmp" = "nsis-sidebar.bmp"; "modern-header.bmp" = "nsis-header.bmp" }
foreach ($k in $pairs.Keys) {
  $got  = Join-Path $tmp $k
  $want = Join-Path "desktop\src-tauri\icons" $pairs[$k]
  if (-not (Test-Path $got)) { Write-Error "没抽到 $k，停止"; exit 1 }
  $h1 = (Get-FileHash $got).Hash
  $h2 = (Get-FileHash $want).Hash
  Write-Host ("{0}  {1}  vs  {2}  {3}" -f $k, $h1.Substring(0,16), $pairs[$k], $h2.Substring(0,16))
  if ($h1 -ne $h2) { Write-Error "$k 与仓库源图不一致，停止"; exit 1 }
}
```

> ⚠️ **别拿 `7z l` 单条的 `Size` 当判据**：NSIS 的条目在 solid 块里，实测 `modern-header.bmp`
> 那行声明 31454、实际只有 25818。要比就比 sha256（或 `7z l` 末尾的 `Files / Size` 汇总）。
>
> 判据二在 macOS 上写不出来（本机没有 pwsh，无法先跑通），所以**它以判据一为准**，
> 机器断言只作加强；万一段落报错，按语义改一下，别跳过得过。

---

## 三、任务 B：安装

1. 把 `数字人服务_<版本>_x64-setup.exe` 拷到目标机器，双击。
2. 安装向导是**当前用户模式**，不会弹 UAC、**不需要管理员权限**；安装目录向导里会给，
   也可以自己改。
3. 装完开始菜单会出现「数字人服务」。

**判据**：开始菜单里能搜到「数字人服务」，且「设置 → 应用」里能看到它（可卸载）。

---

## 四、任务 B：启动与首次配置

### 4.1 启动

从开始菜单点「数字人服务」。会依次发生：

1. 壳找解释器：先看自己 resources 里的 `python\python.exe`（**内嵌的，一定有**），
   再退到系统的 `python3` / `python`；
2. 拉起服务端，数据目录建在 `%APPDATA%\DigitalHuman`；
3. 冷启动要加载语音模型，窗口会显示「正在启动本地服务…」「首次启动需加载语音模型，约十几秒…」；
4. 就绪后**窗口自己变成完整工作台**（和 mac 端看到的是同一套界面，不再是过渡页）。

**判据**：窗口里出现「数字人语料服务 · 多租户工作台」页面（标题栏仍是「数字人服务」）。

> **关窗口不等于退出。** 这是个常驻服务，点 X 之后窗口会**收进右下角托盘**继续跑，
> 服务不受影响。要真正退出，走托盘菜单的「退出」（它会先把服务进程带走）。

### 4.1.1 开机自启动（默认已开）

安装后**第一次启动**会把本程序加进系统启动项（当前用户的
`HKCU\Software\Microsoft\Windows\CurrentVersion\Run`，用户态写入，**不需要管理员权限**）。
之后每次登录 Windows，它会在右下角静默常驻、自动把服务拉起来，**不弹窗口**。

- 托盘菜单里有「开机自启动」勾选项，状态取自系统实际状态（不是壳自己记的变量）。
  在 Windows「任务管理器 → 启动应用」里手工禁用过，这里也会如实显示为未勾选。
- 取消勾选即关闭；再勾上即恢复。**默认值只在第一次启动给一次**，之后完全听用户的，
  不会出现「关掉了下次启动又被偷偷打开」。

> 想确认/清理：`任务管理器 → 启动应用` 里找「数字人服务」。
> 卸载后若该条目仍在（安装器的卸载脚本不负责清这一项），手工删掉即可：
> `reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v 数字人服务 /f`

**⚠️ 自启的唯一路径就是上面这一项（应用自己写 HKCU Run 键）。**
**不要**再手工往「启动」文件夹
（`shell:startup` → `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup`）
放一个自己的快捷方式 —— 那是第二条互不知情的拉起路径，会让登录时同时拉起两次。

> 为什么这条要写进手册：Mac 端 2026-09-18 实测出现过「菜单栏两个图标，
> 一个运行中、一个未启动」，根因正是**两套自启各拉一个**（系统「登录项」+
> 壳自己的 LaunchAgent），详情见 `docs/verification.md` §43。
>
> 从 **v1.5.13** 起，桌面壳已接入单实例保护（`tauri-plugin-single-instance`）：
> 即使真的被两条路径同时拉起，**第二个实例会立刻退出并把已有窗口唤到前台**，
> 不会出现两个托盘图标，也**不会**再 spawn 第二套 Python 子进程去抢 8766 端口。
> 所以手工多加速捷方式**不会**造成双实例，但仍建议只保留应用自己那一项，
> 免得将来排查自启问题时多一个干扰变量。

### 4.2 首次启动会弹 Windows 防火墙授权

服务要监听 8766 让手机连进来，所以会弹授权框（弹窗里可能只显示 `python.exe`）。

**只勾「专用网络」**，不要勾公用网络。

没弹窗、或者手滑点了取消、或者手机连不上时，用管理员身份的 PowerShell 补一条规则：

```powershell
netsh advfirewall firewall add rule name="DigitalHuman 8766" dir=in action=allow protocol=TCP localport=8766 profile=private
```

> 不授权的话本机浏览器访问仍然正常，但手机连不上。

### 4.3 打开工作台（窗口内直接显示，不再依赖浏览器）

**v1.5.6 起，工作台就在壳窗口里**：过渡页轮询到服务就绪后会自动在本窗口内导航到
工作台页面，看到的就是 mac 端那套完整界面。窗口关掉之后再打开，走**托盘菜单 →
「打开工作台」**。

这背后有个必须知道的细节 —— 服务端现在监听**两个**端口：

| 地址 | 协议 | 谁在用 |
| --- | --- | --- |
| `https://127.0.0.1:8766/console` | 自签 HTTPS | 系统浏览器、手机、局域网其它机器 |
| `http://127.0.0.1:8767/console` | **明文 HTTP，只绑 127.0.0.1** | 壳窗口自己 |

为什么壳不能直接用 8766：Tauri 用的 WebView（Windows 上是 WebView2）对
**不受信证书的处理就是「导航失败」**，而且没有浏览器那种「高级 → 继续访问」可点
—— 窗口只会白屏。所以服务端额外开了一个**只绑回环**的明文入口专门给壳用。
它物理上出不了本机，手机与局域网那两条链路仍走 8766 的 HTTPS，一行没改。

**判据**：窗口里出现「数字人语料服务 · 多租户工作台」；且
`Get-NetTCPConnection -LocalPort 8766,8767 -State Listen` 两条都在。

**降级路径**：万一 90 秒内回环入口没起来（例如服务是先被别的方式拉起来的、没带
`DH_DESKTOP_HTTP=1`），过渡页会自动退回「用系统浏览器打开」老路子：

- Chrome / Edge：「您的连接不是私密连接」→ 点「高级」→「继续访问 127.0.0.1（不安全）」
- Firefox：「警告：面临潜在的安全风险」→「高级」→「接受风险并继续」

手工在浏览器里打开也随时可以：

```
https://127.0.0.1:8766/console
```

> 浏览器里**不要**用 `http://127.0.0.1:8766` —— 那台恒定带 TLS，明文打过去是
> `Empty reply from server`，浏览器只会转圈然后报错。（`http://127.0.0.1:8767`
> 是明文，但它只绑回环、且是给壳用的，手工开也能用，只是没必要。）

### 4.4 工作台里先填「配对 Token」

工作台的接口都要 Bearer 鉴权。页面上的「安全」卡片里填 Token 后点保存：

| 项 | 值 |
| --- | --- |
| 配对 Token | **默认 `digital-human`；但这台机器若点过工作台的「更换 Token」，实际值形如 `dhm_xxxxxxxx`。** 以 `logs\api.log` 启动横幅里的「配对 Token」为准（§五 有取法）。必须与 `env.local` 里的 `DH_TOKEN` 逐字一致 |
| 保存位置 | 浏览器 localStorage（每台机器填一次） |

**判据**：填完点保存，页面由「先填配对 Token」的提示转为能拉到状态与日报。

Token 填错的表现是接口 401、页面提示「先填配对 Token」，不会报得更详细 —— 别去猜，
直接把横幅里那串复制粘贴。

想换 Token：页面「安全」卡里有「更换 Token」按钮，服务端生成强随机值并热生效；
**换完每台已配对的手机都要重填新 Token。**

### 4.5 配云端文字模型（Windows 上必做）

**Windows 包内没有 `mlx-lm`** —— 它依赖 Apple 的 Metal，装不上。所以文字模型必须走云端 API，
否则去口气、总结、日报都会是空的。

两种写法，任选一种。**推荐第一种**，它由服务端校验并立即生效，不用重启。

#### 写法一：调用配置接口（推荐）

```powershell
# 自签证书：只在本 PowerShell 会话内放行（不改系统设置）
[Net.ServicePointManager]::ServerCertificateValidationCallback = { $true }

$T = "digital-human"          # 换成你自己的配对 Token
$H = @{ Authorization = "Bearer $T" }

$body = @{ values = @{
  DH_LLM_PROVIDER = "cloud"
  DH_LLM_PROTOCOL = "openai"
  DH_LLM_BASE_URL = "https://api.deepseek.com/v1"
  DH_LLM_MODEL    = "deepseek-v4-flash"
  DH_LLM_API_KEY  = "sk-你的Key"
} } | ConvertTo-Json -Depth 4

Invoke-RestMethod -Method Put "https://127.0.0.1:8766/api/console/config" `
  -Headers $H -ContentType "application/json" -Body $body | ConvertTo-Json -Depth 4
```

**判据**：返回里 `ok` 为 `true`，`written` 列出刚写的几个键名。

写完立刻验一次真实连通（这一步是「地址 + 密钥 + 模型名」三者都对才算过）：

```powershell
$probe = @{ base_url = "https://api.deepseek.com/v1"; model = "deepseek-v4-flash"
            api_key = "sk-你的Key"; protocol = "openai"; provider = "cloud" } | ConvertTo-Json
Invoke-RestMethod -Method Post "https://127.0.0.1:8766/api/console/llm/test" `
  -Headers $H -ContentType "application/json" -Body $probe | ConvertTo-Json
```

**判据**：`ok` 为 `true`，并带 `latency_ms`。返回 `ok: false` 时 `message` 就是人话解释：

| message | 含义与处理 |
| --- | --- |
| `API Key 无效或已过期（401）` | Key 抄错或已吊销 |
| `Key 没有该模型的权限（403）` | 账号没开通这个模型 |
| `模型名「...」不存在（404）` | 模型 id 抄错，改成厂商控制台里的准确 id |
| `触发限流或余额不足（429）` | 余额或额度问题 |
| `云端接口缺少 API Key` | Key 字段是空的 |

#### 写法二：直接改配置文件

编辑 `%APPDATA%\DigitalHuman\env.local`，加上（该文件支持 `KEY=VALUE`，
不强制写 `export`；已有的注释不要删，排障时要用）：

```
DH_LLM_PROVIDER=cloud
DH_LLM_PROTOCOL=openai
DH_LLM_BASE_URL=https://api.deepseek.com/v1
DH_LLM_MODEL=deepseek-v4-flash
DH_LLM_API_KEY=sk-你的Key
```

**这种写法要重启服务才生效**（配置在进程启动时读入）：托盘右键「停止服务」→「启动服务」，
或直接退出后重新打开应用。

#### 厂商参数参考（接口协议一律选 OpenAI 兼容，除直连 Claude 官方）

| 厂商 | 接口地址 | 模型名示例 |
| --- | --- | --- |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-v4-flash` |
| 阿里云百炼（通义） | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen3.8-max` |
| 月之暗面 Kimi | `https://api.moonshot.cn/v1` | `kimi-k3` |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-5.3` |
| 火山方舟（豆包） | `https://ark.cn-beijing.volces.com/api/v3` | `doubao-seed-2.1-pro` |
| SiliconFlow | `https://api.siliconflow.cn/v1` | `Qwen/Qwen3-8B` |

模型名不确定就先用「拉取模型列表」接口：

```powershell
Invoke-RestMethod -Method Post "https://127.0.0.1:8766/api/console/llm/models" `
  -Headers $H -ContentType "application/json" `
  -Body (@{ base_url="https://api.deepseek.com/v1"; api_key="sk-你的Key" } | ConvertTo-Json)
```

> 境外厂商（OpenAI / Anthropic / Gemini）如果连不上，在「云端代理」里填本机代理，
> 形如 `http://127.0.0.1:7890`。国内厂商不需要。
>
> **语音识别（两级 ASR）全程本地运行，不需要联网、也不受这里影响。**

### 4.6 工作台有哪几页（v1.5.6 起与 mac 端功能对齐）

窗口里那个工作台是**服务端渲染**的（`server/app/console.py`），
mac、Windows、浏览器打开的是**同一份界面**，改一处三端同时生效。共 8 页：

| 页签 | 作用 |
| --- | --- |
| 概览 | 服务/模型/行业/安全四张卡；「更换 Token」也在这里 |
| 日报 | 按日期看 原始稿 / 干净稿 / 总结稿，并能播报 |
| 行业词表 | 切换行业、编辑术语与纠错表 |
| 角色 | 已注册的具名角色；**上传录音创建角色** |
| 设备与管家 | 每台设备用哪个空间、哪个管家、能不能用本机 Hermes |
| 空间管理 | 新建/注销租户空间（每部手机一个空间，数据目录级隔离） |
| **设置** | 50 个可配项（通用/模型与接口/转写/说话人/实时对话/高级）、厂商预设、测连通性、拉模型列表、立即备份 |
| **日志** | `api.log` / `llm.log` 尾部实时看，可自动刷新与复制 |

> 地址栏形式支持深链：`…/console#settings`、`#logs`、`#report` … 直接落到那一页。
>
> 「设置」写的是 `env.local`（与服务端、mac 应用共用同一份配置）；
> 标「需重启」的项保存后要在托盘菜单点「重启」才生效。

顶栏右侧还有两个按钮，三端（Windows 桌面壳 / mac 主窗口 / 浏览器）都一样：

| 按钮 | 作用 |
| --- | --- |
| **跟随系统 / 浅色 / 深色** | 点一下循环切换整套配色。选「跟随系统」时会跟系统的浅色/深色自动变；选「浅色」「深色」则固定住、不再随系统。选择记在本机浏览器里，各端互不影响 |
| **刷新** | 重新拉当前这一页的数据（窗口里没有菜单栏，也用不上浏览器那个 `⌘R`） |

> 换主题**只换颜色**，卡片宽度、间距、字号都不动 —— 窗口拉宽时卡片照旧跟着铺满。

### 4.7 托盘菜单

托盘图标在**右下角通知区域**（可能被折叠进「^」隐藏区，拖出来即可常驻显示）。
右键六个菜单项：

| 菜单项 | 作用 |
| --- | --- |
| 打开工作台 | 把壳窗口（内含完整工作台）显示到前台 |
| 开机自启动 | **勾选项**。勾上 = 登录时自动拉起；状态取自系统实际状态 |
| 启动服务 | 手动拉起（已在跑则忽略，不会起第二个抢端口） |
| 停止服务 | 停掉服务进程 |
| 显示数据目录 | 打开 `%APPDATA%\DigitalHuman` |
| 退出 | 停服务并退出（**这是唯一的退出方式**） |

> 若改了端口（`DH_PORT`），壳窗口加载的仍是回环入口（固定 8767，与服务端主端口无关），
> 因此**不受影响**；但手机/浏览器要填新端口。

> 通知区域看不到图标时：先确认进程还在（任务管理器「详细信息」里找
> `digital-human-desktop.exe`），再看是不是被 Windows 折叠进「^」隐藏区了。

---

## 五、手机端配对

服务端启动时会把配对需要的四项直接打进日志横幅。用 PowerShell 取出来：

```powershell
Get-Content "$env:APPDATA\DigitalHuman\logs\api.log" -Tail 40 |
  Select-String -Pattern "局域网地址|端口|配对 Token|证书指纹|工作台"
```

**判据**：能取到形如下面的一张表（`=` 与 `-` 是横幅边框；`<Windows主机IP>` 处实际输出的
是本机局域网 IP，各机器不同）：

```
==============================================================
  数字人语料服务已启动
--------------------------------------------------------------
  局域网地址： <Windows主机IP>, 127.0.0.1
  端口：       8766
  配对 Token： digital-human
  租户空间：   default
  工作台：     https://<Windows主机IP>:8766/console
  证书指纹：   xxxxxxxx（base64）
--------------------------------------------------------------
```

然后在手机上填（Android 端「设置 → Mac 服务连接」）：

| 手机端字段 | 填什么 |
| --- | --- |
| 地址 | 上面「局域网地址」里那个 `192.168.x.x`（不要填 127.0.0.1，那是本机） |
| 端口 | `8766` |
| 配对 Token | 与上面「配对 Token」逐字一致 |
| 证书指纹 | 点手机上的「获取证书指纹」按钮**自动填入**，不用手抄 |

**判据**：手机端状态页能显示服务在线，且开始上传语料（本机日志里出现
`POST /api/upload/...` 或工作台数据开始变长）。

> 手机与这台 Windows 必须在同一局域网；两条都满足还连不上，回头查 §4.2 的防火墙。

---

## 六、验收清单

逐条打勾，全过才算部署完成：

| # | 检查项 | 判据 |
| --- | --- | --- |
| 1 | 安装包体积 | ≥ 25 MB（正常约 41 MB） |
| 2 | 安装包内容 | `python.exe` / `site-packages/fastapi` / `site-packages/sherpa_onnx` 都命中，`mlx_lm` 为 0 |
| 3 | 安装完成 | 开始菜单有「数字人服务」 |
| 4 | 服务起来 | 8766 与 8767 都在监听：`Get-NetTCPConnection -LocalPort 8766,8767 -State Listen` |
| 5 | **窗口内工作台** | 启动后壳窗口里出现「数字人语料服务 · 多租户工作台」（不是停在过渡页） |
| 6 | 浏览器仍可访问（手机/局域网那条链路没被影响） | 浏览器打开 `https://127.0.0.1:8766/console` 出页面 |
| 7 | **关闭 = 常驻** | 点窗口 X → 窗口消失但服务还在（8766 仍 Listen），托盘菜单「打开工作台」能把它叫回来 |
| 8 | **开机自启动** | 托盘「开机自启动」为勾选态；任务管理器「启动应用」里能看到「数字人服务」 |
| 9 | 数据目录 | `%APPDATA%\DigitalHuman` 下出现 `data\`、`logs\api.log`、`env.local` |
| 10 | 云端文字模型 | 连通性测试返回 `ok: true` |
| 11 | 局域网可达（可选） | 另一台机器能打开 `https://<本机局域网IP>:8766/console` |
| 12 | 手机配对 | 手机端显示在线并开始上传 |

---

## 七、故障速查

| 现象 | 根因与处理 |
| --- | --- |
| `npm error could not determine executable to run` | 漏了 `npm ci`（`@tauri-apps/cli` 是 devDependency） |
| 构建报「不支持的目标」 | 漏了 `--bundles nsis` |
| 构建绿了但安装包只有 5 MB | `resources` 映射失效（`.runtime/python` 没生成，或平台配置被改坏）→ 回到 §2.2 |
| 7-Zip 自检三项全 0 | 路径分隔符没归一化（Windows 的 7z 输出反斜杠），不是内容缺失 |
| 装配阶段报 `UnicodeDecodeError: 'charmap'` | `server/requirements.txt` 第 1 行的编码声明被删了 |
| 双击后窗口一直停在「正在启动本地服务…」 | 服务起不来。看 `%APPDATA%\DigitalHuman\logs\api.log` 末尾；常见是端口被占或运行时缺失 |
| 窗口一直停在过渡页、不变工作台 | 回环入口没起来。查日志里有没有 `[desktop] 回环工作台已启动 http://127.0.0.1:8767/console`；没有就说明服务不是壳拉起来的（缺 `DH_DESKTOP_HTTP=1`）或 8767 被占（日志里会有「不可用…已跳过」）。90 秒后会自动退回浏览器打开 |
| 浏览器打不开工作台 | 确认是 `https://` 不是 `http://`；确认点了「继续访问」 |
| 工作台能开但一直提示「先填配对 Token」 | Token 与 `env.local` 里的 `DH_TOKEN` 不一致 |
| 手机连不上 | ①防火墙没放行 8766（§4.2）②手机填了 127.0.0.1 ③不在同一局域网 ④Token 不一致 |
| 日报/总结一直是空的 | 没配云端文字模型（§4.5）。Windows 上本地模型不可用 |
| **点 X 之后窗口就没了** | 预期行为：关闭 = 收进托盘常驻。从托盘「打开工作台」随时叫回来 |
| **托盘里找不到图标** | ①可能被折叠进通知区域的「^」隐藏区 ②确认 `digital-human-desktop.exe` 还在（任务管理器） |
| **开机自启动没生效** | ①托盘菜单里那一项是否勾着 ②`任务管理器 → 启动应用` 里是否被手工禁用过 ③安装路径/用户名里**含空格**时 `Run` 键的值不会加引号，系统拉不起来 —— 这种情况改用「启动」文件夹放快捷方式 |
| 想换端口 | 改 `env.local` 的 `DH_PORT` 后重启。壳窗口加载的是固定 8767 回环入口，不受影响；手机/浏览器要填新端口 |

---

## 附 A：路径与端口一览

| 项 | 位置 |
| --- | --- |
| 数据根目录 | `%APPDATA%\DigitalHuman` |
| 数据库（按租户） | `%APPDATA%\DigitalHuman\data\tenants\<租户>\digitalhuman.db` |
| 语料与音频 | `%APPDATA%\DigitalHuman\data\tenants\<租户>\{audio,corpus}\<日期>\` |
| 日志 | `%APPDATA%\DigitalHuman\logs\api.log` |
| 配置文件 | `%APPDATA%\DigitalHuman\env.local` |
| TLS 自签证书 | `%APPDATA%\DigitalHuman\data\tls\{cert.pem,key.pem}` |
| 默认端口 | `8766`（HTTPS，局域网可访问）与 `8767`（明文，**只绑 127.0.0.1**，壳窗口专用） |
| 服务监听 | `0.0.0.0:8766` + `127.0.0.1:8767` |
| 开机自启动项 | `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` 下的「数字人服务」 |
| 本地模型端口 | 不使用（Windows 走云端） |

## 附 B：本手册对应的代码改动（2026-09-16）

### v1.5.6 · 窗口内工作台 / 关闭常驻 / 开机自启动

Windows 端装好后「显示服务已就绪，但看不到 mac 那样的完整操作台，而且关不掉」，
根因与修法如下（四条一起改，缺一条用户就还是用不顺）：

1. **窗口里只有过渡页**。`desktop/src/index.html` 本来只负责把工作台丢给系统浏览器，
   而**不能**改成在窗口内导航到 `https://127.0.0.1:8766/console`：Tauri 用的
   WebView2 / WKWebView 对不受信证书一律「导航失败」，窗口会白屏。
   修法：服务端新增**只绑回环的明文入口** `127.0.0.1:8767`
   （`server/app/desktop_bridge.py`），由 `lib.rs` 在拉服务时用
   `DH_DESKTOP_HTTP=1` 打开；过渡页就绪后在本窗口内导航到它。
   主链路（8766/TLS）一行未改，手机与局域网行为零变化。
   `server/app/main.py` 的 lifespan 加了「只允许初始化一次」的判定 ——
   同一进程里两台 Server 各有一个事件循环，不设防会起**两个**补跑扫描任务，
   同时扫同一个库。
2. **窗口关不掉**。`lib.rs` 的 `on_window_event` 只调了 `prevent_close()` 却没
   `hide()`，于是点 X 什么也不发生。改成「关闭 = 收进托盘」。
3. **托盘可能是个空图标**。原代码自己 `TrayIconBuilder::with_id("main")` 建了个
   跟配置态**同名**的托盘 —— 托盘注册表是 HashMap，同名 insert 会把配置态那个
   挤掉，而带图标的正是配置态那个（mac 走 `icons/tray.png` + 模板剪影，
   Windows 走 `tauri.windows.conf.json` 覆盖的 `icons/tray-32.png`）。
   自建的那个没设 `.icon()`，tray-icon 便完全不调 `with_icon()`，画出来是空白。
   改成复用配置态托盘，只往上挂菜单。
4. **没有开机自启动**。接入 `tauri-plugin-autostart`：Windows 写
   `HKCU\...\Run`、macOS 写 `~/Library/LaunchAgents`，都在用户态。
   默认值只在**第一次启动**给一次（数据目录里落一个标记），之后完全听用户的
   —— 否则「关掉了又被偷偷打开」比默认不自启更烦人。
   自启动拉起时窗口保持隐藏（服务照常在托盘里跑），用户主动双击时正常显示。

### v1.5.5 及更早

1. `desktop/src-tauri/tauri.conf.json` 未开 `withGlobalTauri` → 过渡页的
   `window.__TAURI__` 是 undefined，页面脚本第一行就抛异常，窗口永远停在转圈。
   已显式开启。
2. `desktop/src-tauri/src/lib.rs` 的 `open_in_browser()` 用 `http://` 打开只支持
   HTTPS 的工作台 → 已改为 `https://`，与 `macapp` 的 `openConsole()` 一致。
   （v1.5.6 起它只作为降级路径。）
3. `bundle.windows.nsis` 的 `installerIcon` / `uninstallerIcon` / `headerImage` /
   `sidebarImage` 默认值都是 `None` → 安装器用的是 NSIS 自带图标。已四项都显式指定。

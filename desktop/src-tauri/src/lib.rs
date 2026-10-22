//! 跨平台控制台的后端逻辑。
//!
//! 只做五件事：拉起/停掉 Python 服务、探活、在**本窗口内**显示工作台、
//! 托盘常驻（关闭 ≠ 退出）、开机自启动。
//! 界面交给已有的 Web 工作台（server/app/console.py），这里不重画一遍 UI ——
//! 那样 macOS 原生版、网页版、桌面版三份 UI 迟早不一致。

use std::net::TcpStream;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;

use tauri::{Manager, State};
use tauri_plugin_autostart::ManagerExt as _;

/// 服务的子进程句柄。
///
/// 单锁保护一个 Option<Child>：这个 app 只会有一个服务实例，
/// 上 tokio / 进程池是纯属多余。
struct Server {
    child: Mutex<Option<Child>>,
}

/// 主服务的 TLS 端口：手机端、局域网、系统默认浏览器都走它。
const DEFAULT_PORT: u16 = 8766;

/// 桌面壳内嵌工作台的回环端口（明文 HTTP，只绑 127.0.0.1）。
///
/// 为什么不能直接加载 8766：那台只提供自签 HTTPS，而 Tauri 用的
/// wry/WebView2/WKWebView 没有任何证书例外 API，遇到自签证书一律「导航失败」
/// —— 窗口会白屏，用户也没有浏览器那种「高级 → 继续访问」可点。
/// 服务端因此额外开了这个明文回环入口，见 server/app/desktop_bridge.py。
///
/// 这里只留一份事实来源：端口由 `do_start()` 显式注入给子进程，两侧不会漂移。
const DESKTOP_PORT: u16 = 8767;

/// 开机自启动时传给自己的标记。用来区分「登录时被系统拉起」和「用户双击打开」：
/// 前者保持静默常驻（用户要的是右下角常驻，不是每次开机弹一个窗口）。
const AUTOSTART_FLAG: &str = "--autostart";

// ---------------------------------------------------------------- 路径

/// 数据根目录：环境与 **bundle 都只读**，数据必须落在用户目录，
/// 否则升级整包替换时会把用户的语料与归档库一起带走。
fn data_root() -> PathBuf {
    if let Ok(p) = std::env::var("DH_DATA_ROOT") {
        return PathBuf::from(p);
    }
    #[cfg(target_os = "macos")]
    {
        if let Some(home) = dirs_like_home() {
            return home.join("Library/Application Support/DigitalHuman");
        }
    }
    #[cfg(target_os = "windows")]
    {
        // Windows: %APPDATA%
        if let Ok(p) = std::env::var("APPDATA") {
            return PathBuf::from(p).join("DigitalHuman");
        }
    }
    #[cfg(target_os = "linux")]
    {
        // Linux: XDG_DATA_HOME，退化 ~/.local/share
        let base = std::env::var("XDG_DATA_HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|_| dirs_like_home().unwrap_or_default().join(".local/share"));
        return base.join("digital-human");
    }
    #[allow(unreachable_code)]
    dirs_like_home().unwrap_or_default().join("DigitalHuman")
}

fn dirs_like_home() -> Option<PathBuf> {
    std::env::var("HOME").or_else(|_| std::env::var("USERPROFILE")).ok().map(PathBuf::from)
}

/// 服务端代码目录。
///
/// 优先级：DH_SERVER_ROOT env（开发态直接指向仓库） > bundle 资源 > ./server。
/// 这一层抽象是必需的：macOS 的 .app、Windows 的 resources、Linux 的
/// /usr/share 三个位置各不相同，硬编码任何一个是「只在开发机上能跑」。
fn server_root(app: &tauri::AppHandle) -> Option<PathBuf> {
    if let Ok(p) = std::env::var("DH_SERVER_ROOT") {
        let p = PathBuf::from(p);
        if p.join("run.py").is_file() {
            return Some(p);
        }
    }
    if let Ok(res) = app.path().resource_dir() {
        let p = res.join("server");
        if p.join("run.py").is_file() {
            return Some(p);
        }
    }
    let p = PathBuf::from("server");
    if p.join("run.py").is_file() {
        return Some(p);
    }
    None
}

/// Python 解释器。
///
/// 顺序：bundle 内嵌的 python-build-standalone > 系统 python3。
/// 内嵌版优先是为了「装机即跑」——要求用户自己配 Python 环境，
/// 在 Windows 上是劝退率最高的一个环节。
fn python_bin(app: &tauri::AppHandle) -> Option<String> {
    if let Ok(p) = std::env::var("DH_PYTHON") {
        return Some(p);
    }
    if let Ok(res) = app.path().resource_dir() {
        let cand = res.join("python").join(if cfg!(windows) { "python.exe" } else { "bin/python3" });
        if cand.is_file() {
            return Some(cand.to_string_lossy().to_string());
        }
    }
    for c in ["python3", "python"] {
        if which(c) {
            return Some(c.to_string());
        }
    }
    None
}

/// PATH 里找可执行文件。Windows 上要额外试 pathext，这里用进程的退出码判定：
/// `-h` 对 python 无害，对不存在的文件会返回错误。
fn which(name: &str) -> bool {
    Command::new(name)
        .arg("--version")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
}

// ---------------------------------------------------------------- 命令

/// 实际启动逻辑。command 与托盘菜单共用这一份。
fn do_start(app: &tauri::AppHandle, state: &Server) -> Result<String, String> {
    if state.child.lock().unwrap().is_some() && is_up() {
        // 还在跑就别拉第二个：两个 uvicorn 抢同一个端口，
        // 后起的那个会静默失败，用户看到的还是「没起来」
        return Ok("already-running".into());
    }
    let root = server_root(app).ok_or("找不到服务端代码目录")?;
    let py = python_bin(app).ok_or("找不到 Python 解释器")?;
    let data = data_root();
    let _ = std::fs::create_dir_all(data.join("logs"));

    let mut cmd = Command::new(py);
    cmd.arg("run.py")
        .current_dir(&root)
        // 数据与配置都走可写的用户目录；代码目录（bundle）只读
        .env("DH_DATA_DIR", data.join("data"))
        .env("DH_ENV_FILE", data.join("env.local"))
        .env("DH_MODEL_DIR", data.join("models"))
        .env("DH_LLM_MODEL_DIR", data.join("llm-models"))
        // 让服务端额外开一个只绑 127.0.0.1 的明文入口，供**本窗口内**加载工作台
        // （原因见 DESKTOP_PORT 的注释）。默认关，所以 mac 原生壳与手工
        // `python run.py` 完全不受影响——只有本壳显式打开。
        .env("DH_DESKTOP_HTTP", "1")
        .env("DH_DESKTOP_HTTP_PORT", DESKTOP_PORT.to_string())
        // 代理变量必须清掉：服务只跟本机与局域网通信，
        // 继承宿主的 HTTP_PROXY 会让 127.0.0.1 的请求被送去公司代理
        .env_remove("HTTP_PROXY")
        .env_remove("HTTPS_PROXY")
        .env_remove("http_proxy")
        .env_remove("https_proxy")
        .stdout(Stdio::null())
        .stderr(Stdio::null());

    // Windows 不给 CREATE_NO_WINDOW 的话，关掉控制台窗口会连服务一起带走
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }

    let child = cmd.spawn().map_err(|e| format!("启动失败：{e}"))?;
    *state.child.lock().unwrap() = Some(child);
    Ok("started".into())
}

#[tauri::command]
fn start_server(app: tauri::AppHandle, state: State<'_, Server>) -> Result<String, String> {
    do_start(&app, &state)
}

#[tauri::command]
fn stop_server(state: State<'_, Server>) -> Result<String, String> {
    let mut guard = state.child.lock().unwrap();
    if let Some(mut child) = guard.take() {
        let _ = child.kill();
        let _ = child.wait();
        return Ok("stopped".into());
    }
    Ok("not-running".into())
}

/// 探活：连得上端口就算起来了。不用 HTTP——服务起的过程中
/// 端口先于路由就绪，TcpStream 比等一个完整 HTTP 往返更早给出信号。
#[tauri::command]
fn server_status() -> bool {
    is_up()
}

fn port_open(port: u16) -> bool {
    TcpStream::connect(("127.0.0.1", port))
        .map(|_| true)
        .unwrap_or(false)
}

fn is_up() -> bool {
    port_open(DEFAULT_PORT)
}

/// 壳内工作台是否就绪。
///
/// 判据是**回环端口通**而不是发一次 HTTP：服务端是在 lifespan 初始化
/// 全部完成之后才起这个监听（`main.py` 里 `desktop_bridge.start()` 的位置），
/// 所以「端口开放」等价于「数据库已打开、补跑扫描器已起」，
/// 不会出现「页面打得开但接口连续 500」的中间态。
#[tauri::command]
fn desktop_ready() -> bool {
    port_open(DESKTOP_PORT)
}

/// 壳内工作台地址。让前端拿它去 location.replace，避免端口在 JS 里再写一份。
#[tauri::command]
fn desktop_url() -> String {
    format!("http://127.0.0.1:{DESKTOP_PORT}/console")
}

/// 用系统 opener 打开 URL 或目录。
///
/// 用系统命令而不是 tauri-plugin-opener：少一个依赖，三端行为一致。
fn reveal(path_or_url: &str) -> Result<(), String> {
    let mut cmd = if cfg!(windows) {
        let mut c = Command::new("cmd");
        c.args(["/c", "start", ""]).arg(path_or_url);
        c
    } else if cfg!(target_os = "macos") {
        let mut c = Command::new("open");
        c.arg(path_or_url);
        c
    } else {
        let mut c = Command::new("xdg-open");
        c.arg(path_or_url);
        c
    };
    cmd.stdout(Stdio::null()).stderr(Stdio::null());
    cmd.spawn().map(|_| ()).map_err(|e| format!("无法打开：{e}"))
}

/// 降级路径：壳内入口用不了时，用系统浏览器打开工作台。
///
/// 必须是 **https**：主服务（server/run.py）恒定带 ssl_certfile/ssl_keyfile，
/// 只监听 TLS，明文 http 打过去是 `Empty reply from server`（实测 000）。
/// macapp 的 openConsole() 用的是同一个 https 地址。
#[tauri::command]
fn open_console() -> Result<(), String> {
    open_in_browser()
}

fn open_in_browser() -> Result<(), String> {
    reveal(&format!("https://127.0.0.1:{DEFAULT_PORT}/console"))
}

#[tauri::command]
fn reveal_data() -> Result<(), String> {
    reveal(&data_root().to_string_lossy())
}

#[tauri::command]
fn data_dir() -> String {
    data_root().to_string_lossy().to_string()
}

// ---------------------------------------------------------------- 窗口 / 托盘

/// 把主窗口显示出来（托盘「打开工作台」用）。
///
/// 刻意不写成 `get_webview_window("main")?`：窗口 label 在 tauri.conf.json 里
/// 没写，靠的是默认值 `"main"`（tauri-utils config.rs 的 default_label）。
/// 一旦哪天有人显式改了 label，写死 "main" 会让整条「打开工作台」静默失效
/// —— 菜单点下去毫无反应，而日志里什么都没有。这里退一步取任意一个窗口。
fn show_window(app: &tauri::AppHandle) {
    let w = app
        .get_webview_window("main")
        .or_else(|| app.webview_windows().into_values().next());
    if let Some(w) = w {
        let _ = w.show();
        let _ = w.unminimize();
        let _ = w.set_focus();
    }
}

/// 「默认打开开机自启动」是否已经给过一次。
///
/// 为什么需要这个标记：用户要的是「装完默认就自启」，但**默认值只该在第一次
/// 生效**。若每次启动都无脑 enable()，用户在托盘里关掉自启之后，下次启动又被
/// 悄悄拉回来——那是比「默认不自启」更烦人的 bug（勾选状态与实际不符）。
/// 放数据目录：跟着用户走，卸载重装（数据目录同时被清）才会重新给一次默认值。
fn autostart_marker() -> PathBuf {
    data_root().join("autostart.default.done")
}

fn ensure_autostart_default(app: &tauri::AppHandle) {
    let flag = autostart_marker();
    if flag.exists() {
        return;
    }
    let m = app.autolaunch();
    let ok = if m.is_enabled().unwrap_or(false) {
        // 用户（或上次安装）已经打开了，不用动他
        true
    } else {
        m.enable().is_ok()
    };
    if !ok {
        // 失败就不落标记：下次启动再试一次，而不是把「没成功」当成「设过了」
        return;
    }
    if let Some(dir) = flag.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    let _ = std::fs::write(&flag, b"1");
}

/// 托盘：常驻服务的唯一可见入口。
///
/// 菜单九项之外刻意不加：更多条目等于让人在自己机器上迷路，而设置一个国家或
/// 行业的入口本来就属于工作台页面。
///
/// 实现上**复用配置里的托盘**（tauri.conf.json 的 app.trayIcon，id = "main"），
/// 而不是自己再建一个同名的。这一点很关键：早先这里是
/// `TrayIconBuilder::with_id("main")`，而托盘注册表是个 HashMap，
/// 同名 insert 会把配置态那个挤掉——配置态那个才带图标（mac 用 icons/tray.png
/// + iconAsTemplate，Windows 用 tauri.windows.conf.json 覆盖成 icons/tray-32.png），
/// 而自建的那个没设 `.icon()`，tray-icon 便完全不调 `with_icon()`，
/// 画出来是个**空图标**。用户看到的就是「右下角什么都没有」，
/// 自然也就点不到菜单去退出。
fn build_tray(app: &tauri::AppHandle) -> Result<(), Box<dyn std::error::Error>> {
    use tauri::menu::{CheckMenuItemBuilder, MenuBuilder, MenuItemBuilder};
    use tauri::tray::TrayIconBuilder;

    let tray = match app.tray_by_id("main") {
        Some(t) => t,
        None => {
            // 兜底：配置里没有 trayIcon 时自建。必须显式给图标（原因见函数注释），
            // 拿 bundle 图标顶上，至少不是一片空白。
            let mut b = TrayIconBuilder::with_id("main");
            if let Some(icon) = app.default_window_icon().cloned() {
                b = b.icon(icon);
            }
            b.build(app)?
        }
    };

    // 勾选态取自**系统里的实际状态**，不是我们自己记的变量：用户可能在
    // Windows「任务管理器 → 启动应用」或 mac「系统设置 → 通用 → 登录项」
    // 里手工关掉，菜单必须如实反映。
    let autostart_on = app.autolaunch().is_enabled().unwrap_or(false);
    let auto = CheckMenuItemBuilder::with_id("autostart", "开机自启动")
        .checked(autostart_on)
        .build(app)?;

    let show = MenuItemBuilder::with_id("show", "打开工作台").build(app)?;
    let start = MenuItemBuilder::with_id("start", "启动服务").build(app)?;
    let stop = MenuItemBuilder::with_id("stop", "停止服务").build(app)?;
    let reveal_item = MenuItemBuilder::with_id("reveal", "显示数据目录").build(app)?;
    let quit = MenuItemBuilder::with_id("quit", "退出").build(app)?;

    let menu = MenuBuilder::new(app)
        .item(&show)
        .item(&auto)
        .separator()
        .item(&start)
        .item(&stop)
        .separator()
        .item(&reveal_item)
        .item(&quit)
        .build()?;

    // 勾选项要能被事件回调改状态，而回调拿不到 builder 里的那个句柄
    // （CheckMenuItem 是 Arc 包装的，clone 出来指的是同一项）。
    let auto_item = auto.clone();

    tray.set_menu(Some(menu))?;
    tray.set_tooltip(Some("数字人服务"))?;
    tray.on_menu_event(move |app, event| match event.id().as_ref() {
        // 托盘菜单直接在 Rust 侧执行动作，不发事件给前端再绕回来——
        // 那条链路要求前端活着才有意义，而托盘恰恰要在前端没开时也能用
        "show" => show_window(app),
        "autostart" => {
            let m = app.autolaunch();
            let on = m.is_enabled().unwrap_or(false);
            let _ = if on { m.disable() } else { m.enable() };
            // 回读系统真实状态再刷新勾选：写失败（组策略 / 权限）时菜单不撒谎
            if let Ok(now) = m.is_enabled() {
                let _ = auto_item.set_checked(now);
            }
        }
        "start" => {
            let state: State<'_, Server> = app.state();
            let _ = do_start(app, &state);
        }
        "stop" => {
            let state: State<'_, Server> = app.state();
            let _ = stop_server(state);
        }
        "reveal" => { let _ = reveal(&data_root().to_string_lossy()); }
        "quit" => {
            // 退出前必须带走子进程：它们不随父进程退出，
            // 留下来就是没人能关掉的孤儿进程，还占着端口
            let state: State<'_, Server> = app.state();
            let _ = stop_server(state);
            app.exit(0);
        }
        _ => {}
    });
    Ok(())
}

// ---------------------------------------------------------------- 入口

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        // 单实例保护，**必须第一个注册**（插件自身的要求）。
        //
        // 为什么必需：同一台机器上「服务端」只能有一个，而「开机自启」与
        // 「用户双击桌面快捷方式」是两条互不知情的拉起路径。没有这层保护时，
        // 第二条路径会再起一个完整实例 —— 托盘多一个图标、再 spawn 一套
        // Python 子进程，与新实例抢同一个 8766/1235 端口，表现为「一个在跑、
        // 一个起不来」，而用户点错那个还会把正在跑的服务停掉。
        // macOS 壳上这个故障 2026-09-18 实际发生过（登录项 + LaunchAgent
        // 同时拉起），修法与论证见 macapp/Sources/DigitalHumanApp/SingleInstance.swift。
        //
        // 回调在**第二个**实例里执行：把已有实例的窗口唤到前台再退出，
        // 否则用户双击的观感是「点了没反应」。
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.show();
                let _ = w.unminimize();
                let _ = w.set_focus();
            }
        }))
        .plugin(tauri_plugin_shell::init())
        // 开机自启动。args 会写进系统里的启动项（Windows: HKCU 的 Run 键，
        // macOS: ~/Library/LaunchAgents 的 plist），随下次启动传回来，
        // 用来区分「登录时被拉起」与「用户双击打开」。
        .plugin(tauri_plugin_autostart::init(
            tauri_plugin_autostart::MacosLauncher::LaunchAgent,
            Some(vec![AUTOSTART_FLAG]),
        ))
        .manage(Server { child: Mutex::new(None) })
        .invoke_handler(tauri::generate_handler![
            start_server,
            stop_server,
            server_status,
            desktop_ready,
            desktop_url,
            open_console,
            reveal_data,
            data_dir
        ])
        .setup(|app| {
            ensure_autostart_default(app.handle());
            build_tray(app.handle())?;
            // 窗口默认是可见的（tauri.conf.json 没设 visible）。被自启动拉起时
            // 收起来：服务照常在托盘里跑，用户点「打开工作台」随时能看。
            // 用户主动双击打开时保持可见，否则会以为「点了没反应」。
            if std::env::args().any(|a| a == AUTOSTART_FLAG) {
                if let Some(w) = app.handle().get_webview_window("main") {
                    let _ = w.hide();
                }
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            // 关闭 = 收进托盘常驻，**不等于退出**。
            //
            // 这是个常驻服务：窗口只是它的一个视图，退出的唯一入口是托盘菜单的
            // 「退出」（那里会先带走 Python 子进程）。早先这里只调了
            // prevent_close() 而没有 hide()，于是点 X 之后什么也不发生——
            // 用户报的就是「无法关闭」。
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .build(tauri::generate_context!())
        .expect("构建 Tauri 应用失败")
        .run(|_app, _event| {});
}

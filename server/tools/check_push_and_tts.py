#!/usr/bin/env python3
"""自测脚本：TTS 合成 / 工作台推送幂等 / 未配置降级 / 既有端点回归。

## 测的是哪个实例（先看这段）

    本次用的是源码实例(8777)，生产实例(8767) 未被触碰。

脚本自己拉起（或复用）**源码实例**：主服务 `DH_PORT=8776`、桌面桥接
`DH_DESKTOP_HTTP_PORT=8777`，断言全部打 **8777 的明文 HTTP**。

生产实例是 macOS App 打包的那一份（8766/8767），跑着用户自己的数据与进程。
脚本连它的端口都不碰：改它的配置、杀它的进程，对用户正在用的服务是事故，
而这次要验证的东西（幂等、降级）完全可以在一个隔离实例上验证。

## 为什么脚本要自己拉实例

采集服务的配置只在**进程启动时**读一次。想在「没配工作台」的状态下验证降级，
就需要一个**配置不同的进程**——没法靠改环境变量去影响一个已经跑着的服务。
与其去改生产实例的配置（危险），不如起一个带独立 `DATA_DIR` 的临时实例。

## 退出码

0 = 全部断言通过；1 = 有断言失败。

## 用法

    python server/tools/check_push_and_tts.py

可选参数：
    --base-port 8777      源码实例桌面桥接端口（默认 8777）
    --keep                跑完不关掉脚本自己拉起的实例（默认跑完就关）
    --reuse               端口上已有源码实例就复用（默认总是拉全新的）
    --python <路径>       指定跑服务端源码的 python（默认自动找 App 内嵌的那套）
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
BUNDLE_RES = Path("/Applications/数字人服务.app/Contents/Resources")

# 源码实例的端口。刻意与生产实例（8766/8767）错开，避免「打错端口打到生产」。
SRC_HTTP_PORT = 8776
SRC_BRIDGE_PORT = 8777
# 两个降级实例：没配工作台的、基址指向死端口的。
BADCFG_BRIDGE_PORT = 8779
DEADURL_BRIDGE_PORT = 8781
# 假 Hermes（门禁自带）与「Hermes 不可达」降级实例的端口。
FAKE_HERMES_PORT = 8790
HERMES_DEAD_BRIDGE_PORT = 8783

# 源码实例的 token：仓库 env.local 没有设 DH_TOKEN，于是走默认值。
# （生产实例用的是另一个真实 token，与本脚本无关，也不该被本脚本读到。）
DEFAULT_TOKEN = "digital-human"


# --------------------------------------------------------------------------
# 断言收集
# --------------------------------------------------------------------------
class Checker:
    """收集断言结果。失败不中断——一次跑完把所有问题都摆出来，比逐个试错快。"""

    def __init__(self) -> None:
        self.passed = 0
        self.failures: list[str] = []

    def check(self, ok: bool, name: str, detail: str = "") -> bool:
        if ok:
            self.passed += 1
            print(f"  [PASS] {name}" + (f" —— {detail}" if detail else ""))
        else:
            self.failures.append(f"{name}：{detail}")
            print(f"  [FAIL] {name} —— {detail}")
        return ok

    def summary(self) -> int:
        total = self.passed + len(self.failures)
        print()
        print(f"断言合计 {total} 条：通过 {self.passed}，失败 {len(self.failures)}")
        for f in self.failures:
            print(f"  - {f}")
        return 0 if not self.failures else 1


# --------------------------------------------------------------------------
# HTTP 辅助
# --------------------------------------------------------------------------
# 显式禁代理的 opener。
# 为什么：环境里若注入 HTTP_PROXY，urllib 会把 127.0.0.1 的请求也丢给代理
# （NO_PROXY 不一定覆盖到），表现是「服务明明在跑，脚本却探不到」。
# 与服务端 hermes_client.py / workbench_push.py 的处理一致。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http(url: str, payload: dict | None = None, token: str = "",
         method: str = "", timeout: float = 240.0) -> tuple[int, bytes, object]:
    """发一个请求，返回 (状态码, 响应体, 响应头)。

    刻意**不**抛 HTTPError：4xx/5xx 本身就是断言对象，
    在这里转成返回值，断言才能直接看状态码。

    响应头**原样返回**（email.message.Message），不转成 dict：
    Starlette 发出来的是小写头名（content-type），转成 dict 后
    `headers.get("Content-Type")` 就永远取不到值；Message 的 get 是大小写不敏感的。
    """
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method=method or ("POST" if data is not None else "GET"))
    if data is not None:
        req.add_header("Content-Type", "application/json; charset=utf-8")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return resp.status, resp.read(), resp.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


def jbody(raw: bytes) -> dict:
    try:
        v = json.loads(raw.decode("utf-8", errors="replace"))
        return v if isinstance(v, dict) else {"_": v}
    except ValueError:
        return {"_raw": raw[:200].decode("utf-8", errors="replace")}


def is_wav(body: bytes) -> bool:
    """是不是合法的 WAV：RIFF 头 + WAVE 标识 + 有采样数据。

    只看 RIFF 不够——「新奇声音」那类静默失败会产出 44 字节的空音频
    （只有 WAV 头没有采样数据），file 看着像 wav，播出来是空的。
    """
    return len(body) > 44 and body[:4] == b"RIFF" and body[8:12] == b"WAVE"


# --------------------------------------------------------------------------
# 实例管理
# --------------------------------------------------------------------------
def port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def wait_port_free(port: int, timeout: float = 30.0) -> bool:
    """等端口真正空出来。

    探法与服务端一致：带 SO_REUSEADDR（uvicorn 建监听套接字时也带）。
    少了这个选项，刚退干净的实例留下的 TIME_WAIT 会让 bind 报「端口被占」，
    于是脚本会对着一个「起不来」的端口干等。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            time.sleep(0.3)
        finally:
            s.close()
    return False


def kill_stale(data_dir: Path) -> None:
    """把上一轮跑剩的测试实例收掉。

    必须收：**复用上一个实例等于拿旧代码跑断言** —— 脚本会「通过」，
    而被测的其实是改动之前的服务。这种假绿比红更糟。
    只杀自己拉起的：pid 记在 data_dir/instance.pid 里，跑完就删，
    所以这里杀的一定是本脚本的残留，不会碰到别人的进程。
    """
    pid_file = data_dir / "instance.pid"
    if not pid_file.is_file():
        return
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        pid_file.unlink(missing_ok=True)
        return
    try:
        os.kill(pid, 15)          # SIGTERM
    except OSError:
        pid_file.unlink(missing_ok=True)
        return
    for _ in range(50):          # 等它退干净，最多 10 秒
        time.sleep(0.2)
        try:
            os.kill(pid, 0)
        except OSError:
            break                 # 已经退了
    else:
        try:
            os.kill(pid, 9)       # SIGKILL 兜底
        except OSError:
            pass
    pid_file.unlink(missing_ok=True)


def wait_ready(port: int, timeout: float = 90.0) -> bool:
    """等明文端口上的服务应答。

    探的是 **/api/agent/health** 而不是 /api/health：后者要鉴权（手机端 Token），
    拿它探活会永远拿到 401，于是「实例其实起来了」却被判成「没起来」。
    agent/health 是公开端点，只返能力标志、不含任何数据。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status, _, _ = http(f"http://127.0.0.1:{port}/api/agent/health", timeout=3)
            if status == 200:
                return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


def find_python(explicit: str = "") -> tuple[str, dict[str, str]]:
    """找一个能跑起服务端源码的 python。

    顺序：显式指定 → 仓库 .venv → **App 内嵌的那套** → 系统 python。
    App 内嵌那套是生产环境实际在用的，依赖最齐；系统 python 往往是 3.9，
    跑不动 `str | None` 这类新式注解（pydantic 在导入期就会炸）。
    """
    if explicit:
        return explicit, {}
    venv = SERVER_DIR / ".venv" / "bin" / "python"
    if venv.is_file():
        return str(venv), {}
    bundle_py = BUNDLE_RES / "python" / "bin" / "python3"
    site = BUNDLE_RES / "site-packages"
    if bundle_py.is_file() and site.is_dir():
        return str(bundle_py), {"PYTHONPATH": str(site)}
    return sys.executable, {}


def start_instance(name: str, http_port: int, bridge_port: int,
                   data_dir: Path, env_file: Path | None,
                   extra_env: dict[str, str], py: str,
                   py_env: dict[str, str]) -> subprocess.Popen:
    """拉起一个源码实例。日志落到 data_dir 旁的 .log，出问题能直接翻。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    # 本机回环调用一律不走代理：环境里注入的 HTTP_PROXY 会把 127.0.0.1 的请求
    # 发到外网代理上（curl 报 502、urllib 报超时），表现成「服务没起来」。
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
              "ALL_PROXY", "all_proxy"):
        env.pop(k, None)
    env.update(py_env)
    env["DH_PORT"] = str(http_port)
    env["DH_HOST"] = "127.0.0.1"          # 只绑回环，别把测试实例暴露到局域网
    env["DH_DESKTOP_HTTP"] = "1"
    env["DH_DESKTOP_HTTP_PORT"] = str(bridge_port)
    env["DH_DATA_DIR"] = str(data_dir)
    # 不设 DH_TOKEN → 源码实例用默认值；生产实例的真实 token 与本脚本无关。
    env.pop("DH_TOKEN", None)
    # 处理窗口设成一个几乎不会命中的时刻：否则窗口内（默认 13:00/17:45）
    # 这个实例会把 3.6GB 的本地 LLM 拉起来，白占机器资源。
    env["DH_AGG_WINDOWS"] = "03:00"
    if env_file is not None:
        env["DH_ENV_FILE"] = str(env_file)
    else:
        env.pop("DH_ENV_FILE", None)
    env.update(extra_env)

    log_path = data_dir.parent / f"src-instance-{name}.log"
    log_file = open(log_path, "ab", buffering=0)
    proc = subprocess.Popen(
        [py, "run.py"], cwd=str(SERVER_DIR), env=env,
        stdout=log_file, stderr=log_file, start_new_session=True,
    )
    proc._dh_log = log_path          # type: ignore[attr-defined]
    proc._dh_log_file = log_file     # type: ignore[attr-defined]
    # 记 pid：下一轮脚本靠它把上次的残留收掉（见 kill_stale）。
    (data_dir / "instance.pid").write_text(str(proc.pid), encoding="utf-8")
    return proc


def stop_instance(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        proc._dh_log_file.close()      # type: ignore[attr-defined]
    except Exception:
        pass


# --------------------------------------------------------------------------
# 假 Hermes（门禁自带，零新增依赖）
# --------------------------------------------------------------------------
FAKE_HERMES_SRC = r'''
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(sys.argv[1])


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/health"):
            self._send(200, b'{"ok": true}')
        else:
            self._send(404, b'{"error": "unknown path"}')

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        self._send(200, json.dumps({
            "ok": True, "skip": False,
            "reply_text": "已收到这段物料描述。",
            "reply_short": "已收到这段物料描述。",
            "intent_type": "note", "ha_used": False,
            "call_id": "fake-hermes", "duration_ms": 12,
        }).encode("utf-8"))

    def log_message(self, *args):
        pass


HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
'''


def start_fake_hermes(port: int, work_dir: Path, py: str) -> subprocess.Popen:
    """起一个最小假 Hermes。

    为什么不用本机那个真的：门禁必须能在任何机器上复现。
    那个常驻进程不在线时断言就会红，而那是环境问题、不是代码问题。
    这里的假服务只实现门禁要用的两个端点：/health 与 /hermes/voice/invoke。
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    src = work_dir / "fake_hermes.py"
    src.write_text(FAKE_HERMES_SRC, encoding="utf-8")
    log_file = open(work_dir / "fake-hermes.log", "ab", buffering=0)
    proc = subprocess.Popen([py, str(src), str(port)], stdout=log_file,
                            stderr=log_file, start_new_session=True)
    proc._dh_log_file = log_file      # type: ignore[attr-defined]
    for _ in range(50):
        if port_open(port):
            return proc
        time.sleep(0.1)
    return proc


# --------------------------------------------------------------------------
# 假工作台（门禁自带，零新增依赖）
# --------------------------------------------------------------------------
#
# 为什么必须有它：**门禁不能打真实工作台**。
# 之前这个脚本用 env.local 里的真地址与真 Key，跑一次就在生产库里留下
# 一批抽取作业、抽取文档与审计记录 —— 门禁本身成了数据污染源，
# 跑一次脏一次库，最后只能靠人工清理兜着。
#
# 判据不能是「我以为打的是假的」，要落在数据上：
# 假工作台把收到的每个请求写进 received.jsonl，
# 脚本跑完读它 —— 收不到请求就说明采集端打去了别处。
FAKE_WORKBENCH_PORT = 8791

FAKE_WORKBENCH_SRC = r'''
import json
import sys
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(sys.argv[1])
LOG = sys.argv[2]

# 收到的作业按 sourceRef 记一份：幂等断言要拿它查「同一条作业只有一条」。
RECEIVED = []


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/content/extractions"):
            self._send(200, json.dumps(RECEIVED, ensure_ascii=False).encode("utf-8"))
        else:
            self._send(404, b'{"error": "unknown path"}')

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError:
            payload = {}
        job_id = str(uuid.uuid4())
        # 工作台把 title 存进 sourceRef（见 content.controller.ts 的 transcript 分支），
        # 假工作台必须照抄这条规则 —— 否则幂等断言按 sourceRef 查不到同一条作业。
        title = payload.get("title") or "对话纪要"
        RECEIVED.append({
            "id": job_id, "sourceRef": title,
            "status": "CONFIRMED", "score": 97,
            "docType": payload.get("docType") or "meeting",
        })
        try:
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "path": self.path, "title": title,
                    "tenant": self.headers.get("X-Tenant-Id") or "",
                    "authorized": bool(self.headers.get("Authorization")),
                }, ensure_ascii=False) + "\n")
        except OSError:
            pass
        self._send(201, json.dumps({
            "id": job_id, "status": "CONFIRMED", "score": 97,
            "document": {
                "docType": payload.get("docType") or "meeting",
                "title": title, "confirmed": True,
                "fields": [
                    {"key": "name", "label": "名称", "value": "自检产品",
                     "confidence": 0.98},
                    {"key": "brand", "label": "品牌", "value": "自检品牌",
                     "confidence": 0.95},
                    {"key": "model", "label": "型号", "value": "X1",
                     "confidence": 0.93},
                    {"key": "price", "label": "单价", "value": "899",
                     "confidence": 0.91},
                    {"key": "unit", "label": "单位", "value": "个",
                     "confidence": 0.9},
                ],
            },
            "deficiencies": [],
        }, ensure_ascii=False).encode("utf-8"))

    def log_message(self, *args):
        pass


HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
'''


def start_fake_workbench(port: int, work_dir: Path, py: str) -> subprocess.Popen:
    """起一个最小假工作台。

    只实现门禁要用的两个端点：
      POST /api/content/extractions/transcript  建一条抽取作业
      GET  /api/content/extractions             列出作业（幂等断言要查）
    响应字段与真实工作台对齐（content.controller.ts / content.service.ts）。
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    src = work_dir / "fake_workbench.py"
    src.write_text(FAKE_WORKBENCH_SRC, encoding="utf-8")
    log_file = open(work_dir / "fake-workbench.log", "ab", buffering=0)
    proc = subprocess.Popen(
        [py, str(src), str(port), str(work_dir / "received.jsonl")],
        stdout=log_file, stderr=log_file, start_new_session=True)
    proc._dh_log_file = log_file      # type: ignore[attr-defined]
    for _ in range(50):
        if port_open(port):
            return proc
        time.sleep(0.1)
    return proc


# --------------------------------------------------------------------------
# 配置读取（工作台凭据）
# --------------------------------------------------------------------------
def read_env_file(path: Path) -> dict[str, str]:
    """读 env.local 里的 KEY=VALUE（忽略 export 前缀与成对引号）。

    刻意自己解析而不是 import config：config 读的是**进程**的环境，
    而脚本是独立进程，它的 env 与被测实例未必一致。
    """
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        if s.startswith("export "):
            s = s[len("export "):].strip()
        k, _, v = s.partition("=")
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        if k:
            out[k] = v
    return out


def workbench_conf(overrides: dict[str, str] | None = None) -> dict[str, str]:
    """工作台对接配置：先看仓库 env.local，再看本进程环境，最后被显式覆盖。

    `overrides` 不是可选项而是**必需**：门禁不能打真实工作台 ——
    用真地址与真 Key 跑一次，就在生产库里留下一批抽取作业与审计记录，
    门禁本身成了数据污染源。这里强制把地址与凭据换成门禁自带的假工作台，
    凭据是假的、租户是假的，只有**代码路径**是真的。
    """
    conf = read_env_file(SERVER_DIR / "env.local")
    for k in ("DH_WORKBENCH_BASE_URL", "DH_WORKBENCH_API_KEY",
              "DH_WORKBENCH_TENANT_ID"):
        if not conf.get(k) and os.environ.get(k):
            conf[k] = os.environ[k].strip()
    if overrides:
        conf.update(overrides)
    return conf


# --------------------------------------------------------------------------
# 断言主体
# --------------------------------------------------------------------------
def assert_regression(base: str, token: str, agent_token: str, c: Checker) -> None:
    """既有端点回归：这次加了两个端点，不能把老的带坏。"""
    print("\n[1] 既有端点回归")
    status, _, _ = http(f"{base}/api/health", token=token)
    c.check(status == 200, "/api/health 可用", f"HTTP {status}")

    status, _, _ = http(f"{base}/api/agent/health")
    c.check(status == 200, "/api/agent/health 可用", f"HTTP {status}")

    status, _, _ = http(f"{base}/api/agent/days", token=agent_token)
    c.check(status == 200, "/api/agent/days 可用", f"HTTP {status}")

    status, body, headers = http(f"{base}/api/tts/preview", token=token)
    c.check(status == 200 and is_wav(body), "/api/tts/preview 仍能合成",
            f"HTTP {status}，{len(body)} 字节，{headers.get('Content-Type')}")


def assert_tts_synth(base: str, token: str, c: Checker) -> None:
    print("\n[2] POST /api/tts/synthesize")
    text = "数字人语料采集服务的语音合成自检，这是一句用于验证的中文测试语句。"
    status, body, headers = http(f"{base}/api/tts/synthesize",
                                 {"text": text}, token=token)
    c.check(status == 200, "合成返回 200", f"HTTP {status}，{len(body)} 字节")
    c.check(headers.get("Content-Type", "").startswith("audio/wav"),
            "Content-Type 是 audio/wav", headers.get("Content-Type", "(无)"))
    c.check(is_wav(body), "是合法 WAV（RIFF 头 + 有采样数据）",
            f"前 12 字节 {body[:12]!r}，共 {len(body)} 字节")
    echoed = urllib.parse.unquote(headers.get("X-TTS-Text", ""))
    c.check(echoed == text[:500], "X-TTS-Text 回显了念的内容", echoed[:40])

    # 超长必须 400：这是会被外部调用的端点，不能无限吃。
    status, body, _ = http(f"{base}/api/tts/synthesize",
                           {"text": "字" * 20001}, token=token)
    c.check(status == 400, "超长文本返回 400", f"HTTP {status}，{jbody(body)}")

    # 响应头不能被撑爆：一个中文汉字 URL 编码后占 9 字节，
    # 不截断的话 20000 字就是 180KB 的头，Node 客户端（默认 16KB）会直接丢响应。
    long_text = "这是一段用于验证响应头长度的中文文本，" * 40
    status, body, headers = http(f"{base}/api/tts/synthesize",
                                 {"text": long_text}, token=token)
    hdr_len = len(headers.get("X-TTS-Text", "") or "")
    c.check(status == 200 and hdr_len <= 16384,
            "长文本的 X-TTS-Text 头仍在 16KB 以内",
            f"HTTP {status}，头 {hdr_len} 字节（文本 {len(long_text)} 字）")


def assert_push(base: str, token: str, c: Checker,
                conf: dict[str, str]) -> None:
    print("\n[3] POST /api/push/workbench（幂等）")
    if not (conf.get("DH_WORKBENCH_API_KEY") and conf.get("DH_WORKBENCH_TENANT_ID")):
        c.check(False, "工作台已配置（DH_WORKBENCH_API_KEY / DH_WORKBENCH_TENANT_ID）",
                "server/env.local 里没有这两个配置，无法验证推送")
        return

    stamp = f"{int(time.time())}-{os.getpid()}"
    ref = f"check-{stamp}"
    title = f"幂等自检-{stamp}"
    text = ("这是一段用于验证推送幂等性的物料描述文本，长度超过二十个字符，"
            "包含产品名称、品牌与若干规格参数，确保工作台能够正常接收并完成结构化抽取。")
    payload = {"text": text, "docType": "product", "ref": ref, "title": title}

    s1, b1, _ = http(f"{base}/api/push/workbench", payload, token=token)
    j1 = jbody(b1)
    c.check(s1 == 200, "首次推送返回 200", f"HTTP {s1}，{json.dumps(j1, ensure_ascii=False)[:200]}")
    c.check(j1.get("duplicate") is False, "首次推送标记为非重复",
            f"duplicate={j1.get('duplicate')}")
    c.check(bool(j1.get("jobId")), "拿到了工作台的作业 id", str(j1.get("jobId")))
    c.check(j1.get("docType") == "product", "docType 透传为 product",
            str(j1.get("docType")))
    c.check(isinstance(j1.get("deficiencyCount"), int),
            "返回了缺项数", str(j1.get("deficiencyCount")))

    s2, b2, _ = http(f"{base}/api/push/workbench", payload, token=token)
    j2 = jbody(b2)
    c.check(s2 == 200, "重复推送返回 200", f"HTTP {s2}")
    c.check(j2.get("duplicate") is True, "重复推送被识别为重复",
            f"duplicate={j2.get('duplicate')}，status={j2.get('status')}")
    c.check(j2.get("jobId") == j1.get("jobId"),
            "重复推送回显同一个作业 id（没有二次入库）",
            f"首次 {j1.get('jobId')} / 重复 {j2.get('jobId')}")

    # 直接问工作台：这个 sourceRef 到底建了几条作业。
    wb_base = (conf.get("DH_WORKBENCH_BASE_URL") or "http://127.0.0.1:3080").rstrip("/")
    st, wb_body, _ = http(f"{wb_base}/api/content/extractions",
                           token=conf.get("DH_WORKBENCH_API_KEY", ""),
                           method="GET")
    if st == 200:
        rows = jbody(wb_body)
        items = rows if isinstance(rows, list) else (rows.get("_") or [])
        same = [r for r in items if isinstance(r, dict) and r.get("sourceRef") == title]
        c.check(len(same) == 1, "工作台侧只建了一条作业（没有产生两次入库）",
                f"sourceRef={title} 命中 {len(same)} 条")
    else:
        c.check(False, "能查到工作台的作业列表", f"HTTP {st}")


def assert_degraded(bridge_port: int, token: str, c: Checker,
                    label: str, expect: str) -> None:
    """降级断言：配置坏了必须是 503 + 说清原因，绝不能 500。"""
    base = f"http://127.0.0.1:{bridge_port}"
    payload = {
        "text": "这是一段用于验证降级行为的物料描述文本，长度超过二十个字符，"
                "在配置缺失或对端不可达时必须返回明确的 503 而不是崩溃。",
        "docType": "meeting", "ref": f"degraded-{label}-{int(time.time())}",
    }
    status, body, _ = http(f"{base}/api/push/workbench", payload, token=token)
    detail = str(jbody(body).get("detail", ""))
    c.check(status == 503, f"{label}：返回 503（不是 500）", f"HTTP {status}")
    c.check(expect in detail, f"{label}：提示里说明了原因", detail[:160])


def assert_hermes_via(base: str, token: str, c: Checker,
                      conf: dict[str, str]) -> None:
    """经 Hermes 中转投递：文本先过 Hermes（它做理解），再投工作台。"""
    print("\n[6] 经 Hermes 中转投递")
    if not (conf.get("DH_WORKBENCH_API_KEY") and conf.get("DH_WORKBENCH_TENANT_ID")):
        c.check(False, "工作台已配置（无法验证中转投递）",
                "server/env.local 里没有 DH_WORKBENCH_API_KEY / DH_WORKBENCH_TENANT_ID")
        return

    text = ("这是一段用于验证经 Hermes 中转投递的物料描述文本，长度超过二十个字符，"
            "包含产品名称、品牌与规格参数，工作台应当能正常完成结构化抽取。")
    stamp = f"{int(time.time())}-{os.getpid()}"

    # 1) 开启中转
    payload = {"text": text, "docType": "product", "ref": f"hermes-{stamp}",
               "title": f"中转自检-{stamp}", "viaHermes": True}
    s, b, _ = http(f"{base}/api/push/workbench", payload, token=token)
    j = jbody(b)
    c.check(s == 200, "经 Hermes 中转推送返回 200",
            f"HTTP {s}，{json.dumps(j, ensure_ascii=False)[:200]}")
    c.check(j.get("viaHermes") is True, "响应标明了走了 Hermes 中转",
            f"viaHermes={j.get('viaHermes')}")
    hj = j.get("hermes")
    c.check(isinstance(hj, dict) and "intentType" in hj,
            "响应带 Hermes 的理解结果（intentType 等）",
            json.dumps(hj, ensure_ascii=False)[:160] if hj is not None else "(null)")
    c.check(bool(j.get("jobId")), "中转之后照样拿到工作台作业 id", str(j.get("jobId")))

    # 2) 字符串 "false"：不能被当成 True
    #    bool("false") 是 True —— 那样「显式说不要走中转」会被当成「要走」，方向反了。
    payload2 = dict(payload)
    payload2["ref"] = f"hermes-false-{stamp}"
    payload2["title"] = f"中转关闭自检-{stamp}"
    payload2["viaHermes"] = "false"
    s2, b2, _ = http(f"{base}/api/push/workbench", payload2, token=token)
    j2 = jbody(b2)
    c.check(s2 == 200 and j2.get("viaHermes") is False,
            "viaHermes 传字符串 \"false\" 时确实不走中转（bool('false') 是 True，会把方向搞反）",
            f"HTTP {s2}，viaHermes={j2.get('viaHermes')}")
    c.check(j2.get("hermes") is None, "没走中转时没有 hermes 段",
            json.dumps(j2.get("hermes"), ensure_ascii=False))


def assert_hermes_degraded(bridge_port: int, token: str, c: Checker) -> None:
    """Hermes 不可达时必须 503 且说清原因 —— 绝不静默直投。"""
    base = f"http://127.0.0.1:{bridge_port}"
    stamp = int(time.time())
    payload = {
        "text": "这是一段用于验证 Hermes 不可达时必须明确报错的物料描述文本，长度超过二十个字符。",
        "docType": "meeting", "ref": f"hermes-dead-{stamp}", "viaHermes": True,
    }
    status, body, _ = http(f"{base}/api/push/workbench", payload, token=token)
    detail = str(jbody(body).get("detail", ""))
    c.check(status == 503, "Hermes 不可达：返回 503（不是 500）", f"HTTP {status}")
    c.check("Hermes" in detail, "Hermes 不可达：提示里点名了 Hermes", detail[:160])
    c.check("直投" in detail, "Hermes 不可达：明说没有静默直投", detail[:200])

    # 失败必须撤销占坑：否则一次 Hermes 抖动就把这段素材永久标记成「已推送」，
    # 而工作台其实什么都没收到，素材从此再也不会被推进。
    payload2 = dict(payload)
    payload2["viaHermes"] = False
    s2, b2, _ = http(f"{base}/api/push/workbench", payload2, token=token)
    j2 = jbody(b2)
    c.check(s2 == 200 and j2.get("duplicate") is False,
            "Hermes 失败后同一 ref 还能重推（占坑已撤销，没被永久标记成已推送）",
            f"HTTP {s2}，duplicate={j2.get('duplicate')}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-port", type=int, default=SRC_BRIDGE_PORT)
    ap.add_argument("--keep", action="store_true", help="跑完不关掉脚本拉起的实例")
    ap.add_argument("--reuse", action="store_true",
                    help="端口上已有源码实例就复用它（默认总是拉一个全新的，"
                         "避免拿旧代码跑出假绿）")
    ap.add_argument("--python", default="", help="跑服务端源码的 python 路径")
    args = ap.parse_args()

    print("=" * 68)
    print("  数字人服务 · TTS 合成 / 工作台推送 自测")
    print("=" * 68)
    print(f"  本次用的是源码实例({args.base_port})，生产实例(8767) 未被触碰。")
    print("  源码实例 = 本仓库 server/ 的代码，端口 8776/8777，独立 DATA_DIR。")
    print("=" * 68)

    c = Checker()
    py, py_env = find_python(args.python)
    print(f"  解释器：{py}")
    print(f"  源码目录：{SERVER_DIR}")

    tmp_root = SERVER_DIR / "data" / "src-instance"
    procs: list[subprocess.Popen] = []
    try:
        # ---- 假 Hermes：门禁自带，不依赖本机那个常驻进程 ----
        fake_hermes = start_fake_hermes(FAKE_HERMES_PORT, tmp_root / "fake-hermes", py)
        procs.append(fake_hermes)
        if not port_open(FAKE_HERMES_PORT):
            c.check(False, "假 Hermes 能启动",
                    f"见 {tmp_root / 'fake-hermes' / 'fake-hermes.log'}")

        # ---- 假工作台：门禁自带，绝不打真实工作台 ----
        # 真实工作台在生产库里会留下抽取作业与审计，跑一次门禁脏一次库。
        fake_wb = start_fake_workbench(FAKE_WORKBENCH_PORT, tmp_root / "fake-workbench", py)
        procs.append(fake_wb)
        if not port_open(FAKE_WORKBENCH_PORT):
            c.check(False, "假工作台能启动",
                    f"见 {tmp_root / 'fake-workbench' / 'fake-workbench.log'}")
        wb_override = {
            "DH_WORKBENCH_BASE_URL": f"http://127.0.0.1:{FAKE_WORKBENCH_PORT}",
            "DH_WORKBENCH_API_KEY": "fake-workbench-key",
            "DH_WORKBENCH_TENANT_ID": "00000000-0000-0000-0000-000000000001",
        }

        # ---- 实例 A：正常配置，跑功能断言 ----
        print("\n[0] 准备源码实例")
        data_dir = tmp_root / "main"
        proc_a: subprocess.Popen | None = None
        if args.reuse and port_open(args.base_port):
            print(f"  复用已在运行的源码实例 :{args.base_port}")
        else:
            # 收掉上一轮的残留：复用旧实例等于拿旧代码跑断言（假绿）。
            kill_stale(data_dir)
            wait_port_free(args.base_port, 30)
            proc_a = start_instance("main", SRC_HTTP_PORT, args.base_port,
                                    data_dir, None,
                                    {"DH_HERMES_BASE_URL": f"http://127.0.0.1:{FAKE_HERMES_PORT}",
                                     **wb_override},
                                    py, py_env)
            procs.append(proc_a)
            if not wait_ready(args.base_port):
                log = getattr(proc_a, "_dh_log", None)
                print(f"  源码实例没起来，日志见 {log}")
                c.check(False, "源码实例能启动", f"见 {log}")
                return c.summary()
            print(f"  已拉起源码实例 :{args.base_port}（日志 {getattr(proc_a, '_dh_log', '')}）")

        base = f"http://127.0.0.1:{args.base_port}"
        token = DEFAULT_TOKEN

        # agent token：agent 端点用独立的凭证（data/agent.token），与手机端 Token 分开。
        agent_token = ""
        for cand in (tmp_root / "main" / "agent.token",
                     SERVER_DIR / "data" / "agent.token"):
            if cand.is_file():
                agent_token = cand.read_text(encoding="utf-8").strip()
                break

        assert_regression(base, token, agent_token, c)
        assert_tts_synth(base, token, c)
        conf = workbench_conf(wb_override)
        assert_push(base, token, c, conf)
        assert_hermes_via(base, token, c, conf)

        # ---- 实例 B：没配工作台 → 503 ----
        print("\n[4] 降级：工作台未配置")
        bad_env = tmp_root / "badcfg.env"
        bad_env.parent.mkdir(parents=True, exist_ok=True)
        bad_env.write_text("", encoding="utf-8")   # 空配置 = 什么工作台配置都没有
        kill_stale(tmp_root / "badcfg")
        wait_port_free(BADCFG_BRIDGE_PORT, 30)
        proc_b = start_instance("badcfg", SRC_HTTP_PORT + 2, BADCFG_BRIDGE_PORT,
                                tmp_root / "badcfg", bad_env, {}, py, py_env)
        procs.append(proc_b)
        if wait_ready(BADCFG_BRIDGE_PORT):
            assert_degraded(BADCFG_BRIDGE_PORT, token, c, "未配置",
                            "DH_WORKBENCH_API_KEY")
        else:
            c.check(False, "降级实例能启动", "见 src-instance-badcfg.log")
        stop_instance(proc_b)
        procs.remove(proc_b)

        # ---- 实例 C：基址指向死端口 → 503 ----
        print("\n[5] 降级：工作台基址不可达")
        dead_env = tmp_root / "deadurl.env"
        dead_port = 9      # 丢弃端口：没人监听
        dead_env.write_text(
            "export DH_WORKBENCH_BASE_URL=\"http://127.0.0.1:%d\"\n"
            "export DH_WORKBENCH_API_KEY=\"dummy-key\"\n"
            "export DH_WORKBENCH_TENANT_ID=\"00000000-0000-0000-0000-000000000000\"\n"
            % dead_port, encoding="utf-8")
        kill_stale(tmp_root / "deadurl")
        wait_port_free(DEADURL_BRIDGE_PORT, 30)
        proc_c = start_instance("deadurl", SRC_HTTP_PORT + 4, DEADURL_BRIDGE_PORT,
                                tmp_root / "deadurl", dead_env, {}, py, py_env)
        procs.append(proc_c)
        if wait_ready(DEADURL_BRIDGE_PORT):
            assert_degraded(DEADURL_BRIDGE_PORT, token, c, "基址不可达", "连不上")
        else:
            c.check(False, "降级实例能启动", "见 src-instance-deadurl.log")
        stop_instance(proc_c)
        procs.remove(proc_c)

        # ---- 实例 D：Hermes 不可达 → 503（绝不静默直投） ----
        print("\n[7] 降级：Hermes 不可达")
        hd_env = tmp_root / "hermesdead.env"
        hd_env.parent.mkdir(parents=True, exist_ok=True)
        hd_env.write_text(
            "export DH_WORKBENCH_BASE_URL=\"%s\"\n"
            "export DH_WORKBENCH_API_KEY=\"%s\"\n"
            "export DH_WORKBENCH_TENANT_ID=\"%s\"\n"
            "export DH_HERMES_BASE_URL=\"http://127.0.0.1:9\"\n"
            % (conf.get("DH_WORKBENCH_BASE_URL") or "http://127.0.0.1:3080",
               conf.get("DH_WORKBENCH_API_KEY") or "dummy-key",
               conf.get("DH_WORKBENCH_TENANT_ID") or "00000000-0000-0000-0000-000000000000"),
            encoding="utf-8")
        kill_stale(tmp_root / "hermesdead")
        wait_port_free(HERMES_DEAD_BRIDGE_PORT, 30)
        proc_d = start_instance("hermesdead", SRC_HTTP_PORT + 6, HERMES_DEAD_BRIDGE_PORT,
                                tmp_root / "hermesdead", hd_env, {}, py, py_env)
        procs.append(proc_d)
        if wait_ready(HERMES_DEAD_BRIDGE_PORT):
            assert_hermes_degraded(HERMES_DEAD_BRIDGE_PORT, token, c)
        else:
            c.check(False, "降级实例能启动", "见 src-instance-hermesdead.log")
        stop_instance(proc_d)
        procs.remove(proc_d)

        # ---- 门禁隔离自证：请求确实进了假工作台，没进真实工作台 ----
        # 判据落在数据上：假工作台自己记的 received.jsonl。
        # 采集端要是打去了真实地址，这个文件就是空的，断言立刻红。
        print("\n[8] 门禁隔离（不打真实工作台）")
        recv_path = tmp_root / "fake-workbench" / "received.jsonl"
        lines: list[dict] = []
        if recv_path.is_file():
            for ln in recv_path.read_text(encoding="utf-8").splitlines():
                if ln.strip():
                    try:
                        lines.append(json.loads(ln))
                    except ValueError:
                        pass
        c.check(len(lines) >= 3,
                "推送请求全部进了假工作台（没有打到真实工作台）",
                f"假工作台收到 {len(lines)} 条请求")
        c.check(all(x.get("authorized") for x in lines),
                "每次推送都带了机器凭证",
                f"带凭证 {sum(1 for x in lines if x.get('authorized'))}/{len(lines)} 条")
        c.check(all(x.get("tenant") for x in lines),
                "每次推送都带了工作台租户头",
                f"带租户头 {sum(1 for x in lines if x.get('tenant'))}/{len(lines)} 条")
        c.check(conf.get("DH_WORKBENCH_BASE_URL") == f"http://127.0.0.1:{FAKE_WORKBENCH_PORT}",
                "门禁配置指向假工作台（不是 env.local 里的真实地址）",
                str(conf.get("DH_WORKBENCH_BASE_URL")))

    finally:
        if not args.keep:
            for p in procs:
                stop_instance(p)
        else:
            print("\n（--keep：保住了脚本拉起的实例）")

    return c.summary()


if __name__ == "__main__":
    sys.exit(main())

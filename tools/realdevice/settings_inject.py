#!/usr/bin/env python3
"""生成 AndroidX Preferences DataStore 的 settings.preferences_pb 并注入真机。

用途：绕开 adb input text 在中文输入法下把 '.' 打成 '。' 的问题，
直接把 Mac IP / 端口 / Token 写进 App 的 DataStore。

protobuf 结构（androidx.datastore.preferences）：
    message PreferenceMap { map<string, Value> preferences = 1; }
    message Value {
      oneof value {
        bool   boolean_value = 1;
        float  float_value   = 2;
        int32  integer_value = 3;
        int64  long_value    = 4;
        string string_value  = 5;
        double double_value  = 6;
        bytes  bytes_value   = 8;
      }
    }
map entry 编码为：{1:key(string)} + {2:Value(message)}
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path


def varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def tag(field: int, wire: int) -> bytes:
    return varint((field << 3) | wire)


def s_field(field: int, payload: bytes) -> bytes:
    return tag(field, 2) + varint(len(payload)) + payload


def v_str(field: int, s: str) -> bytes:
    return s_field(field, s.encode("utf-8"))


def v_int(field: int, n: int) -> bytes:
    return tag(field, 0) + varint(n)


def v_bool(field: int, b: bool) -> bytes:
    return tag(field, 0) + varint(1 if b else 0)


# Value 的 oneof 字段号
V_BOOL, V_INT, V_STR = 1, 3, 5


def entry(key: str, value: bytes) -> bytes:
    return tag(1, 2) + varint(
        len(v_str(1, key)) + len(s_field(2, value))
    ) + v_str(1, key) + s_field(2, value)


def build(mac_ip: str, mac_port: int, token: str, bt_mac: str = "",
          process_minute: int = 1050, retention_days: int = 7,
          max_storage_mb: int = 0, probe_interval: int = 30,
          paused: bool = False) -> bytes:
    entries = [
        entry("mac_ip", v_str(V_STR, mac_ip)),
        entry("mac_port", v_int(V_INT, mac_port)),
        entry("token", v_str(V_STR, token)),
        entry("bt_mac", v_str(V_STR, bt_mac)),
        entry("process_minute", v_int(V_INT, process_minute)),
        entry("retention_days", v_int(V_INT, retention_days)),
        entry("max_storage_mb", v_int(V_INT, max_storage_mb)),
        entry("probe_interval", v_int(V_INT, probe_interval)),
        entry("paused", v_bool(V_BOOL, paused)),
    ]
    return b"".join(tag(1, 2) + varint(len(e)) + e for e in entries)


def sh(args: list[str]) -> tuple[int, str, str]:
    r = subprocess.run(args, capture_output=True, text=True)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", required=True, help="adb 设备序列号")
    ap.add_argument("--mac-ip", required=True)
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--token", default="")
    ap.add_argument("--probe-interval", type=int, default=30)
    ap.add_argument("--pkg", default="com.symi.digitalhuman")
    a = ap.parse_args()

    adb = ["adb", "-s", a.serial]
    rc, out, err = sh(adb + ["shell", "id", "-u"])
    if rc != 0:
        print(f"[FAIL] adb 连接失败: {err}")
        return 1

    blob = build(a.mac_ip, a.port, a.token, probe_interval=a.probe_interval)
    tmp = Path(tempfile.mkdtemp()) / "settings.preferences_pb"
    tmp.write_bytes(blob)
    print(f"[1] 生成 DataStore 快照 {len(blob)} 字节  mac_ip={a.mac_ip} port={a.port} "
          f"token={'<空>' if not a.token else '***'}")

    remote = f"/data/local/tmp/settings.preferences_pb"
    rc, _, err = sh(adb + ["push", str(tmp), remote])
    if rc != 0:
        print(f"[FAIL] push 失败: {err}")
        return 1
    print(f"[2] 已推送到 {remote}")

    # debug 包可 run-as；release 包需要 root 或走 UI 输入
    # 注意：adb shell 会把 && / 引号拆参，必须逐条单命令执行
    rel = f"/data/data/{a.pkg}/files/datastore/settings.preferences_pb"
    steps = [
        ["run-as", a.pkg, "mkdir", "-p", f"/data/data/{a.pkg}/files/datastore"],
        ["run-as", a.pkg, "sh", "-c", f"cat {remote} > {rel}"],
        ["run-as", a.pkg, "chmod", "600", rel],
        ["run-as", a.pkg, "ls", "-l", rel],
    ]
    for i, st in enumerate(steps, start=3):
        rc, out, err = sh(adb + ["shell"] + st)
        if rc != 0:
            print(f"[{i}] FAIL {' '.join(st)}\n     {err.strip()[:300]}")
            if i == 3:
                print("    run-as 不可用（release 包 debuggable=false 或系统限制）。")
                print("    备选：先装 debug 包注入配置，再 adb install -r 覆盖装 release（数据保留）。")
            return 2
        if out:
            print(f"[{i}] {out}")

    print(f"[6] 注入完成：{a.pkg} 的 mac_ip={a.mac_ip}:{a.port}")
    print("    下一步：彻底停止 App 进程后重启，DataStore 才会重新读取文件。")
    sh(adb + ["shell", "am", "force-stop", a.pkg])
    print("    已执行 force-stop。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

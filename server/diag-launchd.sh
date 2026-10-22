#!/bin/bash
# launchd EX_CONFIG 一次性诊断 —— 终端直接执行： bash ~/dh-server/diag-launchd.sh
# 跑完把全部输出发回即可，脚本最后会自动清理测试任务与临时文件
set -u
OUT=/tmp/dh_diag
LA="$HOME/Library/LaunchAgents"
U=$(id -u)
rm -rf "$OUT"; mkdir -p "$OUT"

echo "=================================================="
echo "[0] launchd 系统日志里的真实 spawn 错误（最近 30 分钟）"
echo "=================================================="
log show --last 30m --predicate 'process == "launchd"' --style compact 2>/dev/null \
  | grep -iE "digitalhuman|digital-human|dh-server|EX_CONFIG|posix_spawn|start-all" \
  | tail -15
echo "（若上面为空，用这条扩展搜索：）"
log show --last 30m --predicate 'senderImagePath CONTAINS "launchd"' --style compact 2>/dev/null | grep -iE "spawn|config" | tail -10

# ---------- 生成探针脚本：执行后往 /tmp/dh_diag/<标记> 写一行 ----------
mk_probe() { # $1=脚本路径 $2=标记名
  printf '#!/bin/bash\necho ok >> "%s/%s"\n' "$OUT" "$2" > "$1"
  chmod +x "$1"
}

# ---------- 生成 plist：$1=输出路径 $2=label $3=程序 $4=WorkingDirectory(可空) $5=StdoutPath(可空) ----------
mk_plist() {
  local wd="" so=""
  [ -n "$4" ] && wd="<key>WorkingDirectory</key><string>$4</string>"
  [ -n "$5" ] && so="<key>StandardOutPath</key><string>$5</string><key>StandardErrorPath</key><string>$5</string>"
  cat > "$1" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>$2</string>
<key>ProgramArguments</key><array><string>/bin/bash</string><string>$3</string></array>
<key>RunAtLoad</key><false/>
<key>KeepAlive</key><false/>
$wd
$so
</dict></plist>
EOF
}

# ---------- 执行单个测试：$1=label $2=plist $3=标记名 ----------
run_test() {
  launchctl bootout "gui/$U/$1" 2>/dev/null
  launchctl load "$2" >/dev/null 2>&1
  launchctl kickstart "gui/$U/$1" 2>/dev/null
  sleep 4
  local info ran
  info=$(launchctl print "gui/$U/$1" 2>/dev/null | grep -E "last exit|state =" | head -2 | tr '\n' ' ')
  [ -f "$OUT/$3" ] && ran="已执行" || ran="未执行"
  printf '%-42s %s | 探针: %s\n' "$1:" "${info:-（任务未注册）}" "$ran"
  launchctl bootout "gui/$U/$1" 2>/dev/null
}

echo ""
echo "=================================================="
echo "[1] 对照实验矩阵"
echo "=================================================="
mk_probe "/tmp/dh_diag/t1.sh" t1
mk_probe "/tmp/dh_diag/t2.sh" t2
mk_probe "/tmp/dh_diag/t3.sh" t3
mk_probe "$HOME/dh-server/.diag_t4.sh" t4

mk_plist "$LA/dh.t1.plist" dh.t1 "/tmp/dh_diag/t1.sh" "" "/tmp/dh_diag/t1.log"
mk_plist "$LA/dh.t2.plist" dh.t2 "/tmp/dh_diag/t2.sh" "$HOME/dh-server" "/tmp/dh_diag/t2.log"
mk_plist "$LA/dh.t3.plist" dh.t3 "/tmp/dh_diag/t3.sh" "" "$HOME/dh-server/data/diag_t3.log"
mk_plist "$LA/dh.t4.plist" dh.t4 "$HOME/dh-server/.diag_t4.sh" "" "/tmp/dh_diag/t4.log"

echo "T1 全内部盘（基线）                          应成功"
run_test dh.t1 "$LA/dh.t1.plist" t1
echo "T2 + WorkingDirectory 指向外置卷符号链接     测 chdir"
run_test dh.t2 "$LA/dh.t2.plist" t2
echo "T3 + 日志文件写到外置卷 data/               测 launchd 开日志文件"
run_test dh.t3 "$LA/dh.t3.plist" t3
echo "T4 + 程序本体放在外置卷                     测 exec 外置卷脚本"
run_test dh.t4 "$LA/dh.t4.plist" t4

echo ""
echo "=================================================="
echo "[2] 清理"
echo "=================================================="
rm -f "$LA"/dh.t1.plist "$LA"/dh.t2.plist "$LA"/dh.t3.plist "$LA"/dh.t4.plist \
      "$HOME/dh-server/.diag_t4.sh"
launchctl remove dh.t1 dh.t2 dh.t3 dh.t4 2>/dev/null
echo "清理完成。测试残留仅在 /tmp/dh_diag（重启自动消失）"

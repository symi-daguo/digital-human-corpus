#!/bin/bash
# JNI + VAD 端到端回归探针
#
# 用途：在真机/模拟器上验证「ABI 拆分后原生库未缺失 + Silero VAD 可实际推理」，
#       避免升级 sherpa-onnx AAR、改 splits 配置或改 proguard 规则后，
#       到用户手上才发现 UnsatisfiedLinkError。
#
# 原理：用 app_process 起一个裸 ART 进程，把目标 APK 加进 classpath，
#       反射实例化 App 的 VAD 封装类，再驱动底层 com.k2fsa.sherpa.onnx.Vad 跑真实音频。
#
# 用法：
#   ./run.sh <apk路径> [设备序列号]
#   ./run.sh ../app/app/build/outputs/apk/release/app-arm64-v8a-release.apk emulator-5554
#
# 依赖：javac、Android SDK build-tools(d8、zipalign)、adb；测试音频自动用 say 生成。

set -euo pipefail

APK="${1:?用法: ./run.sh <apk路径> [设备序列号]}"
DEV_ARG=""
[ "${2:-}" != "" ] && DEV_ARG="-s $2"

HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$(mktemp -d)"
trap 'rm -rf "$OUT"' EXIT

PKG="com.symi.digitalhuman"
ANDROID_HOME="${ANDROID_HOME:-$HOME/Library/Android/sdk}"
D8="$(ls "$ANDROID_HOME"/build-tools/*/d8 2>/dev/null | sort -V | tail -1)"
[ -z "$D8" ] && { echo "未找到 d8，请设置 ANDROID_HOME"; exit 1; }

echo "== [1/6] 解析 ABI =="
ABI="$(adb $DEV_ARG shell getprop ro.product.cpu.abi | tr -d '\r')"
echo "设备 ABI = $ABI"
unzip -l "$APK" | grep -q "lib/$ABI/" || {
  echo "FAIL: APK 内不含 $ABI 的原生库"; unzip -l "$APK" | grep "lib/" ; exit 1; }
unzip -l "$APK" | grep "lib/$ABI/.*\.so$"

echo "== [2/6] 抽取原生库 =="
mkdir -p "$OUT/libs"
( cd "$OUT/libs" && unzip -qo "$APK" "lib/$ABI/*.so" )
adb $DEV_ARG shell "rm -rf /data/local/tmp/jnilib && mkdir -p /data/local/tmp/jnilib" >/dev/null
adb $DEV_ARG push "$OUT/libs/lib/$ABI/." /data/local/tmp/jnilib/ >/dev/null
echo "已推送 $(ls "$OUT/libs/lib/$ABI" | wc -l | tr -d ' ') 个 .so 到 /data/local/tmp/jnilib"

echo "== [3/6] 准备测试语音（16kHz 中文 TTS）=="
say -v Tingting -o "$OUT/vad.aiff" "今天天气很好，我们一起来测试语音活动检测的效果，这段话大约持续六秒钟"
afconvert -f WAVE -d LEI16@16000 -c 1 "$OUT/vad.aiff" "$OUT/vad16k.wav"
adb $DEV_ARG push "$OUT/vad16k.wav" /data/local/tmp/vadtest16k.wav >/dev/null
echo "测试音频已就绪"

echo "== [4/6] 确定 VAD 封装类名（release 会被 R8 重命名）=="
CLS="com.symi.digitalhuman.audio.SherpaVad"
MAP="$(dirname "$APK")/../../mapping/release/mapping.txt"
if [ -f "$MAP" ]; then
  OBF="$(grep -m1 "^com\.symi\.digitalhuman\.audio\.SherpaVad ->" "$MAP" | sed 's/.*-> //;s/://')"
  [ -n "$OBF" ] && CLS="$OBF"
fi
echo "目标类 = $CLS"

echo "== [5/6] 编译探针 =="
javac -d "$OUT/classes" "$HERE/VadRel.java" "$HERE/JniProbe.java"
( cd "$OUT/classes" && "$D8" --output . VadRel.class JniProbe.class )
( cd "$OUT/classes" && zip -q "$OUT/probe.zip" classes.dex )
adb $DEV_ARG push "$OUT/probe.zip" /data/local/tmp/probe.zip >/dev/null

echo "== [6/6] 运行 =="
CODEPATH="$(adb $DEV_ARG shell dumpsys package $PKG | grep -m1 'codePath=' | sed 's/.*codePath=//' | tr -d '\r')"
[ -z "$CODEPATH" ] && { echo "FAIL: 设备上未安装 $PKG"; exit 1; }

echo "--- JNI 加载探针 ---"
adb $DEV_ARG shell "LD_LIBRARY_PATH=/data/local/tmp/jnilib /system/bin/app_process \
  -Xmx512m -Djava.library.path=/data/local/tmp/jnilib \
  -cp /data/local/tmp/probe.zip:$CODEPATH/base.apk /data/local/tmp JniProbe" 2>&1 | grep -v "cpuid_info"

echo "--- VAD 端到端探针 ---"
RESULT="$(adb $DEV_ARG shell "LD_LIBRARY_PATH=/data/local/tmp/jnilib /system/bin/app_process \
  -Xmx512m -Djava.library.path=/data/local/tmp/jnilib \
  -cp /data/local/tmp/probe.zip:$CODEPATH/base.apk /data/local/tmp VadRel \
  $CODEPATH/base.apk /data/local/tmp/vadtest16k.wav $CLS" 2>&1)"
echo "$RESULT" | grep -v "cpuid_info"

echo
if echo "$RESULT" | grep -q "RESULT=PASS"; then
  echo "=========================================="
  echo " 结论：PASS — 原生库完整，VAD 推理正常"
  echo "=========================================="
else
  echo "=========================================="
  echo " 结论：FAIL — 请检查上方输出"
  echo "=========================================="
  exit 1
fi

#!/usr/bin/env bash
# 一键启动虚拟串口对 + mock 设备 + GUI，用于无真机端到端测试。
# 依赖: socat, python3 + pyserial + tkinter
set -u

cd "$(dirname "$0")"
PY=python3

# 1) 建虚拟串口对
SOCAT_OUT=$(mktemp)
socat -d -d pty,raw,echo=0 pty,raw,echo=0 >"$SOCAT_OUT" 2>&1 &
SOCAT_PID=$!
sleep 0.6
# 解析出两个 pts 设备名
PORTS=$(grep -oE '/dev/pts/[0-9]+' "$SOCAT_OUT" | sort -u)
P1=$(echo "$PORTS" | sed -n '1p')
P2=$(echo "$PORTS" | sed -n '2p')
if [ -z "$P1" ] || [ -z "$P2" ]; then
  echo "无法解析虚拟串口对，socat 输出:"
  cat "$SOCAT_OUT"
  kill "$SOCAT_PID" 2>/dev/null
  exit 1
fi
echo "虚拟串口对:  mock设备 <- $P1    GUI <- $P2"

cleanup() {
  echo
  echo "退出中..."
  kill "$SOCAT_PID" 2>/dev/null
  pkill -f "mock_device.py $P1" 2>/dev/null
  wait 2>/dev/null
}
trap cleanup EXIT INT TERM

# 2) 启动 mock 设备
$PY mock_device.py "$P1" 115200 &
MOCK_PID=$!
sleep 0.4

# 3) 启动 GUI（提示用户在串口框里填 $P2）
echo "请在 GUI 串口框填入: $P2   波特率: 115200   然后点「连接」"
$PY app.py

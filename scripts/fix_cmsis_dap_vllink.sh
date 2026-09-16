#!/bin/bash
# Vllink CMSIS-DAP 驱动一键修复：udev 规则 + 权限 + ModemManager 排除
# 适用场景：Vllink Basic2 无线 DAP 调试器(VID 1209 PID 6666)插入后，
#   - hidraw 调试接口权限只有 root，非 root 用户无法用 OpenOCD/pyOCD 调试
#   - CDC ACM 串口被 ModemManager 抢占导致 AT 命令干扰
#   - serial_debug 工具无法自动识别该设备
# 使用：sudo bash fix_cmsis_dap_vllink.sh
set -e

VID="1209"
PID="6666"
RULE_FILE="/etc/udev/rules.d/99-cmsis-dap-vllink.rules"

echo "==> 1. 确认设备存在"
if ! lsusb | grep -q "${VID}:${PID}"; then
    echo "  警告：未检测到 ${VID}:${PID} 设备，请确认 Vllink DAP 已插入"
    echo "  仍会写入 udev 规则，下次插入自动生效"
else
    echo "  设备已检测到：$(lsusb | grep ${VID}:${PID})"
fi

echo "==> 2. 写入 udev 规则"
cat > "$RULE_FILE" << 'EOF'
# Vllink Basic2 CMSIS-DAP 无线调试器 (VID 1209 PID 6666)
# 制造商：ARM，产品名：DAPLink CMSIS-DAP，序列号前缀：Vllink.Basic2
# 设备暴露 5 个接口：CDC ACM 串口 + HID CMSIS-DAP 调试 + 其他

# (1) USB 设备权限：非 root 用户可读写
SUBSYSTEM=="usb", ATTRS{idVendor}=="1209", ATTRS{idProduct}=="6666", MODE="0666", GROUP="plugdev"

# (2) hidraw 调试接口权限：OpenOCD/pyOCD 通过 hidraw 访问 CMSIS-DAP
KERNEL=="hidraw*", SUBSYSTEM=="hidraw", ATTRS{idVendor}=="1209", ATTRS{idProduct}=="6666", MODE="0666", GROUP="plugdev"

# (3) CDC ACM 串口权限：serial_debug / 串口工具使用
SUBSYSTEM=="tty", ATTRS{idVendor}=="1209", ATTRS{idProduct}=="6666", MODE="0666", GROUP="dialout"

# (4) 排除 ModemManager：防止 ModemManager 把串口当调制解调器发送 AT 命令
#     这是 CMSIS-DAP 设备的常见问题，ModemManager 会干扰调试器通信
SUBSYSTEM=="usb", ATTRS{idVendor}=="1209", ATTRS{idProduct}=="6666", ENV{ID_MM_DEVICE_IGNORE}="1"
SUBSYSTEM=="usb", ATTRS{idVendor}=="1209", ATTRS{idProduct}=="6666", ENV{ID_MM_PORT_TYPE}="1"
EOF
echo "  规则文件：$RULE_FILE"

echo "==> 3. 重新加载 udev 规则"
udevadm control --reload-rules
udevadm trigger --action=change --subsystem-match=usb
udevadm trigger --action=change --subsystem-match=hidraw
udevadm trigger --action=change --subsystem-match=tty

echo "==> 4. 验证设备状态"

# USB 设备权限
echo "  USB 设备："
USB_DEV=$(lsusb | grep "${VID}:${PID}" | head -1)
if [ -n "$USB_DEV" ]; then
    BUS=$(echo "$USB_DEV" | awk '{print $2}')
    DEVNUM=$(echo "$USB_DEV" | awk '{print $4}' | tr -d ':')
    USB_PATH="/dev/bus/usb/${BUS}/${DEVNUM}"
    echo "    $USB_PATH: $(ls -la $USB_PATH 2>/dev/null | awk '{print $1, $3, $4}')"
else
    echo "    未检测到设备"
fi

# hidraw 设备
echo "  hidraw 调试接口："
HIDRAW_FOUND=0
for hid in /dev/hidraw*; do
    [ -e "$hid" ] || continue
    HID_VID=$(udevadm info -q property "$hid" 2>/dev/null | grep ID_VENDOR_ID | cut -d= -f2)
    HID_PID=$(udevadm info -q property "$hid" 2>/dev/null | grep ID_MODEL_ID | cut -d= -f2)
    if [ "$HID_VID" = "$VID" ] && [ "$HID_PID" = "$PID" ]; then
        echo "    $hid: $(ls -la "$hid" | awk '{print $1, $3, $4}')"
        HIDRAW_FOUND=1
    fi
done
if [ "$HIDRAW_FOUND" = "0" ]; then
    echo "    未找到对应的 hidraw 设备（可能需要重新插拔）"
fi

# ttyACM 串口
echo "  CDC ACM 串口："
TTY_FOUND=0
for tty in /dev/ttyACM*; do
    [ -e "$tty" ] || continue
    TTY_VID=$(udevadm info -q property "$tty" 2>/dev/null | grep ID_VENDOR_ID | cut -d= -f2)
    TTY_PID=$(udevadm info -q property "$tty" 2>/dev/null | grep ID_MODEL_ID | cut -d= -f2)
    if [ "$TTY_VID" = "$VID" ] && [ "$TTY_PID" = "$PID" ]; then
        echo "    $tty: $(ls -la "$tty" | awk '{print $1, $3, $4}')"
        echo "    by-id: $(ls /dev/serial/by-id/ 2>/dev/null | grep -i "DAPLink")"
        TTY_FOUND=1
    fi
done
if [ "$TTY_FOUND" = "0" ]; then
    echo "    未找到对应的 ttyACM 设备（可能需要重新插拔）"
fi

# ModemManager 排除验证
echo "  ModemManager 排除："
if udevadm info /dev/ttyACM* 2>/dev/null | grep -q "ID_MM_DEVICE_IGNORE=1"; then
    echo "    ✅ 已排除（ID_MM_DEVICE_IGNORE=1）"
else
    echo "    ⚠️ 未确认排除（可能需要重新插拔设备）"
fi

cat << 'EOF'

完成。如果权限未立即生效，请重新插拔 Vllink DAP 设备。

验证方法：
  ls -la /dev/hidraw*     # hidraw 权限应为 crw-rw-rw- (0666)
  ls -la /dev/ttyACM*     # ttyACM 权限应为 crw-rw-rw- (0666)
  openocd -f interface/cmsis-dap.cfg -c "transport select swd; adapter speed 4000; init; targets; exit"
  # 串口调试：serial_debug 工具中选 /dev/ttyACM2 (或 by-id: usb-ARM_DAPLink_CMSIS-DAP_*)

设备信息：
  VID:PID = 1209:6666
  制造商 = ARM
  产品名 = DAPLink CMSIS-DAP
  序列号 = Vllink.Basic2.xxxxxxxxxx
  接口 = CDC ACM 串口 + HID CMSIS-DAP 调试接口
  驱动 = cdc_acm (串口) + hid_generic (调试接口)
  无需额外内核驱动，Linux 内核自带 cdc_acm 和 hid_generic 即可
EOF

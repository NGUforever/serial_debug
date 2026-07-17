#!/bin/bash
# CH343 接口抢回 + udev 持久化（防止 cdc_acm 抢占）
# 使用：sudo bash fix_ch343_driver.sh
set -e

VID="1a86"
PID="55d5"

echo "==> 1. 解绑 cdc_acm 抢占的 CH343 接口，绑定到 ch343"
# 遍历所有 cdc_acm 绑定的接口，找出属于 CH343 (1a86:55d5) 的
for dev in /sys/bus/usb/drivers/cdc_acm/*:*; do
    [ -d "$dev" ] || continue
    devname=$(basename "$dev")
    # 向上找到 USB 设备节点
    usb_dev=$(readlink -f "$dev")
    # 在 usb_dev 路径中查找 idVendor/idProduct
    # 路径形如 /sys/devices/.../usb3/3-1/3-1.1/3-1.1:1.2
    # 找父设备的 idVendor
    parent=$(dirname "$usb_dev")
    while [ "$parent" != "/" ]; do
        if [ -f "$parent/idVendor" ]; then
            vid=$(cat "$parent/idVendor")
            pid=$(cat "$parent/idProduct")
            if [ "$vid" = "$VID" ] && [ "$pid" = "$PID" ]; then
                echo "  解绑 $devname (vid=$vid pid=$pid) 从 cdc_acm"
                echo "$devname" | tee /sys/bus/usb/drivers/cdc_acm/unbind > /dev/null || echo "  解绑失败，跳过"
                echo "  绑定 $devname 到 usb_ch343"
                echo "$devname" | tee /sys/bus/usb/drivers/usb_ch343/bind > /dev/null || echo "  绑定失败，跳过"
            fi
            break
        fi
        parent=$(dirname "$parent")
    done
done

echo "==> 2. 写入 udev driver_override 规则"
RULE_FILE="/etc/udev/rules.d/99-ch343-override.rules"
cat > "$RULE_FILE" << 'EOF'
# CH343 设备强制绑定到 ch343 驱动，防止 cdc_acm 抢占
# 在 cdc_acm 绑定前先设置 driver_override
SUBSYSTEM=="usb", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="55d5", DRIVER=="cdc_acm", \
  RUN+="/bin/sh -c 'echo usb_ch343 > /sys$DEVPATH/driver_override'"
# 覆盖 WCH 其他常见 CH343 variant PID（55d2/55d3/55d4/55d7/55d8/55db/55dd/55da/55de/55e7/55df/55e9/55ea/55e8/55eb/55ec）
SUBSYSTEM=="usb", ATTRS{idVendor}=="1a86", DRIVER=="cdc_acm", \
  RUN+="/bin/sh -c 'echo usb_ch343 > /sys$DEVPATH/driver_override'"
EOF
echo "  规则文件：$RULE_FILE"

echo "==> 3. 重新加载 udev"
udevadm control --reload-rules
udevadm trigger

echo "==> 4. 验证当前状态"
echo "ttyCH343USB 设备："
ls /dev/ttyCH343USB* 2>/dev/null || echo "  无"
echo "ttyACM 设备："
ls /dev/ttyACM* 2>/dev/null || echo "  无"
echo "usb_ch343 绑定接口："
ls /sys/bus/usb/drivers/usb_ch343/ | grep ":" || echo "  无"
echo "cdc_acm 绑定接口（应为非 CH343 设备）："
ls /sys/bus/usb/drivers/cdc_acm/ | grep ":" || echo "  无"

cat << 'EOF'

完成。重启电脑后应自动生效，4 个端口应都出现在 /dev/ttyCH343USB0-3。

若仍有问题，检查：
  dmesg | grep -iE "ch343|cdc_acm"
  cat /sys/bus/usb/devices/*/driver_override  # 查看哪些设备设置了 override

注意：第二条规则会对所有 1a86 vendor 的设备在 cdc_acm 抢占时触发 override。
若你有其他 WCH 非 CH343 设备，请删除第二条规则。
EOF

#!/bin/bash
# CH343 驱动一键修复：build + install + udev + rebind
# 适用场景：apt 升级内核后 ch343.ko 丢失（只装在旧内核下），
# 或 cdc_acm 抢占 CH343 接口。
# 使用：sudo bash fix_ch343_driver.sh
set -e

VID="1a86"
PID="55d5"
SRC_DIR="/tmp/ch343ser_linux"
KERNEL_VER=$(uname -r)

echo "==> 0. 确认 ch343 源码 + 内核 headers"
if [ ! -d "$SRC_DIR/driver" ]; then
    echo "  源码不在 $SRC_DIR，开始 git clone..."
    rm -rf "$SRC_DIR"
    git clone --depth 1 https://github.com/WCHSoftGroup/ch343ser_linux "$SRC_DIR"
fi
if [ ! -d "/lib/modules/$KERNEL_VER/build" ]; then
    echo "  错误：内核 headers 未装，请先：sudo apt install linux-headers-$KERNEL_VER"
    exit 1
fi

echo "==> 1. 编译 ch343.ko（针对当前内核 $KERNEL_VER）"
cd "$SRC_DIR/driver"
make clean >/dev/null 2>&1 || true
make -j"$(nproc)"

INSTALLED_KO=$(find /lib/modules/$KERNEL_VER -name "ch343.ko*" 2>/dev/null | head -1)
if [ -z "$INSTALLED_KO" ]; then
    echo "==> 2. 安装 ch343.ko 到 /lib/modules"
    make install
    depmod -a
else
    echo "==> 2. 已安装：$INSTALLED_KO（跳过安装）"
fi

echo "==> 3. 加载 ch343 模块"
modprobe -r ch343 2>/dev/null || true
modprobe ch343

echo "==> 4. 写入 udev driver_override 规则"
RULE_FILE="/etc/udev/rules.d/99-ch343-override.rules"
cat > "$RULE_FILE" << 'EOF'
# CH343 设备强制绑定到 ch343 驱动，防止 cdc_acm 抢占
# 在 cdc_acm 绑定前先设置 driver_override
SUBSYSTEM=="usb", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="55d5", DRIVER=="cdc_acm", \
  RUN+="/bin/sh -c 'echo usb_ch343 > /sys$DEVPATH/driver_override'"
# 覆盖 WCH 其他常见 CH343 variant PID
SUBSYSTEM=="usb", ATTRS{idVendor}=="1a86", DRIVER=="cdc_acm", \
  RUN+="/bin/sh -c 'echo usb_ch343 > /sys$DEVPATH/driver_override'"
EOF
echo "  规则文件：$RULE_FILE"

echo "==> 5. 重新加载 udev"
udevadm control --reload-rules
udevadm trigger

echo "==> 6. 解绑 cdc_acm 抢占的 CH343 接口，绑定到 usb_ch343"
for dev in /sys/bus/usb/drivers/cdc_acm/*:*; do
    [ -d "$dev" ] || continue
    devname=$(basename "$dev")
    usb_dev=$(readlink -f "$dev")
    parent=$(dirname "$usb_dev")
    while [ "$parent" != "/" ]; do
        if [ -f "$parent/idVendor" ]; then
            vid=$(cat "$parent/idVendor")
            pid=$(cat "$parent/idProduct")
            if [ "$vid" = "$VID" ] && [ "$pid" = "$PID" ]; then
                echo "  解绑 $devname 从 cdc_acm"
                echo "$devname" > /sys/bus/usb/drivers/cdc_acm/unbind 2>/dev/null || true
                echo "  绑定 $devname 到 usb_ch343"
                echo "$devname" > /sys/bus/usb/drivers/usb_ch343/bind 2>/dev/null || true
            fi
            break
        fi
        parent=$(dirname "$parent")
    done
done

# 兜底：对所有 1a86:55d5 接口写 driver_override + 触发 reprobe
for devpath in /sys/bus/usb/devices/*:*; do
    [ -f "$devpath/idVendor" ] || continue
    vid=$(cat "$devpath/idVendor" 2>/dev/null)
    pid=$(cat "$devpath/idProduct" 2>/dev/null)
    if [ "$vid" = "$VID" ] && [ "$pid" = "$PID" ]; then
        echo "usb_ch343" > "$devpath/driver_override" 2>/dev/null || true
    fi
done
udevadm trigger --action=change --subsystem-match=usb

echo "==> 7. 验证当前状态"
echo "ttyCH343USB 设备："
ls /dev/ttyCH343USB* 2>/dev/null || echo "  无"
echo "usb_ch343 绑定接口（应 8 个）："
ls /sys/bus/usb/drivers/usb_ch343/ 2>/dev/null | grep ":" || echo "  无"

cat << 'EOF'

完成。重启电脑后应自动生效，4 个端口应都出现在 /dev/ttyCH343USB0-3。

若仍有问题，检查：
  dmesg | grep -iE "ch343|cdc_acm"
  lsmod | grep ch343
  cat /sys/bus/usb/devices/*/driver_override  # 查看哪些设备设置了 override
EOF

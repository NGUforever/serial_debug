#!/bin/bash
# CH34x 驱动一键修复：build + install + udev + rebind
# 适用场景：apt 升级内核后 ch34x.ko 丢失（只装在旧内核下），
# 系统回退到自带 ch341 或 generic usb 驱动，CH340G/CH340T 出现
# /dev/ttyUSB* 消失、通信异常、延迟变大等问题。
# 使用：sudo bash fix_ch340_driver.sh
set -e

VID="1a86"
# CH340 系列常见 PID：7523=CH340G/CH340T，7522=CH340K，5523=CH340
PIDS=("7523" "7522" "5523")
# 本地源码路径（已下载并编译过），不存在则从 github clone
LOCAL_SRC="/home/ninebot/tools/install_packages/CH34x-Driver-Linux"
CLONE_URL="https://github.com/gorgiaxx/CH34x-Driver-Linux.git"
SRC_DIR="$LOCAL_SRC"
KERNEL_VER=$(uname -r)
DRIVER_NAME="ch34x"

echo "==> 0. 确认 ch34x 源码 + 内核 headers"
if [ ! -d "$LOCAL_SRC" ] || [ ! -f "$LOCAL_SRC/ch34x.c" ]; then
    SRC_DIR="/tmp/CH34x-Driver-Linux"
    echo "  本地源码 $LOCAL_SRC 不存在，clone 到 $SRC_DIR"
    rm -rf "$SRC_DIR"
    git clone --depth 1 "$CLONE_URL" "$SRC_DIR"
else
    echo "  使用本地源码：$LOCAL_SRC"
fi
if [ ! -d "/lib/modules/$KERNEL_VER/build" ]; then
    echo "  错误：内核 headers 未装，请先：sudo apt install linux-headers-$KERNEL_VER"
    exit 1
fi

echo "==> 1. 编译 ch34x.ko（针对当前内核 $KERNEL_VER）"
cd "$SRC_DIR"
make clean >/dev/null 2>&1 || true
make -j"$(nproc)"
if [ ! -f ch34x.ko ]; then
    echo "  错误：ch34x.ko 编译失败，检查 make 输出"
    exit 1
fi

echo "==> 2. 安装 ch34x.ko.xz 到 /lib/modules"
# Makefile 的 install 目标会原地 xz ch34x.ko 导致后续 make 重跑失败，手动装
xz -kf ch34x.ko
INSTALL_DIR="/lib/modules/$KERNEL_VER/kernel/drivers/usb/serial"
mkdir -p "$INSTALL_DIR"
cp ch34x.ko.xz "$INSTALL_DIR/ch34x.ko.xz"
depmod -a

echo "==> 3. 卸载冲突模块（ch341 / 旧 ch34x），加载 ${DRIVER_NAME}"
modprobe -r ch341 2>/dev/null || true
modprobe -r ch34x 2>/dev/null || true
# blacklist ch341 防止重启后又抢回去（ch341 alias 含 1a86:7523/7522/5523）
BLACKLIST_FILE="/etc/modprobe.d/ch340-override.conf"
if [ ! -f "$BLACKLIST_FILE" ]; then
    echo "  写入 blacklist：$BLACKLIST_FILE"
    cat > "$BLACKLIST_FILE" << 'EOF'
# CH340G 设备改用 WCH 官方 ch34x 驱动，禁用系统自带 ch341
blacklist ch341
EOF
fi
modprobe "$DRIVER_NAME"

echo "==> 4. 写入 udev driver_override 规则"
RULE_FILE="/etc/udev/rules.d/99-ch340-override.rules"
cat > "$RULE_FILE" << EOF
# CH340 设备强制绑定到 ${DRIVER_NAME} 驱动，防止 ch341 / generic usb 抢占
# ch341 的 alias 包含 1a86:7523/7522/5523，会自动加载抢设备，必须 override
EOF
for pid in "${PIDS[@]}"; do
    cat >> "$RULE_FILE" << EOF
SUBSYSTEM=="usb", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="${pid}", DRIVER!="${DRIVER_NAME}", \\
  RUN+="/bin/sh -c 'echo ${DRIVER_NAME} > /sys\$DEVPATH/driver_override'"
EOF
done
echo "  规则文件：$RULE_FILE"

echo "==> 5. 重新加载 udev"
udevadm control --reload-rules
udevadm trigger

echo "==> 6. 对所有 CH340 设备解绑当前驱动 + 写 driver_override + 绑 ch34x"
for devpath in /sys/bus/usb/devices/*:*; do
    [ -f "$devpath/idVendor" ] || continue
    vid=$(cat "$devpath/idVendor" 2>/dev/null)
    pid=$(cat "$devpath/idProduct" 2>/dev/null)
    is_match=0
    for want in "${PIDS[@]}"; do
        [ "$pid" = "$want" ] && is_match=1
    done
    if [ "$vid" = "$VID" ] && [ "$is_match" = "1" ]; then
        echo "  发现 $vid:$pid @ $devpath"
        cur_drv=$(readlink "$devpath/driver" 2>/dev/null | xargs basename 2>/dev/null)
        devname=$(basename "$devpath")
        if [ -n "$cur_drv" ] && [ "$cur_drv" != "$DRIVER_NAME" ]; then
            echo "    解绑 $devname 从 $cur_drv"
            echo "$devname" > /sys/bus/usb/drivers/"$cur_drv"/unbind 2>/dev/null || true
        fi
        echo "    写 driver_override = $DRIVER_NAME"
        echo "$DRIVER_NAME" > "$devpath/driver_override" 2>/dev/null || true
        echo "    绑定 $devname 到 $DRIVER_NAME"
        echo "$devname" > /sys/bus/usb/drivers/"$DRIVER_NAME"/bind 2>/dev/null || true
    fi
done
udevadm trigger --action=change --subsystem-match=usb

echo "==> 7. 验证当前状态"
echo "ttyUSB 设备："
ls /dev/ttyUSB* 2>/dev/null || echo "  无"
echo "${DRIVER_NAME} 绑定接口："
ls /sys/bus/usb/drivers/"$DRIVER_NAME"/ 2>/dev/null | grep ":" || echo "  无"
echo "lsmod："
lsmod | grep -E "ch34x|ch341" || echo "  无 ch34x/ch341"

cat << EOF

完成。重启电脑后应自动生效，CH340G 应出现在 /dev/ttyUSB0。

若仍有问题，检查：
  dmesg | grep -iE "ch34x|ch341|usb"
  lsmod | grep -E "ch34x|ch341"
  cat /sys/bus/usb/devices/*/driver_override
  udevadm info -a /dev/ttyUSB0  # 看当前绑定到哪个驱动
EOF

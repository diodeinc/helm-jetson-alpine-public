#!/bin/sh
set -eu

source_dir=/source
work_dir=/work
output_dir=/out
rootfs_dir=$work_dir/rootfs
l4t_dir=$work_dir/l4t
l4t_archive=/inputs/Jetson_Linux_R39.2.0_aarch64.tbz2
kernel_release=6.8.12-1021-tegra
rootfs_size=${HELM_ROOTFS_SIZE:-2G}

clean_directory()
{
    directory=$1
    case "$directory" in
        /work/*|/out) ;;
        *) echo "Refusing to clean unexpected directory: $directory" >&2; exit 70 ;;
    esac
    mkdir -p "$directory"
    find "$directory" -mindepth 1 -delete
}

echo "==> Preparing build directories"
clean_directory "$rootfs_dir"
clean_directory "$l4t_dir"
clean_directory "$output_dir"

echo "==> Extracting the Jetson Linux 39.2 kernel inputs"
tar -xjf "$l4t_archive" -C "$l4t_dir" \
    Linux_for_Tegra/kernel/Image \
    Linux_for_Tegra/kernel/kernel_supplements.tbz2 \
    Linux_for_Tegra/kernel/kernel_oot_modules.tbz2 \
    Linux_for_Tegra/kernel/nvidia_l4t_kernel_nvgpu.tbz2 \
    Linux_for_Tegra/kernel/dtb/tegra234-p3768-0000+p3767-0000-nv.dtb \
    Linux_for_Tegra/kernel/dtb/tegra234-p3768-0000+p3767-0001-nv.dtb \
    Linux_for_Tegra/kernel/dtb/tegra234-p3768-0000+p3767-0003-nv.dtb \
    Linux_for_Tegra/kernel/dtb/tegra234-p3768-0000+p3767-0004-nv.dtb \
    Linux_for_Tegra/kernel/dtb/tegra234-p3768-0000+p3767-0005-nv.dtb \
    Linux_for_Tegra/nv_tegra/l4t_deb_packages/nvidia-l4t-firmware_39.2.0-20260601141651_arm64.deb

l4t_root=$l4t_dir/Linux_for_Tegra

echo "==> Installing Alpine Linux 3.24 packages"
mkdir -p "$rootfs_dir/etc/apk"
cp -a /etc/apk/keys "$rootfs_dir/etc/apk/"
cat > "$rootfs_dir/etc/apk/repositories" <<'EOF'
https://dl-cdn.alpinelinux.org/alpine/v3.24/main
https://dl-cdn.alpinelinux.org/alpine/v3.24/community
EOF

packages=$(sed -e '/^[[:space:]]*#/d' -e '/^[[:space:]]*$/d' \
    "$source_dir/packages.txt" | tr '\n' ' ')
# The package names deliberately undergo word splitting here.
# shellcheck disable=SC2086
apk --root "$rootfs_dir" \
    --arch aarch64 \
    --initdb \
    --keys-dir "$rootfs_dir/etc/apk/keys" \
    --repositories-file "$rootfs_dir/etc/apk/repositories" \
    add $packages

echo "==> Adding NVIDIA kernel modules and firmware"
tar -xjf "$l4t_root/kernel/kernel_supplements.tbz2" -C "$rootfs_dir"

oot_dir=$work_dir/oot-modules
clean_directory "$oot_dir"
tar -xjf "$l4t_root/kernel/kernel_oot_modules.tbz2" -C "$oot_dir"
mkdir -p "$rootfs_dir/lib/modules"
cp -a "$oot_dir/usr/lib/modules/." "$rootfs_dir/lib/modules/"

tar -xjf "$l4t_root/kernel/nvidia_l4t_kernel_nvgpu.tbz2" -C "$rootfs_dir"
dpkg-deb -x \
    "$l4t_root/nv_tegra/l4t_deb_packages/nvidia-l4t-firmware_39.2.0-20260601141651_arm64.deb" \
    "$rootfs_dir"

# The firmware package contains one systemd-only Bluetooth override. Alpine
# uses OpenRC/eudev, so retain the firmware and discard that foreign unit.
if [ -d "$rootfs_dir/lib/systemd" ]; then
    find "$rootfs_dir/lib/systemd" -mindepth 1 -delete
    rmdir "$rootfs_dir/lib/systemd"
fi

depmod -b "$rootfs_dir" "$kernel_release"

echo "==> Installing the Helm root filesystem overlay"
cp -a "$source_dir/rootfs-overlay/." "$rootfs_dir/"
mkdir -p "$rootfs_dir/boot/extlinux" "$rootfs_dir/etc/mkinitfs"
install -m 0644 "$source_dir/mkinitfs.conf" \
    "$rootfs_dir/etc/mkinitfs/mkinitfs.conf"
install -m 0644 "$source_dir/extlinux.conf" \
    "$rootfs_dir/boot/extlinux/extlinux.conf"
install -m 0644 "$l4t_root/kernel/Image" "$rootfs_dir/boot/Image"

chmod 0755 \
    "$rootfs_dir/etc/init.d/helm-peripherals" \
    "$rootfs_dir/usr/sbin/helm-console-login" \
    "$rootfs_dir/usr/sbin/helm-info" \
    "$rootfs_dir/usr/sbin/helm-led" \
    "$rootfs_dir/usr/sbin/helm-peripherals"
chmod 0600 "$rootfs_dir/etc/dropbear/dropbear.conf"

mkdir -p "$rootfs_dir/root/.ssh"
chmod 0700 "$rootfs_dir/root/.ssh"
if [ -r /run/helm_authorized_keys ]; then
    install -m 0600 /run/helm_authorized_keys \
        "$rootfs_dir/root/.ssh/authorized_keys"
else
    : > "$rootfs_dir/root/.ssh/authorized_keys"
    chmod 0600 "$rootfs_dir/root/.ssh/authorized_keys"
fi

: > "$rootfs_dir/etc/machine-id"
: > "$rootfs_dir/etc/resolv.conf"

echo "==> Enabling the minimal OpenRC service set"
for runlevel in sysinit boot default shutdown; do
    mkdir -p "$rootfs_dir/etc/runlevels/$runlevel"
done

enable_service()
{
    service=$1
    runlevel=$2
    if [ ! -e "$rootfs_dir/etc/init.d/$service" ]; then
        echo "Missing OpenRC service: $service" >&2
        exit 70
    fi
    ln -s "/etc/init.d/$service" "$rootfs_dir/etc/runlevels/$runlevel/$service"
}

for service in devfs dmesg procfs sysfs udev udev-trigger; do
    enable_service "$service" sysinit
done
for service in modules hwdrivers sysctl hostname bootmisc fsck root localmount seedrng swclock; do
    enable_service "$service" boot
done
for service in udev-postmount syslog dhcpcd chronyd dropbear helm-peripherals local; do
    enable_service "$service" default
done
for service in mount-ro killprocs savecache; do
    enable_service "$service" shutdown
done

echo "==> Compiling and validating the Helm device trees"
dtb_dir=$rootfs_dir/boot/dtb
mkdir -p "$dtb_dir"
dtc -@ -I dts -O dtb \
    -o "$dtb_dir/helm-p3768.dtbo" \
    "$source_dir/device-tree/helm-p3768.dtso"

for sku in 0000 0001 0003 0004 0005; do
    base_dtb=$l4t_root/kernel/dtb/tegra234-p3768-0000+p3767-$sku-nv.dtb
    helm_dtb=$dtb_dir/helm-p3767-$sku.dtb

    fdtoverlay -i "$base_dtb" -o "$helm_dtb" "$dtb_dir/helm-p3768.dtbo"

    # These deletions make the fully merged DTBs exact for Helm. The dynamic
    # overlay remains available for firmware-selected SKU booting.
    fdtput -r "$helm_dtb" "/bus@0/i2c@3160000/eeprom@57" || :
    fdtput -r "$helm_dtb" "/bus@0/i2c@c240000/fusb301@25" || :
    fdtput -r "$helm_dtb" "/gpio-keys/key-power" || :
    fdtput -r "$helm_dtb" "/bus@0/padctl@3520000/ports/usb2-0/port" || :
    fdtput -d "$helm_dtb" "/bus@0/padctl@3520000/ports/usb2-0" usb-role-switch || :
    fdtput -d "$helm_dtb" "/regulator-vdd-3v3-pcie" gpio || :
    fdtput -d "$helm_dtb" "/regulator-vdd-3v3-pcie" enable-active-high || :
    fdtput -t s "$helm_dtb" "/regulator-vdd-3v3-pcie" status disabled

    test "$(fdtget -t s "$helm_dtb" / model)" = \
        "Diode Helm with NVIDIA Jetson Orin NX/Nano"
    test "$(fdtget -t s "$helm_dtb" \
        /bus@0/padctl@3520000/ports/usb3-1 status)" = "okay"
    test "$(fdtget -t i "$helm_dtb" \
        /bus@0/padctl@3520000/ports/usb3-1 nvidia,usb2-companion)" = "1"
    test "$(fdtget -t i "$helm_dtb" \
        /bus@0/padctl@3520000/ports/usb3-2 nvidia,usb2-companion)" = "2"
    test "$(fdtget -t s "$helm_dtb" \
        /helm-leds/green-status label)" = "helm:green:status"
    test "$(fdtget -t s "$helm_dtb" \
        /bus@0/pcie@140a0000 status)" = "okay"
    test "$(fdtget -t x "$helm_dtb" \
        /bus@0/pcie@140a0000 vpcie3v3-supply)" = \
        "$(fdtget -t x "$helm_dtb" /helm-vdd-3v3-pcie phandle)"
    test "$(fdtget -t s "$helm_dtb" \
        /regulator-vdd-3v3-pcie status)" = "disabled"
done

echo "==> Generating the Alpine initramfs"
mkdir -p /lib/modules
ln -s "$rootfs_dir/lib/modules/$kernel_release" "/lib/modules/$kernel_release"
mkinitfs \
    -b "$rootfs_dir/" \
    -c "$rootfs_dir/etc/mkinitfs/mkinitfs.conf" \
    -C gzip \
    -P "$rootfs_dir/etc/mkinitfs/features.d" \
    -o "$rootfs_dir/boot/initramfs-helm" \
    "$kernel_release"
unlink "/lib/modules/$kernel_release"

gzip -dc "$rootfs_dir/boot/initramfs-helm" \
    | cpio -t > "$work_dir/initramfs-files.txt" 2>/dev/null
grep -q '/pcie-tegra194\.ko$' "$work_dir/initramfs-files.txt"
grep -q '/nvme\.ko$' "$work_dir/initramfs-files.txt"
grep -q '/phy-tegra194-p2u\.ko$' "$work_dir/initramfs-files.txt"

cat > "$rootfs_dir/etc/helm-release" <<EOF
HELM_OS=Alpine
ALPINE_BRANCH=v3.24
L4T_RELEASE=39.2
KERNEL_RELEASE=$kernel_release
CARRIER=Helm
EOF

echo "==> Removing package caches and recording the manifest"
find "$rootfs_dir/var/cache/apk" -type f -delete 2>/dev/null || :
apk --root "$rootfs_dir" info -vv | sort > "$output_dir/packages.txt"
cp "$rootfs_dir/etc/helm-release" "$output_dir/helm-release"

echo "==> Building boot and root filesystem artifacts"
alpine_version=$(cat "$rootfs_dir/etc/alpine-release")
artifact_prefix=helm-alpine-$alpine_version-l4t-r39.2

tar --numeric-owner --zstd \
    -cf "$output_dir/$artifact_prefix-rootfs.tar.zst" \
    -C "$rootfs_dir" .
tar --numeric-owner --zstd \
    -cf "$output_dir/$artifact_prefix-boot.tar.zst" \
    -C "$rootfs_dir" boot

rootfs_image=$output_dir/$artifact_prefix-rootfs.ext4
truncate -s "$rootfs_size" "$rootfs_image"
mke2fs -q -F -t ext4 -L HELM_ROOT -m 0 \
    -O 64bit,metadata_csum \
    -d "$rootfs_dir" \
    "$rootfs_image"
e2fsck -fn "$rootfs_image"
zstd -T0 -10 -f "$rootfs_image" -o "$rootfs_image.zst"

echo "==> Performing offline validation"
test -x "$rootfs_dir/sbin/init"
test -x "$rootfs_dir/usr/sbin/sshd" || test -x "$rootfs_dir/usr/sbin/dropbear"
test -e "$rootfs_dir/lib/modules/$kernel_release/modules.dep"
test -e "$rootfs_dir/lib/modules/$kernel_release/updates/drivers/net/ethernet/realtek/r8168/r8168.ko"
test -s "$rootfs_dir/boot/Image"
test -s "$rootfs_dir/boot/initramfs-helm"
test -s "$rootfs_dir/boot/dtb/helm-p3768.dtbo"
test -s "$rootfs_dir/lib/firmware/nvidia/ga10b/gpmu_ucode_next_prod_image.bin"
test "$(grep -c 'modules=phy-tegra194-p2u,pcie-tegra194,nvme' \
    "$rootfs_dir/boot/extlinux/extlinux.conf")" = 6
modinfo \
    "$rootfs_dir/lib/modules/$kernel_release/updates/drivers/net/ethernet/realtek/r8168/r8168.ko" \
    | grep -q "^vermagic: *$kernel_release "
chroot "$rootfs_dir" /bin/sh -ec '
    test "$(uname -m)" = aarch64
    apk --version
    /usr/sbin/helm-info --offline
'

(
    cd "$output_dir"
    sha256sum \
        "$artifact_prefix-rootfs.tar.zst" \
        "$artifact_prefix-boot.tar.zst" \
        "$artifact_prefix-rootfs.ext4.zst" \
        > SHA256SUMS
)

du -sh "$rootfs_dir" > "$output_dir/rootfs-size.txt"
tune2fs -l "$rootfs_image" > "$output_dir/ext4-info.txt"

echo "==> Build complete"
ls -lh "$output_dir"

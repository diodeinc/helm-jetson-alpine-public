#!/bin/sh
set -eu

script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
repo_dir=$(CDPATH='' cd -- "$script_dir/../.." && pwd)

l4t_archive=${HELM_L4T_ARCHIVE:-"$repo_dir/build/downloads/Jetson_Linux_R39.2.0_aarch64.tbz2"}
build_dir=${HELM_ALPINE_BUILD_DIR:-"$repo_dir/build/helm-alpine-native"}
work_dir=$build_dir/work
output_dir=$build_dir/out
rootfs_dir=$work_dir/rootfs
l4t_dir=$work_dir/l4t
oot_dir=$work_dir/oot-modules
deb_dir=$work_dir/firmware-deb
initramfs_dir=$work_dir/initramfs
recovery_dir=$work_dir/recovery-initramfs
kernel_release=6.8.12-1021-tegra

if [ "$(uname -s)" != Darwin ] || [ "$(uname -m)" != arm64 ]; then
	echo "The native builder requires Apple-silicon macOS." >&2
	exit 69
fi

for command in apko brew dtc fdtoverlay fdtget fdtput python3 shasum zstd; do
	if ! command -v "$command" >/dev/null 2>&1; then
		echo "missing native build dependency: $command" >&2
		echo "install with: brew install apko cpio dtc libarchive zstd" >&2
		exit 69
	fi
done

archive_tool=$(brew --prefix libarchive)/bin/bsdtar
cpio_tool=$(brew --prefix cpio)/bin/cpio
for tool in "$archive_tool" "$cpio_tool"; do
	if [ ! -x "$tool" ]; then
		echo "missing native build dependency: $tool" >&2
		exit 69
	fi
done

if [ ! -f "$l4t_archive" ]; then
	echo "Missing NVIDIA Jetson Linux R39.2 BSP archive:" >&2
	echo "  $l4t_archive" >&2
	exit 66
fi

export COPYFILE_DISABLE=1

clean_directory()
{
	directory=$1
	case "$directory" in
		"$rootfs_dir"|"$l4t_dir"|"$oot_dir"|"$deb_dir"|\
		"$initramfs_dir"|"$recovery_dir"|"$output_dir") ;;
		*) echo "refusing to clean unexpected directory: $directory" >&2; exit 70 ;;
	esac
	mkdir -p "$directory"
	find "$directory" -mindepth 1 -delete
}

clean_directory "$rootfs_dir"
clean_directory "$l4t_dir"
clean_directory "$output_dir"

echo "==> Resolving the arm64 Alpine filesystem natively with apko"
base_rootfs=$work_dir/apko-rootfs.tar.gz
apko build-minirootfs \
	--build-arch arm64 \
	"$script_dir/apko.yaml" \
	"$base_rootfs"

# Device nodes cannot be materialized by an unprivileged macOS process and
# are unnecessary in the archive because devtmpfs supplies them at boot.
"$archive_tool" -xpf "$base_rootfs" \
	-C "$rootfs_dir" \
	--no-same-owner \
	--exclude 'dev/*'
mkdir -p "$rootfs_dir/dev/pts" "$rootfs_dir/dev/shm"
# Unprivileged libarchive extraction deliberately clears setuid. Restore the
# one Alpine helper that is shipped setuid and make it readable to the builder.
chmod 4511 "$rootfs_dir/bin/bbsuid"

echo "==> Extracting R39.2 kernel inputs as data (no NVIDIA executable)"
"$archive_tool" -xjf "$l4t_archive" -C "$l4t_dir" \
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
"$archive_tool" -xjf "$l4t_root/kernel/kernel_supplements.tbz2" -C "$rootfs_dir"

clean_directory "$oot_dir"
"$archive_tool" -xjf "$l4t_root/kernel/kernel_oot_modules.tbz2" -C "$oot_dir"
mkdir -p "$rootfs_dir/lib/modules"
cp -a "$oot_dir/usr/lib/modules/." "$rootfs_dir/lib/modules/"

"$archive_tool" -xjf "$l4t_root/kernel/nvidia_l4t_kernel_nvgpu.tbz2" -C "$rootfs_dir"

firmware_deb=$l4t_root/nv_tegra/l4t_deb_packages/nvidia-l4t-firmware_39.2.0-20260601141651_arm64.deb
clean_directory "$deb_dir"
"$archive_tool" -xf "$firmware_deb" -C "$deb_dir"
firmware_data=$(find "$deb_dir" -maxdepth 1 -type f -name 'data.tar*' -print | head -n 1)
[ -n "$firmware_data" ] || { echo "firmware data archive is missing" >&2; exit 70; }
"$archive_tool" -xf "$firmware_data" -C "$rootfs_dir"

if [ -d "$rootfs_dir/lib/systemd" ]; then
	find "$rootfs_dir/lib/systemd" -mindepth 1 -delete
	rmdir "$rootfs_dir/lib/systemd"
fi

echo "==> Applying the Helm filesystem overlay"
cp -a "$script_dir/rootfs-overlay/." "$rootfs_dir/"
mkdir -p "$rootfs_dir/boot/extlinux" "$rootfs_dir/etc/mkinitfs"
install -m 0644 "$script_dir/mkinitfs.conf" "$rootfs_dir/etc/mkinitfs/mkinitfs.conf"
install -m 0644 "$script_dir/extlinux.conf" "$rootfs_dir/boot/extlinux/extlinux.conf"
install -m 0644 "$l4t_root/kernel/Image" "$rootfs_dir/boot/Image"

chmod 0755 \
	"$rootfs_dir/etc/init.d/helm-peripherals" \
	"$rootfs_dir/usr/sbin/helm-console-login" \
	"$rootfs_dir/usr/sbin/helm-info" \
	"$rootfs_dir/usr/sbin/helm-install" \
	"$rootfs_dir/usr/sbin/helm-led" \
	"$rootfs_dir/usr/sbin/helm-peripherals" \
	"$rootfs_dir/usr/sbin/helm-qspi-install"
chmod 0600 "$rootfs_dir/etc/dropbear/dropbear.conf"

mkdir -p "$rootfs_dir/root/.ssh"
chmod 0700 "$rootfs_dir/root/.ssh"
if [ -n "${HELM_SSH_AUTHORIZED_KEYS_FILE:-}" ]; then
	if [ ! -f "$HELM_SSH_AUTHORIZED_KEYS_FILE" ]; then
		echo "HELM_SSH_AUTHORIZED_KEYS_FILE is not a file." >&2
		exit 66
	fi
	install -m 0600 "$HELM_SSH_AUTHORIZED_KEYS_FILE" \
		"$rootfs_dir/root/.ssh/authorized_keys"
else
	: > "$rootfs_dir/root/.ssh/authorized_keys"
	chmod 0600 "$rootfs_dir/root/.ssh/authorized_keys"
fi

: > "$rootfs_dir/etc/machine-id"
: > "$rootfs_dir/etc/resolv.conf"

enable_service()
{
	service=$1
	runlevel=$2
	if [ ! -e "$rootfs_dir/etc/init.d/$service" ]; then
		echo "missing OpenRC service: $service" >&2
		exit 70
	fi
	mkdir -p "$rootfs_dir/etc/runlevels/$runlevel"
	ln -sf "/etc/init.d/$service" "$rootfs_dir/etc/runlevels/$runlevel/$service"
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

echo "==> Building and validating Helm device trees natively"
dtb_dir=$rootfs_dir/boot/dtb
mkdir -p "$dtb_dir"
dtc -@ -I dts -O dtb -o "$dtb_dir/helm-p3768.dtbo" \
	"$script_dir/device-tree/helm-p3768.dtso"

for sku in 0000 0001 0003 0004 0005; do
	base_dtb=$l4t_root/kernel/dtb/tegra234-p3768-0000+p3767-$sku-nv.dtb
	helm_dtb=$dtb_dir/helm-p3767-$sku.dtb
	fdtoverlay -i "$base_dtb" -o "$helm_dtb" "$dtb_dir/helm-p3768.dtbo"

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
	test "$(fdtget -t s "$helm_dtb" /bus@0/pcie@140a0000 status)" = okay
	test "$(fdtget -t s "$helm_dtb" /helm-leds/green-status label)" = \
		"helm:green:status"
done

echo "==> Creating the native Helm initramfs"
clean_directory "$initramfs_dir"
mkdir -p \
	"$initramfs_dir/bin" \
	"$initramfs_dir/dev/pts" \
	"$initramfs_dir/lib/modules/$kernel_release" \
	"$initramfs_dir/newroot" \
	"$initramfs_dir/proc" \
	"$initramfs_dir/run" \
	"$initramfs_dir/sys"
install -m 0755 "$script_dir/initramfs/init" "$initramfs_dir/init"
install -m 0755 "$rootfs_dir/bin/busybox" "$initramfs_dir/bin/busybox"
cp -a "$rootfs_dir/lib/ld-musl-aarch64.so.1" "$initramfs_dir/lib/"
cp -a "$rootfs_dir/lib/libc.musl-aarch64.so.1" "$initramfs_dir/lib/"

module_source=$rootfs_dir/lib/modules/$kernel_release
for module in spi-tegra210-quad phy-tegra194-p2u pcie-tegra194 nvme-core nvme; do
	module_path=$(find "$module_source" -type f -name "$module.ko" -print | head -n 1)
	if [ -n "$module_path" ]; then
		relative_path=${module_path#"$module_source"/}
		mkdir -p "$initramfs_dir/lib/modules/$kernel_release/$(dirname "$relative_path")"
		cp -a "$module_path" "$initramfs_dir/lib/modules/$kernel_release/$relative_path"
	fi
done

(
	cd "$initramfs_dir"
	find . -print | LC_ALL=C sort | \
		"$cpio_tool" --quiet --create --format=newc --owner=0:0 | \
		gzip -n -9 > "$rootfs_dir/boot/initramfs-helm"
)

cat > "$rootfs_dir/etc/helm-release" <<EOF
HELM_OS=Alpine
ALPINE_BRANCH=v3.24
L4T_RELEASE=39.2
KERNEL_RELEASE=$kernel_release
CARRIER=Helm
BUILD_BACKEND=macos-native
EOF

alpine_version=$(cat "$rootfs_dir/etc/alpine-release")
artifact_prefix=helm-alpine-$alpine_version-l4t-r39.2
rootfs_archive=$output_dir/$artifact_prefix-rootfs.tar.zst

echo "==> Packaging the installable rootfs on macOS"
(
	cd "$rootfs_dir"
	find . -print | LC_ALL=C sort | \
		"$archive_tool" \
			--format pax \
			--no-recursion \
			--uid 0 --gid 0 --uname root --gname root \
			--numeric-owner --no-xattrs --no-acls --no-fflags \
			-cf - -T - | \
		zstd -q -T0 -10 -o "$rootfs_archive"
)

echo "==> Creating a self-contained NVMe recovery initramfs"
clean_directory "$recovery_dir"
mkdir -p "$recovery_dir/lib" "$recovery_dir/opt/helm/payload"

# The recovery environment uses Alpine's complete small userspace, but carries
# only the kernel modules needed to discover NVMe. The install archive contains
# the full target module and firmware trees.
for directory in bin etc sbin usr var; do
	cp -a "$rootfs_dir/$directory" "$recovery_dir/"
done
find "$rootfs_dir/lib" -mindepth 1 -maxdepth 1 \
	! -name modules ! -name firmware \
	-exec cp -a {} "$recovery_dir/lib/" \;
mkdir -p \
	"$recovery_dir/dev/pts" \
	"$recovery_dir/lib/modules/$kernel_release" \
	"$recovery_dir/proc" \
	"$recovery_dir/run" \
	"$recovery_dir/sys" \
	"$recovery_dir/tmp"
install -m 0755 "$script_dir/initramfs/recovery-init" "$recovery_dir/init"
install -m 0644 "$rootfs_archive" \
	"$recovery_dir/opt/helm/payload/helm-rootfs.tar.zst"

for module in spi-tegra210-quad phy-tegra194-p2u pcie-tegra194 nvme-core nvme; do
	module_path=$(find "$module_source" -type f -name "$module.ko" -print | head -n 1)
	if [ -n "$module_path" ]; then
		relative_path=${module_path#"$module_source"/}
		mkdir -p "$recovery_dir/lib/modules/$kernel_release/$(dirname "$relative_path")"
		cp -a "$module_path" "$recovery_dir/lib/modules/$kernel_release/$relative_path"
	fi
done

recovery_initramfs=$output_dir/$artifact_prefix-recovery-initramfs.gz
(
	cd "$recovery_dir"
	find . -print | LC_ALL=C sort | \
		"$cpio_tool" --quiet --create --format=newc --owner=0:0 | \
		gzip -n -9 > "$recovery_initramfs"
)

recovery_boot=$output_dir/$artifact_prefix-recovery-boot.img
python3 "$repo_dir/tools/helm-macos/mkbootimg.py" \
	--kernel "$rootfs_dir/boot/Image" \
	--ramdisk "$recovery_initramfs" \
	--cmdline "rdinit=/init rw console=ttyTCU0,115200 console=tty0 firmware_class.path=/lib/firmware pci=pcie_bus_perf nvme.use_threaded_interrupts=1" \
	--output "$recovery_boot"

echo "==> Packaging the boot archive on macOS"
(
	cd "$rootfs_dir"
	find boot -print | LC_ALL=C sort | \
		"$archive_tool" \
			--format pax \
			--no-recursion \
			--uid 0 --gid 0 --uname root --gname root \
			--numeric-owner --no-xattrs --no-acls --no-fflags \
			-cf - -T - | \
		zstd -q -T0 -10 -o "$output_dir/$artifact_prefix-boot.tar.zst"
)

cp "$rootfs_dir/boot/initramfs-helm" "$output_dir/$artifact_prefix-initramfs.gz"
cp "$rootfs_dir/etc/helm-release" "$output_dir/helm-release"
awk -F: '/^P:/{package=$2} /^V:/{print package "=" $2}' \
	"$rootfs_dir/lib/apk/db/installed" | LC_ALL=C sort > "$output_dir/packages.txt"

(
	cd "$output_dir"
	shasum -a 256 \
		"$artifact_prefix-rootfs.tar.zst" \
		"$artifact_prefix-boot.tar.zst" \
		"$artifact_prefix-initramfs.gz" \
		"$artifact_prefix-recovery-initramfs.gz" \
		"$artifact_prefix-recovery-boot.img" \
		> SHA256SUMS
)

test -L "$rootfs_dir/sbin/init"
test -x "$rootfs_dir/bin/busybox"
test -x "$rootfs_dir/usr/sbin/dropbear"
test -x "$rootfs_dir/usr/sbin/helm-install"
test -x "$rootfs_dir/usr/sbin/helm-qspi-install"
test -x "$rootfs_dir/usr/bin/zstd"
test -x "$rootfs_dir/sbin/sfdisk"
test -s "$rootfs_dir/boot/Image"
test -s "$rootfs_dir/boot/initramfs-helm"
test -s "$rootfs_dir/boot/dtb/helm-p3768.dtbo"
test -e "$rootfs_dir/lib/modules/$kernel_release/kernel/drivers/pci/controller/dwc/pcie-tegra194.ko"
test -e "$rootfs_dir/lib/modules/$kernel_release/updates/drivers/net/ethernet/realtek/r8168/r8168.ko"
test -s "$recovery_initramfs"
test "$(dd if="$recovery_boot" bs=8 count=1 2>/dev/null)" = "ANDROID!"

echo "==> Native macOS build complete"
ls -lh "$output_dir"

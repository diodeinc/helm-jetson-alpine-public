#!/bin/sh
set -eu

script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
repo_dir=$(CDPATH='' cd -- "$script_dir/../.." && pwd)

l4t_archive=${HELM_L4T_ARCHIVE:-"$repo_dir/build/downloads/Jetson_Linux_R39.2.0_aarch64.tbz2"}
bootloader_deb=${HELM_R39_BOOTLOADER_DEB:-"$repo_dir/build/downloads/nvidia-l4t-bootloader_39.2.0-20260601141651_arm64.deb"}
build_dir=${HELM_ALPINE_BUILD_DIR:-"$repo_dir/build/helm-alpine-native"}
work_dir=$build_dir/work
output_dir=$build_dir/out
rootfs_dir=$work_dir/rootfs
l4t_dir=$work_dir/l4t
oot_dir=$work_dir/oot-modules
deb_dir=$work_dir/firmware-deb
launcher_dir=$work_dir/bootloader-deb
initramfs_dir=$work_dir/initramfs
recovery_dir=$work_dir/recovery-initramfs
kernel_release=6.8.12-1021-tegra
bootloader_deb_sha256=01ef88369674ab5b9dd0dca6378e19012b594b4b3b95f2378b2d191ee0c331be
launcher_sha256=c9b54649f7a05fc326d1bf82fc68fa234d6e5a915cb56f8030874fdb21514d32

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

if [ ! -f "$bootloader_deb" ]; then
	echo "Missing NVIDIA Jetson Linux R39.2 bootloader package:" >&2
	echo "  $bootloader_deb" >&2
	exit 66
fi
actual_bootloader_deb_sha256=$(shasum -a 256 "$bootloader_deb" | awk '{print $1}')
if [ "$actual_bootloader_deb_sha256" != "$bootloader_deb_sha256" ]; then
	echo "NVIDIA bootloader package SHA-256 mismatch:" >&2
	echo "  expected: $bootloader_deb_sha256" >&2
	echo "  actual:   $actual_bootloader_deb_sha256" >&2
	exit 65
fi

export COPYFILE_DISABLE=1

clean_directory()
{
	directory=$1
	case "$directory" in
		"$rootfs_dir"|"$l4t_dir"|"$oot_dir"|"$deb_dir"|"$launcher_dir"|\
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

# apko 1.2.x renders its InstallIf []string with Go's bracket notation in
# the legacy installed database (for example, "i:[openrc chrony=4.8-r7]").
# apk-tools 3 treats the brackets as unsupported dependency syntax and can
# reject the entire database. Normalize only that field back to apk's v2
# database syntax; leave all package and file records otherwise untouched.
apk_installed_db=$rootfs_dir/usr/lib/apk/db/installed
apk_installed_db_tmp=$apk_installed_db.helm-new
awk '
	$0 == "i:[]" { next }
	/^i:\[/ {
		if (substr($0, length($0), 1) != "]") exit 1
		print "i:" substr($0, 4, length($0) - 4)
		next
	}
	{ print }
' "$apk_installed_db" > "$apk_installed_db_tmp"
chmod 0644 "$apk_installed_db_tmp"
mv "$apk_installed_db_tmp" "$apk_installed_db"
if grep -q '^i:\[' "$apk_installed_db"; then
	echo "apko installed database still contains bracketed install_if data" >&2
	exit 70
fi

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

echo "==> Extracting NVIDIA's pinned T23x UEFI OS launcher as data"
clean_directory "$launcher_dir"
"$archive_tool" -xf "$bootloader_deb" -C "$launcher_dir" data.tar.zst
launcher_data=$launcher_dir/data.tar.zst
[ -s "$launcher_data" ] || { echo "bootloader package data archive is missing" >&2; exit 70; }
"$archive_tool" -xf "$launcher_data" -C "$launcher_dir" \
	./opt/ota_package/t23x/BOOTAA64.efi
launcher=$launcher_dir/opt/ota_package/t23x/BOOTAA64.efi
[ -s "$launcher" ] || { echo "T23x UEFI OS launcher is missing" >&2; exit 70; }
actual_launcher_sha256=$(shasum -a 256 "$launcher" | awk '{print $1}')
[ "$actual_launcher_sha256" = "$launcher_sha256" ] || {
	echo "T23x UEFI OS launcher SHA-256 mismatch: $actual_launcher_sha256" >&2
	exit 65
}

if [ -d "$rootfs_dir/lib/systemd" ]; then
	find "$rootfs_dir/lib/systemd" -mindepth 1 -delete
	rmdir "$rootfs_dir/lib/systemd"
fi

echo "==> Applying the Helm filesystem overlay"
cp -a "$script_dir/rootfs-overlay/." "$rootfs_dir/"
mkdir -p \
	"$rootfs_dir/boot/extlinux" \
	"$rootfs_dir/etc/mkinitfs" \
	"$rootfs_dir/usr/lib/helm"
install -m 0644 "$script_dir/mkinitfs.conf" "$rootfs_dir/etc/mkinitfs/mkinitfs.conf"
install -m 0644 "$script_dir/extlinux.conf" "$rootfs_dir/boot/extlinux/extlinux.conf"
install -m 0644 "$l4t_root/kernel/Image" "$rootfs_dir/boot/Image"
install -m 0644 "$launcher" "$rootfs_dir/usr/lib/helm/BOOTAA64.EFI"

chmod 0755 \
	"$rootfs_dir/etc/init.d/helm-boot-success" \
	"$rootfs_dir/etc/init.d/helm-peripherals" \
	"$rootfs_dir/usr/sbin/helm-boot-success" \
	"$rootfs_dir/usr/sbin/helm-console-login" \
	"$rootfs_dir/usr/sbin/helm-info" \
	"$rootfs_dir/usr/sbin/helm-install" \
	"$rootfs_dir/usr/sbin/helm-led" \
	"$rootfs_dir/usr/sbin/helm-peripherals" \
	"$rootfs_dir/usr/sbin/helm-provision" \
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
for service in udev-postmount syslog dhcpcd chronyd helm-peripherals helm-boot-success dropbear local; do
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
	fdtput -d "$helm_dtb" "/__symbols__" typec_p0 || :
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
for module in at24 spi-tegra210-quad phy-tegra194-p2u pcie-tegra194 nvme-core nvme; do
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
rootfs_tar=$output_dir/$artifact_prefix-rootfs.tar

chrony_uid=$(awk -F: '$1 == "chrony" { print $3 }' "$rootfs_dir/etc/passwd")
chrony_gid=$(awk -F: '$1 == "chrony" { print $3 }' "$rootfs_dir/etc/group")
if [ "$chrony_uid:$chrony_gid" != 100:101 ]; then
	echo "unexpected chrony account IDs: $chrony_uid:$chrony_gid" >&2
	exit 70
fi
dhcpcd_uid=$(awk -F: '$1 == "dhcpcd" { print $3 }' "$rootfs_dir/etc/passwd")
dhcpcd_gid=$(awk -F: '$1 == "dhcpcd" { print $3 }' "$rootfs_dir/etc/group")
if [ "$dhcpcd_uid:$dhcpcd_gid" != 101:102 ]; then
	echo "unexpected dhcpcd account IDs: $dhcpcd_uid:$dhcpcd_gid" >&2
	exit 70
fi

echo "==> Packaging the installable rootfs on macOS"
(
	cd "$rootfs_dir"
	# macOS cannot materialize numeric owners from apko while extracting, so
	# omit service-owned state directories from the all-root archive and append
	# their headers with the declarative UIDs/GIDs.
	find . -print | LC_ALL=C sort | \
		awk '$0 != "./var/lib/chrony" && $0 != "./var/lib/dhcpcd"' | \
		"$archive_tool" \
			--format pax \
			--no-recursion \
			--uid 0 --gid 0 --uname root --gname root \
			--numeric-owner --no-xattrs --no-acls --no-fflags \
			-cf "$rootfs_tar" -T -
	printf '%s\n' ./var/lib/chrony | \
		"$archive_tool" \
			--format pax \
			--no-recursion \
			--uid "$chrony_uid" --gid "$chrony_gid" \
			--numeric-owner --no-xattrs --no-acls --no-fflags \
			-rf "$rootfs_tar" -T -
	printf '%s\n' ./var/lib/dhcpcd | \
		"$archive_tool" \
			--format pax \
			--no-recursion \
			--uid "$dhcpcd_uid" --gid "$dhcpcd_gid" \
			--numeric-owner --no-xattrs --no-acls --no-fflags \
			-rf "$rootfs_tar" -T -
)
"$archive_tool" --numeric-owner -tvf "$rootfs_tar" | \
	awk '$NF == "./var/lib/chrony/" && $3 == 100 && $4 == 101 { found = 1 } END { exit !found }'
"$archive_tool" --numeric-owner -tvf "$rootfs_tar" | \
	awk '$NF == "./var/lib/dhcpcd/" && $3 == 101 && $4 == 102 { found = 1 } END { exit !found }'
zstd -q -T0 -10 "$rootfs_tar" -o "$rootfs_archive"
rm "$rootfs_tar"

echo "==> Creating a self-contained NVMe recovery initramfs"
clean_directory "$recovery_dir"
mkdir -p "$recovery_dir/lib" "$recovery_dir/opt/helm/payload"

# The recovery environment uses Alpine's complete small userspace, but carries
# only the kernel modules needed to identify the module and discover QSPI/NVMe.
# The install archive contains the full target module and firmware trees.
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

for module in \
	pwm-tegra pwm-fan \
	at24 spi-tegra210-quad phy-tegra194-p2u pcie-tegra194 nvme-core nvme \
	tegra-xudc libcomposite u_serial usb_f_acm u_ether usb_f_ncm
do
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
	--cmdline "rdinit=/init rw console=tty0 console=ttyTCU0,115200 firmware_class.path=/lib/firmware pci=pcie_bus_perf nvme.use_threaded_interrupts=1" \
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
test -x "$rootfs_dir/usr/sbin/helm-provision"
test -x "$rootfs_dir/usr/sbin/helm-qspi-install"
"$rootfs_dir/usr/sbin/helm-install" self-test
"$rootfs_dir/usr/sbin/helm-provision" self-test
grep -qx 'DEFAULT helm-profile-required' \
	"$rootfs_dir/boot/extlinux/extlinux.conf"
! grep -q '^LABEL helm-auto$' "$rootfs_dir/boot/extlinux/extlinux.conf"
test -x "$rootfs_dir/usr/bin/zstd"
test -x "$rootfs_dir/sbin/sfdisk"
test -s "$rootfs_dir/boot/Image"
test -s "$rootfs_dir/boot/initramfs-helm"
test -s "$rootfs_dir/boot/dtb/helm-p3768.dtbo"
test "$(shasum -a 256 "$rootfs_dir/usr/lib/helm/BOOTAA64.EFI" | awk '{print $1}')" = \
	"$launcher_sha256"
test -x "$rootfs_dir/sbin/mkfs.fat"
test -x "$rootfs_dir/usr/bin/iperf3"
awk -F: '$1 == "chrony" && $3 == 100 && $4 == 101 && $6 == "/dev/null" && $7 == "/sbin/nologin" { found = 1 } END { exit !found }' \
	"$rootfs_dir/etc/passwd"
awk -F: '$1 == "chrony" && $3 == 101 { found = 1 } END { exit !found }' \
	"$rootfs_dir/etc/group"
awk -F: '$1 == "dhcpcd" && $3 == 101 && $4 == 102 && $6 == "/var/lib/dhcpcd" && $7 == "/sbin/nologin" { found = 1 } END { exit !found }' \
	"$rootfs_dir/etc/passwd"
awk -F: '$1 == "dhcpcd" && $3 == 102 { found = 1 } END { exit !found }' \
	"$rootfs_dir/etc/group"
grep -Eq '^i:openrc chrony=' "$rootfs_dir/usr/lib/apk/db/installed"
! grep -q '^i:\[' "$rootfs_dir/usr/lib/apk/db/installed"
test -e "$recovery_dir/lib/modules/$kernel_release/kernel/drivers/misc/eeprom/at24.ko"
test -e "$recovery_dir/lib/modules/$kernel_release/kernel/drivers/usb/gadget/udc/tegra-xudc.ko"
test -e "$recovery_dir/lib/modules/$kernel_release/kernel/drivers/usb/gadget/function/usb_f_acm.ko"
test -e "$recovery_dir/lib/modules/$kernel_release/kernel/drivers/usb/gadget/function/usb_f_ncm.ko"
test -e "$recovery_dir/lib/modules/$kernel_release/kernel/drivers/pwm/pwm-tegra.ko"
test -e "$recovery_dir/lib/modules/$kernel_release/kernel/drivers/hwmon/pwm-fan.ko"
test -x "$recovery_dir/usr/sbin/helm-provision"
test -e "$rootfs_dir/lib/modules/$kernel_release/kernel/drivers/pci/controller/dwc/pcie-tegra194.ko"
test -e "$rootfs_dir/lib/modules/$kernel_release/updates/drivers/net/ethernet/realtek/r8168/r8168.ko"
test -s "$recovery_initramfs"
test "$(dd if="$recovery_boot" bs=8 count=1 2>/dev/null)" = "ANDROID!"

echo "==> Native macOS build complete"
ls -lh "$output_dir"

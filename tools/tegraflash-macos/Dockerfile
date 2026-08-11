FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    USER=root

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        abootimg \
        android-sdk-libsparse-utils \
        bash \
        bc \
        binutils \
        bsdextrautils \
        busybox \
        bzip2 \
        ca-certificates \
        coreutils \
        cpp \
        cpio \
        dbus \
        device-tree-compiler \
        diffutils \
        dmsetup \
        dosfstools \
        e2fsprogs \
        fdisk \
        file \
        findutils \
        gawk \
        gdisk \
        grep \
        gzip \
        iproute2 \
        iputils-ping \
        isc-dhcp-client \
        kmod \
        libaudit1 \
        libcap-ng0 \
        liblz4-tool \
        libnsl2 \
        libpam-modules \
        libpam-runtime \
        libpam0g \
        libpcre3 \
        libwrap0 \
        libxml2-utils \
        linux-tools-generic \
        login \
        lz4 \
        mtd-utils \
        net-tools \
        nfs-kernel-server \
        openssh-client \
        openssh-server \
        openssl \
        parted \
        passwd \
        procps \
        python-is-python3 \
        python3 \
        python3-yaml \
        rsync \
        sed \
        sshpass \
        sudo \
        tar \
        udev \
        usbutils \
        util-linux \
        uuid-runtime \
        xxd \
        xz-utils \
        xmlstarlet \
        zstd \
    && usbip_bin="$(find /usr/lib/linux-tools-* -maxdepth 1 -type f -name usbip | head -n 1)" \
    && test -x "$usbip_bin" \
    && ln -s "$usbip_bin" /usr/local/bin/usbip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /l4t

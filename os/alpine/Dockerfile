FROM alpine:3.24

RUN apk add --no-cache \
        binutils \
        bzip2 \
        coreutils \
        cpio \
        dtc \
        dpkg \
        e2fsprogs \
        e2fsprogs-extra \
        findutils \
        kmod \
        mkinitfs \
        tar \
        zstd

COPY build-rootfs.sh /usr/local/bin/build-helm-alpine
RUN chmod 0755 /usr/local/bin/build-helm-alpine

ENTRYPOINT ["/usr/local/bin/build-helm-alpine"]

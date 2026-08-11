#ifndef HELM_APX_TRANSPORT_H
#define HELM_APX_TRANSPORT_H

#include <libusb.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define APX_NVIDIA_VENDOR_ID UINT16_C(0x0955)
#define APX_T234_CHIP_ID UINT8_C(0x23)
#define APX_MAX_PORT_DEPTH 8
#define APX_MAX_ENDPOINTS 16

typedef struct apx_endpoint_info {
    uint8_t address;
    uint8_t attributes;
    uint16_t max_packet_size;
} apx_endpoint_info;

typedef struct apx_device_info {
    uint16_t vendor_id;
    uint16_t product_id;
    uint16_t usb_bcd;
    uint8_t device_class;
    uint8_t device_subclass;
    uint8_t device_protocol;
    uint8_t bus;
    uint8_t address;
    uint8_t ports[APX_MAX_PORT_DEPTH];
    int port_count;
    int speed;
    uint8_t configuration_value;
    uint8_t interface_number;
    uint8_t interface_class;
    uint8_t interface_subclass;
    uint8_t interface_protocol;
    apx_endpoint_info endpoints[APX_MAX_ENDPOINTS];
    size_t endpoint_count;
} apx_device_info;

typedef struct apx_filter {
    uint16_t vendor_id;
    uint16_t product_id;
    bool match_product;
} apx_filter;

typedef int (*apx_enumerate_callback)(const apx_device_info *info, void *opaque);

bool apx_is_t234_product(uint16_t product_id);
const char *apx_chip_name(uint16_t product_id);
const char *apx_speed_name(int speed);
const char *apx_transfer_type_name(uint8_t attributes);
void apx_format_path(const apx_device_info *info, char *buffer, size_t size);

int apx_enumerate(libusb_context *context,
                  const apx_filter *filter,
                  apx_enumerate_callback callback,
                  void *opaque,
                  size_t *match_count);

/*
 * Opens the first matching device and claims its bulk interface. The caller
 * owns the returned handle and must release the interface and close it.
 */
int apx_open_first(libusb_context *context,
                   const apx_filter *filter,
                   libusb_device_handle **handle,
                   apx_device_info *info);

/*
 * Tegra BootROM exposes a 16-byte chip UID as the first bulk-IN transfer.
 * Reading it consumes that greeting. A later flashing program may therefore
 * require the board to be put back into recovery mode.
 */
int apx_read_bootrom_uid(libusb_device_handle *handle,
                        const apx_device_info *info,
                        uint8_t uid[16],
                        unsigned int timeout_ms);

#ifdef __cplusplus
}
#endif

#endif

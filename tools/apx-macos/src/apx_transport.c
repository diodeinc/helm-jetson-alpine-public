#include "apx_transport.h"

#include <stdio.h>
#include <string.h>

static bool apx_matches(const struct libusb_device_descriptor *descriptor,
                        const apx_filter *filter)
{
    if (descriptor->idVendor != filter->vendor_id) {
        return false;
    }
    return !filter->match_product || descriptor->idProduct == filter->product_id;
}

static void apx_read_configuration(libusb_device *device, apx_device_info *info)
{
    struct libusb_config_descriptor *configuration = NULL;
    int status = libusb_get_active_config_descriptor(device, &configuration);

    if (status == LIBUSB_ERROR_NOT_FOUND) {
        status = libusb_get_config_descriptor(device, 0, &configuration);
    }
    if (status != LIBUSB_SUCCESS || configuration == NULL) {
        return;
    }

    info->configuration_value = configuration->bConfigurationValue;
    for (uint8_t interface_index = 0;
         interface_index < configuration->bNumInterfaces;
         ++interface_index) {
        const struct libusb_interface *interface =
            &configuration->interface[interface_index];

        for (int alternate_index = 0;
             alternate_index < interface->num_altsetting;
             ++alternate_index) {
            const struct libusb_interface_descriptor *descriptor =
                &interface->altsetting[alternate_index];
            bool has_bulk_in = false;
            bool has_bulk_out = false;

            for (uint8_t endpoint_index = 0;
                 endpoint_index < descriptor->bNumEndpoints;
                 ++endpoint_index) {
                const struct libusb_endpoint_descriptor *endpoint =
                    &descriptor->endpoint[endpoint_index];
                const uint8_t transfer_type =
                    endpoint->bmAttributes & LIBUSB_TRANSFER_TYPE_MASK;

                if (transfer_type != LIBUSB_TRANSFER_TYPE_BULK) {
                    continue;
                }
                if ((endpoint->bEndpointAddress & LIBUSB_ENDPOINT_DIR_MASK) ==
                    LIBUSB_ENDPOINT_IN) {
                    has_bulk_in = true;
                } else {
                    has_bulk_out = true;
                }
            }

            if (!has_bulk_in || !has_bulk_out) {
                continue;
            }

            info->interface_number = descriptor->bInterfaceNumber;
            info->interface_class = descriptor->bInterfaceClass;
            info->interface_subclass = descriptor->bInterfaceSubClass;
            info->interface_protocol = descriptor->bInterfaceProtocol;
            for (uint8_t endpoint_index = 0;
                 endpoint_index < descriptor->bNumEndpoints &&
                 info->endpoint_count < APX_MAX_ENDPOINTS;
                 ++endpoint_index) {
                const struct libusb_endpoint_descriptor *endpoint =
                    &descriptor->endpoint[endpoint_index];
                apx_endpoint_info *destination =
                    &info->endpoints[info->endpoint_count++];

                destination->address = endpoint->bEndpointAddress;
                destination->attributes = endpoint->bmAttributes;
                destination->max_packet_size = endpoint->wMaxPacketSize;
            }
            libusb_free_config_descriptor(configuration);
            return;
        }
    }

    libusb_free_config_descriptor(configuration);
}

static int apx_describe_device(libusb_device *device,
                               const struct libusb_device_descriptor *descriptor,
                               apx_device_info *info)
{
    memset(info, 0, sizeof(*info));
    info->vendor_id = descriptor->idVendor;
    info->product_id = descriptor->idProduct;
    info->usb_bcd = descriptor->bcdUSB;
    info->device_class = descriptor->bDeviceClass;
    info->device_subclass = descriptor->bDeviceSubClass;
    info->device_protocol = descriptor->bDeviceProtocol;
    info->bus = libusb_get_bus_number(device);
    info->address = libusb_get_device_address(device);
    info->speed = libusb_get_device_speed(device);
    info->port_count = libusb_get_port_numbers(
        device, info->ports, (int)sizeof(info->ports));
    if (info->port_count < 0) {
        info->port_count = 0;
    }
    apx_read_configuration(device, info);
    return LIBUSB_SUCCESS;
}

bool apx_is_t234_product(uint16_t product_id)
{
    return (uint8_t)(product_id & UINT16_C(0x00ff)) == APX_T234_CHIP_ID;
}

const char *apx_chip_name(uint16_t product_id)
{
    /* Product IDs documented by NVIDIA's current Jetson Linux recovery
     * table. Keep the full ID mappings ahead of the chip-family fallback.
     */
    switch (product_id) {
    case 0x7023:
        return "Tegra234 / Jetson AGX Orin (P3701-0000/0005/0008)";
    case 0x7223:
        return "Tegra234 / Jetson AGX Orin 32GB (P3701-0004)";
    case 0x7323:
        return "Tegra234 / Jetson Orin NX 16GB (P3767-0000)";
    case 0x7423:
        return "Tegra234 / Jetson Orin NX 8GB (P3767-0001)";
    case 0x7523:
        return "Tegra234 / Jetson Orin Nano 8GB (P3767-0003/0005)";
    case 0x7623:
        return "Tegra234 / Jetson Orin Nano 4GB (P3767-0004)";
    case 0x7026:
        return "Jetson T5000 (P3834-0008)";
    case 0x7226:
        return "Jetson T4000 (P3834-0000)";
    default:
        break;
    }

    switch ((uint8_t)(product_id & UINT16_C(0x00ff))) {
    case 0x20:
        return "Tegra20";
    case 0x30:
        return "Tegra30";
    case 0x35:
        return "Tegra114";
    case 0x40:
        return "Tegra124";
    case 0x21:
        return "Tegra210";
    case 0x18:
        return "Tegra186";
    case 0x19:
        return "Tegra194";
    case APX_T234_CHIP_ID:
        return "Tegra234 / Jetson Orin";
    default:
        return "unknown NVIDIA USB device";
    }
}

const char *apx_speed_name(int speed)
{
    switch (speed) {
    case LIBUSB_SPEED_LOW:
        return "1.5 Mbit/s (low-speed)";
    case LIBUSB_SPEED_FULL:
        return "12 Mbit/s (full-speed)";
    case LIBUSB_SPEED_HIGH:
        return "480 Mbit/s (high-speed)";
    case LIBUSB_SPEED_SUPER:
        return "5 Gbit/s (SuperSpeed)";
#ifdef LIBUSB_SPEED_SUPER_PLUS
    case LIBUSB_SPEED_SUPER_PLUS:
        return "10+ Gbit/s (SuperSpeedPlus)";
#endif
    default:
        return "unknown";
    }
}

const char *apx_transfer_type_name(uint8_t attributes)
{
    switch (attributes & LIBUSB_TRANSFER_TYPE_MASK) {
    case LIBUSB_TRANSFER_TYPE_CONTROL:
        return "control";
    case LIBUSB_TRANSFER_TYPE_ISOCHRONOUS:
        return "isochronous";
    case LIBUSB_TRANSFER_TYPE_BULK:
        return "bulk";
    case LIBUSB_TRANSFER_TYPE_INTERRUPT:
        return "interrupt";
    default:
        return "unknown";
    }
}

void apx_format_path(const apx_device_info *info, char *buffer, size_t size)
{
    int written = snprintf(buffer, size, "%u", info->bus);

    if (written < 0 || (size_t)written >= size) {
        return;
    }
    size_t offset = (size_t)written;
    for (int index = 0; index < info->port_count; ++index) {
        written = snprintf(buffer + offset, size - offset, "%s%u",
                           index == 0 ? "-" : ".", info->ports[index]);
        if (written < 0 || (size_t)written >= size - offset) {
            return;
        }
        offset += (size_t)written;
    }
}

int apx_enumerate(libusb_context *context,
                  const apx_filter *filter,
                  apx_enumerate_callback callback,
                  void *opaque,
                  size_t *match_count)
{
    libusb_device **devices = NULL;
    const ssize_t device_count = libusb_get_device_list(context, &devices);
    size_t matches = 0;

    if (device_count < 0) {
        return (int)device_count;
    }

    int status = LIBUSB_SUCCESS;
    for (ssize_t index = 0; index < device_count; ++index) {
        struct libusb_device_descriptor descriptor;
        status = libusb_get_device_descriptor(devices[index], &descriptor);
        if (status != LIBUSB_SUCCESS) {
            continue;
        }
        if (!apx_matches(&descriptor, filter)) {
            continue;
        }

        apx_device_info info;
        apx_describe_device(devices[index], &descriptor, &info);
        ++matches;
        if (callback != NULL && callback(&info, opaque) != 0) {
            break;
        }
    }

    libusb_free_device_list(devices, 1);
    if (match_count != NULL) {
        *match_count = matches;
    }
    return LIBUSB_SUCCESS;
}

int apx_open_first(libusb_context *context,
                   const apx_filter *filter,
                   libusb_device_handle **handle,
                   apx_device_info *info)
{
    libusb_device **devices = NULL;
    const ssize_t device_count = libusb_get_device_list(context, &devices);

    if (device_count < 0) {
        return (int)device_count;
    }

    int status = LIBUSB_ERROR_NO_DEVICE;
    *handle = NULL;
    for (ssize_t index = 0; index < device_count; ++index) {
        struct libusb_device_descriptor descriptor;
        const int descriptor_status =
            libusb_get_device_descriptor(devices[index], &descriptor);
        if (descriptor_status != LIBUSB_SUCCESS ||
            !apx_matches(&descriptor, filter)) {
            continue;
        }

        apx_describe_device(devices[index], &descriptor, info);
        if (info->endpoint_count == 0) {
            status = LIBUSB_ERROR_NOT_FOUND;
            continue;
        }
        status = libusb_open(devices[index], handle);
        if (status != LIBUSB_SUCCESS) {
            continue;
        }
        status = libusb_claim_interface(*handle, info->interface_number);
        if (status == LIBUSB_SUCCESS) {
            break;
        }
        libusb_close(*handle);
        *handle = NULL;
    }

    libusb_free_device_list(devices, 1);
    return status;
}

int apx_read_bootrom_uid(libusb_device_handle *handle,
                        const apx_device_info *info,
                        uint8_t uid[16],
                        unsigned int timeout_ms)
{
    uint8_t input_endpoint = 0;
    for (size_t index = 0; index < info->endpoint_count; ++index) {
        const apx_endpoint_info *endpoint = &info->endpoints[index];
        if ((endpoint->attributes & LIBUSB_TRANSFER_TYPE_MASK) ==
                LIBUSB_TRANSFER_TYPE_BULK &&
            (endpoint->address & LIBUSB_ENDPOINT_DIR_MASK) ==
                LIBUSB_ENDPOINT_IN) {
            input_endpoint = endpoint->address;
            break;
        }
    }
    if (input_endpoint == 0) {
        return LIBUSB_ERROR_NOT_FOUND;
    }

    int transferred = 0;
    const int status = libusb_bulk_transfer(handle, input_endpoint, uid, 16,
                                            &transferred, timeout_ms);
    if (status != LIBUSB_SUCCESS) {
        return status;
    }
    return transferred == 16 ? LIBUSB_SUCCESS : LIBUSB_ERROR_IO;
}

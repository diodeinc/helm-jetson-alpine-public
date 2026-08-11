#include "apx_transport.h"

#include <errno.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

enum {
    APX_EXIT_OK = 0,
    APX_EXIT_NOT_FOUND = 2,
    APX_EXIT_USAGE = 64,
    APX_EXIT_USB = 70
};

typedef struct output_options {
    bool json;
    bool inspect;
    size_t index;
} output_options;

static void print_usage(FILE *stream)
{
    fprintf(stream,
            "Usage:\n"
            "  apxctl list [--json] [--vid ID] [--pid ID]\n"
            "  apxctl inspect [--json] [--vid ID] [--pid ID]\n"
            "  apxctl wait [--timeout SECONDS] [--json] [--vid ID] [--pid ID]\n"
            "  apxctl cid --consume-greeting [--vid ID] [--pid ID]\n"
            "  apxctl help\n"
            "\n"
            "Defaults: VID 0x0955 (NVIDIA), any product ID. Product IDs whose\n"
            "low byte is 0x23 are identified as T234 / Jetson Orin.\n"
            "\n"
            "The list, inspect, and wait commands are read-only. The cid command\n"
            "consumes BootROM's one-time 16-byte greeting; recovery mode may need\n"
            "to be re-entered before another RCM program can connect.\n");
}

static int parse_u16(const char *text, uint16_t *value)
{
    char *end = NULL;
    errno = 0;
    const unsigned long parsed = strtoul(text, &end, 0);
    if (errno != 0 || end == text || *end != '\0' || parsed > UINT16_MAX) {
        return -1;
    }
    *value = (uint16_t)parsed;
    return 0;
}

static int parse_double(const char *text, double *value)
{
    char *end = NULL;
    errno = 0;
    const double parsed = strtod(text, &end);
    if (errno != 0 || end == text || *end != '\0' || parsed < 0.0) {
        return -1;
    }
    *value = parsed;
    return 0;
}

static int parse_common_option(int argc,
                               char **argv,
                               int *index,
                               apx_filter *filter,
                               output_options *output)
{
    const char *argument = argv[*index];
    if (strcmp(argument, "--json") == 0) {
        output->json = true;
        return 1;
    }
    if (strcmp(argument, "--vid") == 0 || strcmp(argument, "--pid") == 0) {
        if (*index + 1 >= argc) {
            fprintf(stderr, "apxctl: %s requires a value\n", argument);
            return -1;
        }
        uint16_t value = 0;
        ++*index;
        if (parse_u16(argv[*index], &value) != 0) {
            fprintf(stderr, "apxctl: invalid USB ID: %s\n", argv[*index]);
            return -1;
        }
        if (strcmp(argument, "--vid") == 0) {
            filter->vendor_id = value;
        } else {
            filter->product_id = value;
            filter->match_product = true;
        }
        return 1;
    }
    return 0;
}

static int print_device(const apx_device_info *info, void *opaque)
{
    output_options *options = opaque;
    char path[64] = {0};
    apx_format_path(info, path, sizeof(path));

    if (options->json) {
        if (options->index > 0) {
            printf(",\n");
        }
        printf("  {\"vendor_id\":\"0x%04x\",\"product_id\":\"0x%04x\","
               "\"chip\":\"%s\",\"t234\":%s,\"path\":\"%s\","
               "\"bus\":%u,\"address\":%u,\"speed\":\"%s\"",
               info->vendor_id, info->product_id, apx_chip_name(info->product_id),
               apx_is_t234_product(info->product_id) ? "true" : "false", path,
               info->bus, info->address, apx_speed_name(info->speed));
        if (options->inspect) {
            printf(",\"usb_bcd\":\"%x.%02x\",\"configuration\":%u,"
                   "\"interface\":%u,\"interface_class\":%u,"
                   "\"interface_subclass\":%u,\"interface_protocol\":%u,"
                   "\"endpoints\":[",
                   (info->usb_bcd >> 8) & 0xff, info->usb_bcd & 0xff,
                   info->configuration_value, info->interface_number,
                   info->interface_class, info->interface_subclass,
                   info->interface_protocol);
            for (size_t index = 0; index < info->endpoint_count; ++index) {
                const apx_endpoint_info *endpoint = &info->endpoints[index];
                printf("%s{\"address\":\"0x%02x\",\"direction\":\"%s\","
                       "\"type\":\"%s\",\"max_packet_size\":%u}",
                       index == 0 ? "" : ",", endpoint->address,
                       (endpoint->address & LIBUSB_ENDPOINT_DIR_MASK) ==
                               LIBUSB_ENDPOINT_IN
                           ? "in"
                           : "out",
                       apx_transfer_type_name(endpoint->attributes),
                       endpoint->max_packet_size);
            }
            printf("]");
        }
        printf("}");
    } else if (!options->inspect) {
        printf("%04x:%04x  %-24s path=%s address=%u speed=%s\n",
               info->vendor_id, info->product_id,
               apx_chip_name(info->product_id), path, info->address,
               apx_speed_name(info->speed));
    } else {
        printf("Device %zu\n", options->index + 1);
        printf("  USB ID:      %04x:%04x\n", info->vendor_id, info->product_id);
        printf("  Chip family: %s\n", apx_chip_name(info->product_id));
        printf("  T234 APX:    %s\n",
               apx_is_t234_product(info->product_id) ? "yes" : "no");
        printf("  USB path:    %s (address %u)\n", path, info->address);
        printf("  USB version: %x.%02x\n", (info->usb_bcd >> 8) & 0xff,
               info->usb_bcd & 0xff);
        printf("  Link speed:  %s\n", apx_speed_name(info->speed));
        printf("  Config/if:   %u/%u class=%u subclass=%u protocol=%u\n",
               info->configuration_value, info->interface_number,
               info->interface_class, info->interface_subclass,
               info->interface_protocol);
        printf("  Endpoints:\n");
        for (size_t index = 0; index < info->endpoint_count; ++index) {
            const apx_endpoint_info *endpoint = &info->endpoints[index];
            printf("    0x%02x  %-3s %-12s max-packet=%u\n", endpoint->address,
                   (endpoint->address & LIBUSB_ENDPOINT_DIR_MASK) ==
                           LIBUSB_ENDPOINT_IN
                       ? "in"
                       : "out",
                   apx_transfer_type_name(endpoint->attributes),
                   endpoint->max_packet_size);
        }
    }
    ++options->index;
    return 0;
}

static int enumerate_and_print(libusb_context *context,
                               const apx_filter *filter,
                               output_options *output,
                               size_t *matches)
{
    if (output->json) {
        printf("[\n");
    }
    output->index = 0;
    const int status =
        apx_enumerate(context, filter, print_device, output, matches);
    if (output->json) {
        printf("\n]\n");
    }
    return status;
}

static double monotonic_seconds(void)
{
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) {
        return 0.0;
    }
    return (double)now.tv_sec + (double)now.tv_nsec / 1000000000.0;
}

static int command_list_or_inspect(int argc,
                                   char **argv,
                                   libusb_context *context,
                                   bool inspect)
{
    apx_filter filter = {.vendor_id = APX_NVIDIA_VENDOR_ID};
    output_options output = {.inspect = inspect};
    for (int index = 2; index < argc; ++index) {
        const int parsed =
            parse_common_option(argc, argv, &index, &filter, &output);
        if (parsed <= 0) {
            fprintf(stderr, "apxctl: unknown option: %s\n", argv[index]);
            return APX_EXIT_USAGE;
        }
    }

    size_t matches = 0;
    const int status =
        enumerate_and_print(context, &filter, &output, &matches);
    if (status != LIBUSB_SUCCESS) {
        fprintf(stderr, "apxctl: USB enumeration failed: %s\n",
                libusb_error_name(status));
        return APX_EXIT_USB;
    }
    return matches > 0 ? APX_EXIT_OK : APX_EXIT_NOT_FOUND;
}

static int command_wait(int argc, char **argv, libusb_context *context)
{
    apx_filter filter = {.vendor_id = APX_NVIDIA_VENDOR_ID};
    output_options output = {0};
    double timeout = 0.0;

    for (int index = 2; index < argc; ++index) {
        const int parsed =
            parse_common_option(argc, argv, &index, &filter, &output);
        if (parsed > 0) {
            continue;
        }
        if (strcmp(argv[index], "--timeout") == 0) {
            if (++index >= argc || parse_double(argv[index], &timeout) != 0) {
                fprintf(stderr, "apxctl: --timeout requires non-negative seconds\n");
                return APX_EXIT_USAGE;
            }
            continue;
        }
        fprintf(stderr, "apxctl: unknown option: %s\n", argv[index]);
        return APX_EXIT_USAGE;
    }

    const double started = monotonic_seconds();
    for (;;) {
        size_t matches = 0;
        int status = apx_enumerate(context, &filter, NULL, NULL, &matches);
        if (status != LIBUSB_SUCCESS) {
            fprintf(stderr, "apxctl: USB enumeration failed: %s\n",
                    libusb_error_name(status));
            return APX_EXIT_USB;
        }
        if (matches > 0) {
            output.inspect = true;
            status = enumerate_and_print(context, &filter, &output, &matches);
            return status == LIBUSB_SUCCESS ? APX_EXIT_OK : APX_EXIT_USB;
        }
        if (timeout > 0.0 && monotonic_seconds() - started >= timeout) {
            return APX_EXIT_NOT_FOUND;
        }
        usleep(250000);
    }
}

static int command_cid(int argc, char **argv, libusb_context *context)
{
    apx_filter filter = {.vendor_id = APX_NVIDIA_VENDOR_ID};
    output_options output = {0};
    bool consent = false;

    for (int index = 2; index < argc; ++index) {
        const int parsed =
            parse_common_option(argc, argv, &index, &filter, &output);
        if (parsed > 0) {
            if (output.json) {
                fprintf(stderr, "apxctl: --json is not supported by cid\n");
                return APX_EXIT_USAGE;
            }
            continue;
        }
        if (strcmp(argv[index], "--consume-greeting") == 0) {
            consent = true;
            continue;
        }
        fprintf(stderr, "apxctl: unknown option: %s\n", argv[index]);
        return APX_EXIT_USAGE;
    }
    if (!consent) {
        fprintf(stderr,
                "apxctl: refusing to consume the BootROM greeting without "
                "--consume-greeting\n");
        return APX_EXIT_USAGE;
    }

    libusb_device_handle *handle = NULL;
    apx_device_info info;
    int status = apx_open_first(context, &filter, &handle, &info);
    if (status != LIBUSB_SUCCESS) {
        fprintf(stderr, "apxctl: cannot open APX device: %s\n",
                libusb_error_name(status));
        return status == LIBUSB_ERROR_NO_DEVICE ? APX_EXIT_NOT_FOUND
                                                : APX_EXIT_USB;
    }

    uint8_t uid[16];
    status = apx_read_bootrom_uid(handle, &info, uid, 5000);
    libusb_release_interface(handle, info.interface_number);
    libusb_close(handle);
    if (status != LIBUSB_SUCCESS) {
        fprintf(stderr, "apxctl: UID read failed: %s\n", libusb_error_name(status));
        return APX_EXIT_USB;
    }

    printf("BR_CID: 0x");
    for (size_t index = 0; index < sizeof(uid); ++index) {
        printf("%02" PRIx8, uid[index]);
    }
    printf("\n");
    return APX_EXIT_OK;
}

int main(int argc, char **argv)
{
    if (argc < 2 || strcmp(argv[1], "help") == 0 ||
        strcmp(argv[1], "--help") == 0 || strcmp(argv[1], "-h") == 0) {
        print_usage(stdout);
        return APX_EXIT_OK;
    }

    libusb_context *context = NULL;
    const int status = libusb_init(&context);
    if (status != LIBUSB_SUCCESS) {
        fprintf(stderr, "apxctl: libusb initialization failed: %s\n",
                libusb_error_name(status));
        return APX_EXIT_USB;
    }

    int result = APX_EXIT_USAGE;
    if (strcmp(argv[1], "list") == 0) {
        result = command_list_or_inspect(argc, argv, context, false);
    } else if (strcmp(argv[1], "inspect") == 0) {
        result = command_list_or_inspect(argc, argv, context, true);
    } else if (strcmp(argv[1], "wait") == 0) {
        result = command_wait(argc, argv, context);
    } else if (strcmp(argv[1], "cid") == 0) {
        result = command_cid(argc, argv, context);
    } else {
        fprintf(stderr, "apxctl: unknown command: %s\n", argv[1]);
        print_usage(stderr);
    }

    libusb_exit(context);
    return result;
}

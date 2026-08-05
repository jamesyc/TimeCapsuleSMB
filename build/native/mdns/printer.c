#include "mdns.h"
TC_LOCAL int ieee1284_lookup_field(char *out,
                                 size_t out_len,
                                 const unsigned char *id,
                                 size_t id_len,
                                 const char *key);
TC_LOCAL int TC_UNUSED extract_cmd_from_ieee1284_device_id(char *out,
                                                         size_t out_len,
                                                         const unsigned char *buf,
                                                         size_t actual_len);
TC_LOCAL int TC_UNUSED sanitize_usb_printer_device_id_transfer(unsigned char *buf,
                                                             size_t buf_len,
                                                             int transferred_len,
                                                             int *actual_len);
TC_LOCAL int ieee1284_lookup_field(char *out,
                                 size_t out_len,
                                 const unsigned char *id,
                                 size_t id_len,
                                 const char *key) {
    size_t key_len;
    size_t pos = 0;

    if (out == NULL || out_len == 0 || id == NULL || key == NULL) {
        return -1;
    }
    out[0] = '\0';
    key_len = strlen(key);
    while (pos < id_len) {
        size_t start = pos;
        size_t end;
        size_t colon;
        size_t value_start;
        size_t value_end;
        size_t value_len;

        while (pos < id_len && id[pos] != ';') {
            pos++;
        }
        end = pos;
        if (pos < id_len && id[pos] == ';') {
            pos++;
        }
        colon = start;
        while (colon < end && id[colon] != ':') {
            colon++;
        }
        if (colon == end || colon - start != key_len ||
            strncasecmp((const char *)id + start, key, key_len) != 0) {
            continue;
        }
        value_start = colon + 1;
        value_end = end;
        while (value_start < value_end && isspace((unsigned char)id[value_start])) {
            value_start++;
        }
        while (value_end > value_start && isspace((unsigned char)id[value_end - 1])) {
            value_end--;
        }
        value_len = value_end - value_start;
        if (value_len == 0 || value_len >= out_len) {
            return -1;
        }
        memcpy(out, id + value_start, value_len);
        out[value_len] = '\0';
        return 0;
    }
    return -1;
}

TC_LOCAL int TC_UNUSED extract_cmd_from_ieee1284_device_id(char *out,
                                                         size_t out_len,
                                                         const unsigned char *buf,
                                                         size_t actual_len) {
    size_t reported_len;
    size_t id_len;

    if (out == NULL || out_len == 0 || buf == NULL || actual_len <= 2) {
        return -1;
    }
    reported_len = ((size_t)buf[0] << 8) | (size_t)buf[1];
    if (reported_len > 2 && reported_len <= actual_len) {
        id_len = reported_len - 2;
    } else {
        id_len = actual_len - 2;
    }

    if (ieee1284_lookup_field(out, out_len, buf + 2, id_len, "CMD") == 0 ||
        ieee1284_lookup_field(out, out_len, buf + 2, id_len, "COMMAND SET") == 0) {
        return 0;
    }
    return -1;
}

TC_LOCAL int TC_UNUSED sanitize_usb_printer_device_id_transfer(unsigned char *buf,
                                                             size_t buf_len,
                                                             int transferred_len,
                                                             int *actual_len) {
    if (buf == NULL || actual_len == NULL) {
        return -1;
    }
    *actual_len = 0;
    if (transferred_len < 0 || (size_t)transferred_len > buf_len) {
        memset(buf, 0, buf_len);
        return -1;
    }
    if ((size_t)transferred_len < buf_len) {
        memset(buf + transferred_len, 0, buf_len - (size_t)transferred_len);
    }
    if (transferred_len <= 2) {
        return -1;
    }
    *actual_len = transferred_len;
    return 0;
}

#if defined(__NetBSD__)
TC_LOCAL int usb_device_info_has_ulpt(const struct usb_device_info *info) {
    size_t i;

    for (i = 0; i < USB_MAX_DEVNAMES; i++) {
        if (strncmp(info->udi_devnames[i], "ulpt", 4) == 0) {
            return 1;
        }
    }
    return 0;
}

TC_LOCAL int usb_device_info_matches_riousbprint(const struct config *cfg,
                                               const struct usb_device_info *info) {
    if (cfg->riousbprint_vendor_id != 0 &&
        cfg->riousbprint_product_id != 0 &&
        info->udi_vendorNo == cfg->riousbprint_vendor_id &&
        info->udi_productNo == cfg->riousbprint_product_id) {
        return 1;
    }
    return usb_device_info_has_ulpt(info);
}

TC_LOCAL int add_unique_usb_candidate(unsigned int candidates[], size_t *count, unsigned int value) {
    size_t i;

    for (i = 0; i < *count; i++) {
        if (candidates[i] == value) {
            return 0;
        }
    }
    if (*count >= 4) {
        return -1;
    }
    candidates[*count] = value;
    *count += 1;
    return 0;
}

TC_LOCAL int query_usb_printer_device_id(int fd,
                                       int addr,
                                       const struct usb_device_info *info,
                                       unsigned char *buf,
                                       size_t buf_len,
                                       int *actual_len) {
    unsigned int configs[4];
    unsigned int indexes[4];
    size_t config_count = 0;
    size_t index_count = 0;
    size_t i;
    size_t j;

    if (info->udi_config != 0) {
        add_unique_usb_candidate(configs, &config_count, info->udi_config);
    }
    add_unique_usb_candidate(configs, &config_count, 1);
    add_unique_usb_candidate(configs, &config_count, 0);
    add_unique_usb_candidate(indexes, &index_count, 0);
    add_unique_usb_candidate(indexes, &index_count, 1);
    add_unique_usb_candidate(indexes, &index_count, 0x0100);
    add_unique_usb_candidate(indexes, &index_count, 0x0101);

    for (i = 0; i < config_count; i++) {
        for (j = 0; j < index_count; j++) {
            struct usb_ctl_request request;

            memset(buf, 0, buf_len);
            memset(&request, 0, sizeof(request));
            request.ucr_addr = addr;
            request.ucr_request.bmRequestType = UT_READ_CLASS_INTERFACE;
            request.ucr_request.bRequest = 0;
            USETW(request.ucr_request.wValue, configs[i]);
            USETW(request.ucr_request.wIndex, indexes[j]);
            USETW(request.ucr_request.wLength, buf_len);
            request.ucr_data = buf;
            request.ucr_flags = USBD_SHORT_XFER_OK;
            if (ioctl(fd, USB_REQUEST, &request) == 0) {
                if (sanitize_usb_printer_device_id_transfer(buf, buf_len, request.ucr_actlen, actual_len) == 0) {
                    return 0;
                }
            }
        }
    }
    return -1;
}

int discover_riousbprint_usb_cmd(struct config *cfg) {
    int bus;

    if (!is_riousbprint_enabled(cfg) || cfg->riousbprint_cmd[0] != '\0') {
        return 0;
    }

    for (bus = 0; bus < 4; bus++) {
        char path[32];
        int fd;
        int addr;

        snprintf(path, sizeof(path), "/dev/usb%d", bus);
        fd = open(path, O_RDWR);
        if (fd < 0) {
            continue;
        }
        for (addr = 1; addr < USB_MAX_DEVICES; addr++) {
            struct usb_device_info info;
            unsigned char device_id[IEEE1284_DEVICE_ID_MAX];
            int actual_len = 0;
            char cmd[MAX_TXT_STRING + 1];

            memset(&info, 0, sizeof(info));
            info.udi_addr = (uint8_t)addr;
            if (ioctl(fd, USB_DEVICEINFO, &info) != 0) {
                continue;
            }
            if (!usb_device_info_matches_riousbprint(cfg, &info)) {
                continue;
            }
            if (query_usb_printer_device_id(fd,
                                            addr,
                                            &info,
                                            device_id,
                                            sizeof(device_id),
                                            &actual_len) != 0) {
                continue;
            }
            if (extract_cmd_from_ieee1284_device_id(cmd, sizeof(cmd), device_id, (size_t)actual_len) == 0) {
                strncpy(cfg->riousbprint_cmd, cmd, sizeof(cfg->riousbprint_cmd) - 1);
                cfg->riousbprint_cmd[sizeof(cfg->riousbprint_cmd) - 1] = '\0';
                close(fd);
                fprintf(stderr,
                        "riousbprint usb: found IEEE-1284 CMD via %s addr=%d vendor=0x%04x product=0x%04x\n",
                        path,
                        addr,
                        info.udi_vendorNo,
                        info.udi_productNo);
                return 0;
            }
        }
        close(fd);
    }

    fprintf(stderr, "riousbprint usb: IEEE-1284 CMD not available from NetBSD USB controller\n");
    return -1;
}
#else
int discover_riousbprint_usb_cmd(struct config *cfg) {
    if (is_riousbprint_enabled(cfg) && cfg->riousbprint_cmd[0] == '\0') {
        fprintf(stderr, "riousbprint usb: IEEE-1284 CMD probing is unavailable on this platform\n");
    }
    return 0;
}
#endif

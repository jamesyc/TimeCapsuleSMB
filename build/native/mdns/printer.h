#ifndef TC_MDNS_PRINTER_H
#define TC_MDNS_PRINTER_H
#include "types.h"
struct usb_device_info;
#ifdef TC_NATIVE_TEST
int ieee1284_lookup_field(char *out,
                                 size_t out_len,
                                 const unsigned char *id,
                                 size_t id_len,
                                 const char *key);
#endif
#ifdef TC_NATIVE_TEST
int extract_cmd_from_ieee1284_device_id(char *out,
                                                         size_t out_len,
                                                         const unsigned char *buf,
                                                         size_t actual_len);
#endif
#ifdef TC_NATIVE_TEST
int sanitize_usb_printer_device_id_transfer(unsigned char *buf,
                                                             size_t buf_len,
                                                             int transferred_len,
                                                             int *actual_len);
#endif
#ifdef TC_NATIVE_TEST
int usb_device_info_has_ulpt(const struct usb_device_info *info);
#endif
#ifdef TC_NATIVE_TEST
int usb_device_info_matches_riousbprint(const struct config *cfg,
                                               const struct usb_device_info *info);
#endif
#ifdef TC_NATIVE_TEST
int add_unique_usb_candidate(unsigned int candidates[], size_t *count, unsigned int value);
#endif
#ifdef TC_NATIVE_TEST
int query_usb_printer_device_id(int fd,
                                       int addr,
                                       const struct usb_device_info *info,
                                       unsigned char *buf,
                                       size_t buf_len,
                                       int *actual_len);
#endif
int discover_riousbprint_usb_cmd(struct config *cfg);
#endif

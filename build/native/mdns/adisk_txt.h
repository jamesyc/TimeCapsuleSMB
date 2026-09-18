#ifndef TC_MDNS_ADISK_TXT_H
#define TC_MDNS_ADISK_TXT_H
#include "types.h"
int validate_single_dns_label(const char *value, const char *field_name);
int build_adisk_system_txt(char *out, size_t out_len, const char *wama);
int build_adisk_disk_txt(char *out, size_t out_len, const char *disk_key, const char *share_name,
                         const char *adisk_uuid, const char *adisk_disk_advf);
int add_adisk_disk_config(struct config *cfg, const char *share_name, const char *disk_key,
                         const char *adisk_uuid, const char *adisk_disk_advf);
int adisk_enabled(const struct config *cfg);
/* Assembles the _adisk TXT record bytes (length-prefixed items): sys item
 * first, then one per disk. Returns the byte count or -1. */
int build_adisk_txt_record(unsigned char *out, size_t out_len, const char *wama, const struct adisk_disk_set *disks);
#ifdef TC_NATIVE_TEST
int validate_adisk_disk_advf(const char *value);
#endif
#endif

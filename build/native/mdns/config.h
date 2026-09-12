#ifndef TC_MDNS_CONFIG_H
#define TC_MDNS_CONFIG_H
#include "types.h"
void usage(const char *prog);
#ifdef TC_NATIVE_TEST
int validate_dns_label_text(const char *value, const char *field_name, int allow_dots);
#endif
int validate_single_dns_label(const char *value, const char *field_name);
int validate_generated_dns_label(const char *value, const char *field_name);
#ifdef TC_NATIVE_TEST
int escape_dns_label(char *out, size_t out_len, const char *label);
#endif
int build_instance_fqdn(char *out, size_t out_len, const char *instance_name, const char *service_type);
int build_host_fqdn(char *out, size_t out_len, const char *host_label);
#ifdef TC_NATIVE_TEST
void trim_ascii_whitespace(char *value);
#endif
#ifdef TC_NATIVE_TEST
int add_adisk_disk_config(struct config *cfg, const char *share_name, const char *disk_key,
                                 const char *adisk_uuid, const char *adisk_disk_advf);
#endif
int parse_adisk_shares_file(struct config *cfg, const char *path);
#ifdef TC_NATIVE_TEST
int adisk_configured(const struct config *cfg);
#endif
int adisk_enabled(const struct config *cfg);
int smb_enabled(const struct config *cfg);
int afp_enabled(const struct config *cfg);
int is_airport_enabled(const struct config *cfg);
#ifdef TC_NATIVE_TEST
int is_airport_usb_printer_enabled(const struct config *cfg);
#endif
int is_riousbprint_enabled(const struct config *cfg);
int is_pdl_datastream_enabled(const struct config *cfg);
const struct config *mdns_config_for_scope(struct config *scoped,
                                                  const struct config *cfg,
                                                  enum mdns_service_scope scope);
#endif

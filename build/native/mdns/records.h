#ifndef TC_MDNS_RECORDS_H
#define TC_MDNS_RECORDS_H
#include "types.h"
int build_model_txt(char *out, size_t out_len, const char *device_model);
int build_adisk_system_txt(char *out, size_t out_len, const char *wama);
#ifdef TC_NATIVE_TEST
int normalize_mac_for_airport_txt(char *out, size_t out_len, const char *value, const char *field_name);
#endif
#ifdef TC_NATIVE_TEST
int validate_adisk_disk_advf(const char *value);
#endif
int build_adisk_disk_txt(char *out, size_t out_len, const char *disk_key, const char *share_name, const char *adisk_uuid, const char *adisk_disk_advf);
#ifdef TC_NATIVE_TEST
int validate_airport_ascii_field(const char *value, const char *field_name);
#endif
int build_airport_txt(char *out, size_t out_len, const struct config *cfg);
#ifdef TC_NATIVE_TEST
int validate_txt_ascii_field(const char *value, const char *field_name);
#endif
#ifdef TC_NATIVE_TEST
int append_txt_itemf(char storage[][MAX_TXT_STRING + 1],
                            const char *txts[],
                            size_t *count,
                            size_t max_count,
                            const char *format,
                            ...);
#endif
#ifdef TC_NATIVE_TEST
int build_riousbprint_pdl(char *out, size_t out_len, const char *cmd);
#endif
#ifdef TC_NATIVE_TEST
int validate_airport_usb_printer_txt_fields(const struct config *cfg);
#endif
#ifdef TC_NATIVE_TEST
int append_airport_usb_printer_intro_txt_items(const struct config *cfg,
                                                      char storage[][MAX_TXT_STRING + 1],
                                                      const char *txts[],
                                                      size_t *txt_count,
                                                      size_t max_count);
#endif
#ifdef TC_NATIVE_TEST
int append_airport_usb_printer_device_txt_items(const struct config *cfg,
                                                       char storage[][MAX_TXT_STRING + 1],
                                                       const char *txts[],
                                                       size_t *txt_count,
                                                       size_t max_count);
#endif
int build_riousbprint_txt_items(const struct config *cfg,
                                       char storage[][MAX_TXT_STRING + 1],
                                       const char *txts[],
                                       size_t *txt_count);
int build_pdl_datastream_txt_items(const struct config *cfg,
                                          char storage[][MAX_TXT_STRING + 1],
                                          const char *txts[],
                                          size_t *txt_count);
int add_adisk_records(uint8_t *buf, size_t *off, size_t cap, const struct config *cfg, uint32_t ttl, int *answers);
int add_device_info_records(uint8_t *buf, size_t *off, size_t cap, const struct config *cfg, uint32_t ttl, int *answers);
int add_airport_records(uint8_t *buf, size_t *off, size_t cap, const struct config *cfg, uint32_t ttl, int *answers);
int add_riousbprint_records(uint8_t *buf, size_t *off, size_t cap, const struct config *cfg, uint32_t ttl, int *answers);
int add_pdl_datastream_records(uint8_t *buf, size_t *off, size_t cap, const struct config *cfg, uint32_t ttl, int *answers);
int add_empty_txt_service_records(uint8_t *buf, size_t *off, size_t cap, const struct config *cfg,
                                         const char *service_type, uint16_t port,
                                         uint32_t ttl, int *answers);
#ifdef TC_NATIVE_TEST
int plan_empty_txt_service_records(struct planned_rr_set *set,
                                          int routes,
                                          const struct config *cfg,
                                          const char *service_type,
                                          uint16_t port,
                                          const char *instance_fqdn,
                                          const struct link_context *link,
                                          int include_ptr,
                                          int include_srv,
                                          int include_txt,
                                          int include_a,
                                          int include_aaaa);
#endif
int plan_smb_records(struct planned_rr_set *set,
                            int routes,
                            const struct config *cfg,
                            const char *instance_fqdn,
                            const struct link_context *link,
                            int include_ptr,
                            int include_srv,
                            int include_txt,
                            int include_a,
                            int include_aaaa);
int plan_afp_records(struct planned_rr_set *set,
                            int routes,
                            const struct config *cfg,
                            const char *instance_fqdn,
                            const struct link_context *link,
                            int include_ptr,
                            int include_srv,
                            int include_txt,
                            int include_a,
                            int include_aaaa);
int plan_adisk_records(struct planned_rr_set *set,
                              int routes,
                              const struct config *cfg,
                              const char *instance_fqdn,
                              const struct link_context *link,
                              int include_ptr,
                              int include_srv,
                              int include_txt,
                              int include_a,
                              int include_aaaa);
int plan_device_info_records(struct planned_rr_set *set,
                                    int routes,
                                    const struct config *cfg,
                                    const char *instance_fqdn,
                                    const struct link_context *link,
                                    int include_ptr,
                                    int include_srv,
                                    int include_txt,
                                    int include_a,
                                    int include_aaaa);
int plan_airport_records(struct planned_rr_set *set,
                                int routes,
                                const struct config *cfg,
                                const char *instance_fqdn,
                                const struct link_context *link,
                                int include_ptr,
                                int include_srv,
                                int include_txt,
                                int include_a,
                                int include_aaaa);
int plan_riousbprint_records(struct planned_rr_set *set,
                                    int routes,
                                    const struct config *cfg,
                                    const char *instance_fqdn,
                                    const struct link_context *link,
                                    int include_ptr,
                                    int include_srv,
                                    int include_txt,
                                    int include_a,
                                    int include_aaaa);
int plan_pdl_datastream_records(struct planned_rr_set *set,
                                       int routes,
                                       const struct config *cfg,
                                       const char *instance_fqdn,
                                       const struct link_context *link,
                                       int include_ptr,
                                       int include_srv,
                                       int include_txt,
                                       int include_a,
                                       int include_aaaa);
#ifdef TC_NATIVE_TEST
int plan_service_type_enumeration_type(struct planned_rr_set *set,
                                              int routes,
                                              const char *service_type,
                                              uint32_t ttl);
#endif
int plan_service_type_enumeration_records(struct planned_rr_set *set,
                                                 int routes,
                                                 const struct config *cfg);
#endif

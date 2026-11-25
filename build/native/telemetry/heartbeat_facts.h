#ifndef TC_TELEMETRY_HEARTBEAT_FACTS_H
#define TC_TELEMETRY_HEARTBEAT_FACTS_H
#include "telemetry.h"
#include "../common/plan.h"

/* Everything a heartbeat reads from ACP, in one collection: the identity
 * keys (sySN, syAP, syAM) and the network plan's keys (syNm among them), 13
 * acp children one after another under one budget, stopping at the first
 * timeout (an ACPd that stopped answering one key will not answer the next)
 * or the first child that cannot start (a full process table stays full).
 *
 * The owner keeps one struct for its life. Identity values are kept once
 * read: a later read can fail because fork() does (a full process table) or
 * ACPd hangs, exactly when the device is in trouble, and acp answers a get
 * that failed with empty output and exit 0, so an empty answer never
 * replaces a kept value. A value never read goes out empty: the router is
 * named by its serial or hardware address, so a missing model or name does
 * not change which router a heartbeat is from. The plan is built from the
 * last validated one (device_plan_history_update, as discovery does), so a
 * collection that loses keys reports the roles it kept, with plan_error.
 * It is the plan of a device with a payload: discovery learns --diskless
 * from the manager's argv, and telemetry, started once, is not told, so a
 * diskless device's heartbeat can name roles its discovery does not use. */
enum { TC_ID_SERIAL, TC_ID_SYAP, TC_ID_SYAM, TC_ID_SYNM, TC_ID_COUNT };

/* The hardware address, sent as "mac" and as the router ID of a device with
 * no serial, is ACP's waMA, which every collection reads. Early in boot it
 * holds a placeholder (the same one on every unit of a model), which counts
 * as no answer. The last usable answer is kept: a hardware address does not
 * go away, and a later read fails exactly when the device is in trouble.
 * The daemon's boot heartbeat waits TC_BOOT_HOLD_SECONDS (planner.h), so its
 * collection usually finds it. Until then each heartbeat sends the
 * interface table's address of the moment (one sysctl, never kept;
 * addr_device_mac) unless it is a placeholder too, which names a router
 * whose ACP never answered this process (ACPd hung, or no process to fork);
 * else none. With no serial either there is no router ID: the heartbeat
 * fails acp_unavailable and the planner's retries collect again. */
/* 0 with out the address in lowercase, or -1 for text that is not one, an
 * all-zero or multicast address, or a known boot placeholder. */
int tc_mac_parse(const char *text, char out[18]);

/* Collections that went wrong, for the heartbeat's health object. */
struct tc_acp_counts {
    unsigned timeouts;         /* a key timed out */
    unsigned budget_exhausted; /* the budget ran out before every key answered */
    unsigned empty;            /* every identity key answered with nothing: an
                                  ACPd that cannot answer (acp prints nothing
                                  and exits 0 for a failed get) */
};

struct tc_heartbeat_facts {
    char id[TC_ID_COUNT][HEARTBEAT_MAX_FIELD];
    char wama[18];             /* the last usable waMA, lowercase, or empty */
    char mac[18];              /* the address this heartbeat sends, or empty */
    struct device_plan history;
    int have_history;
    struct device_plan last;   /* the last plan built, sent when nothing is collected */
    int have_last;
    long long last_ms;         /* when the last collection ran */
    int last_complete;         /* every key of it answered (a value or "not set"), and a serial
                                  if this process read one before */
    struct tc_acp_counts acp;
};

/* What one update did. */
struct tc_facts_round {
    int collected;      /* acp ran */
    int timed_out;      /* a key timed out */
    int budget_spent;   /* the collection budget ran out */
    int local_resource; /* a child could not start for want of processes, descriptors or memory */
};

/* The router ID a heartbeat names: the serial (ACP sySN) once read, else
 * "mac-" and the address this heartbeat sends, so a device whose ACP never
 * gave a serial still reports; every heartbeat also carries "mac", which
 * joins the two IDs. 0 with it in out, or -1 with out empty when there
 * is neither. out may be NULL with len 0 to ask only. */
int tc_heartbeat_facts_router_id(const struct tc_heartbeat_facts *f, char *out, size_t len);
/* Whether a heartbeat at now_ms may keep f's facts under reuse (struct
 * tc_facts_reuse): something was collected, under reuse.max_age seconds
 * before now_ms, and complete if reuse.complete_only says so (no key timed
 * out, ran out of budget, failed to start or was skipped, and the serial
 * came back if one was read before). Facts that name no router
 * (tc_heartbeat_facts_router_id) cannot build a payload and are never
 * kept, under any reuse. A clock that went back keeps nothing. */
int tc_heartbeat_facts_reusable(const struct tc_heartbeat_facts *f, struct tc_facts_reuse reuse, long long now_ms);
/* Keeps the facts when tc_heartbeat_facts_reusable allows, else reads acp
 * once; round says which. The address sent (mac) is chosen again either
 * way. */
void tc_heartbeat_facts_update(struct tc_heartbeat_facts *f, struct tc_facts_reuse reuse, struct tc_facts_round *round);
#endif

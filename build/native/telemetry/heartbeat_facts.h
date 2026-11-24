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
 * ACPd hangs, exactly when the device is in trouble. A value never read goes
 * out empty: the router is named by its serial or hardware address, so a
 * partial identity cannot make a different router (main's fallback ID was a
 * hash of syAP, syAM and syNm, which is why it refused one). The plan is built from
 * the last validated one (device_plan_history_update, as discovery does), so
 * a collection that loses keys reports the roles it kept, with plan_error. */
enum { TC_ID_SERIAL, TC_ID_SYAP, TC_ID_SYAM, TC_ID_SYNM, TC_ID_COUNT };

/* The hardware address, sent as "mac" and as the router ID of a device with
 * no serial, is ACP's waMA, which every collection reads. Early in boot it
 * holds a placeholder (the same one on every unit of a model), which counts
 * as no answer. The last usable answer is kept: a hardware address does not
 * go away, and a later read fails exactly when the device is in trouble.
 * The daemon's boot heartbeat waits TC_BOOT_HOLD_SECONDS (planner.h), so its
 * collection usually finds it. Until then each heartbeat sends the
 * interface table's address of the moment (one sysctl, never kept;
 * if_table_device_mac) unless it is a placeholder too, which names a router
 * whose ACP never answered this process (ACPd hung, or no process to fork);
 * else none. With no serial either there is no router ID: the heartbeat
 * fails acp_unavailable and the planner's retries collect again. */
/* 0 with out the address in lowercase, or -1 for text that is not one, an
 * all-zero or multicast address, or a known boot placeholder. */
int tc_mac_parse(const char *text, char out[18]);

struct tc_heartbeat_facts {
    char id[TC_ID_COUNT][HEARTBEAT_MAX_FIELD];
    int answered[TC_ID_COUNT]; /* acp answered the key once (a value or "not set") */
    char wama[18];             /* the last usable waMA, lowercase, or empty */
    char mac[18];              /* the address this heartbeat sends, or empty */
    struct device_plan history;
    int have_history;
    struct device_plan last;   /* the last plan built, sent when nothing is collected */
    int have_last;
    long long last_ms;         /* when the last collection ran */
    int last_complete;         /* every key of it answered: a value or "not set" */
    unsigned acp_timeouts;     /* collections in which a key timed out */
    unsigned acp_budget_exhausted; /* collections that ran out of budget before every key answered */
};

/* What one update did. */
struct tc_facts_round {
    int collected;      /* acp ran */
    int timed_out;      /* a key timed out */
    int budget_spent;   /* the collection budget ran out */
    int local_resource; /* a child could not start for want of processes, descriptors or memory */
};

/* A heartbeat retried after a network or server failure may skip acp: the
 * last collection was complete (no key timed out, ran out of budget,
 * failed to start or was skipped) and ran less than TC_FACTS_REUSE_SECONDS
 * before now_ms. Usually they are the facts the failed heartbeat carried;
 * an outage that outlasts the limit gets a fresh read on its next retry. */
#ifndef TC_FACTS_REUSE_SECONDS
#define TC_FACTS_REUSE_SECONDS 3600
#endif
int tc_heartbeat_facts_fresh(const struct tc_heartbeat_facts *f, long long now_ms);
/* Reads acp when refresh is set or nothing was ever collected. */
void tc_heartbeat_facts_update(struct tc_heartbeat_facts *f, int refresh, struct tc_facts_round *round);
#endif

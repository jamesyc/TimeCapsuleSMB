#ifndef TC_TELEMETRY_HEARTBEAT_FACTS_H
#define TC_TELEMETRY_HEARTBEAT_FACTS_H
#include "telemetry.h"
#include "../common/plan.h"

/* Everything a heartbeat reads from ACP, in one collection: the identity
 * keys (sySN, syAP, syAM) and the network plan's keys (syNm among them), 13
 * acp children one after another under one budget, stopping at the first
 * timeout (an ACPd that stopped answering one key will not answer the next).
 *
 * The owner keeps one struct for its life. Identity values are kept once
 * read: a later read can fail because fork() does (a full process table) or
 * ACPd hangs, exactly when the device is in trouble. The plan is built from
 * the last validated one (device_plan_history_update, as discovery does), so
 * a collection that loses keys reports the roles it kept, with plan_error.
 * The hardware address is read once from the interface table, with no acp. */
enum { TC_ID_SERIAL, TC_ID_SYAP, TC_ID_SYAM, TC_ID_SYNM, TC_ID_COUNT };

struct tc_heartbeat_facts {
    char id[TC_ID_COUNT][HEARTBEAT_MAX_FIELD];
    int answered[TC_ID_COUNT]; /* acp answered the key once (a value or "not set") */
    char mac[18];              /* lowercase with colons, or empty */
    struct device_plan history;
    int have_history;
    struct device_plan last;   /* the last plan built, sent when nothing is collected */
    int have_last;
    long long last_ms;
    unsigned acp_timeouts;     /* collections in which a key timed out */
};

/* What one update did. */
struct tc_facts_round {
    int collected;      /* acp ran */
    int timed_out;      /* a key timed out */
    int local_resource; /* a child could not start for want of processes, descriptors or memory */
    int identity_lost;  /* an identity key never answered failed for another reason: send nothing */
};

/* Reads acp when refresh is set or nothing was ever collected. */
void tc_heartbeat_facts_update(struct tc_heartbeat_facts *f, int refresh, struct tc_facts_round *round);
#endif

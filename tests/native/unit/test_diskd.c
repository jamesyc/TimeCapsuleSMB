/* Every row of the phase table in build/native/README.md "Disk owner
 * handoff", on a simulated clock. */
#include "service/diskd.h"
#include "storage/settle.h"
#include <assert.h>
#include <string.h>

static const struct tc_diskd_identity STOCK = {229, 100}, LOOPBACK = {680, 200}, OTHER = {700, 300};

static struct tc_diskd_observation none(void) {
    return (struct tc_diskd_observation){.kernel_valid = 1, .valid = 1, .drained = 1};
}
static struct tc_diskd_observation one(struct tc_diskd_identity owner, int loopback, const struct tc_diskd *s) {
    struct tc_diskd_observation o = none();
    o.owners = 1;
    o.owner = owner;
    o.loopback = loopback;
    o.current_present = s->owner.pid == owner.pid && s->owner.born == owner.born;
    return o;
}
static int named(const struct tc_diskd *s, const char *phase, const char *reason) {
    return !strcmp(tc_diskd_phase_name(s->phase), phase) && !strcmp(tc_diskd_reason_name(s->reason), reason);
}
/* Drive a fresh machine to READY through a launch of LOOPBACK. */
static void ready(struct tc_diskd *s) {
    struct tc_diskd_observation o = none();
    tc_diskd_init(s, 0);
    assert(tc_diskd_step(s, &o, 1) == TC_DISKD_LAUNCH);
    o.launch = 1;
    assert(!tc_diskd_step(s, &o, 2) && s->phase == TC_DISKD_LAUNCHING);
    o = one(LOOPBACK, 1, s);
    assert(tc_diskd_step(s, &o, 3) == TC_DISKD_PROBE && s->phase == TC_DISKD_STARTING);
    tc_diskd_probed(s, s->probe_token, 1, 4);
    assert(named(s, "ready", "none"));
}

static void boot_takeover_from_stock(void) {
    struct tc_diskd s;
    tc_diskd_init(&s, 0);
    struct tc_diskd_observation o = one(STOCK, 0, &s);
    o.drained = 0;
    assert(!tc_diskd_step(&s, &o, 1) && named(&s, "quiesce", "none"));
    o = one(STOCK, 0, &s);
    o.drained = 1;
    o.disk_work = 1;
    assert(!tc_diskd_step(&s, &o, 2) && named(&s, "quiesce", "maintenance") && s.since == 2);
    o.disk_work = 0;
    assert(tc_diskd_step(&s, &o, 3) == TC_DISKD_TERM && named(&s, "stopping", "none"));
    /* TERM is sent once, however long the shutdown takes. */
    for (long long t = 4; t < 1000000; t += 1000)
        assert(!tc_diskd_step(&s, &o, t) && s.phase == TC_DISKD_STOPPING);
    unsigned generation = s.generation;
    o = none();
    o.mounts = 1; /* the stock owner's cleanup has not finished unmounting */
    assert(tc_diskd_step(&s, &o, 2000000) == TC_DISKD_RETIRE && named(&s, "inspect", "residual_mounts"));
    assert(s.generation == generation + 1 && !s.owner.pid);
    o.mounts = -1;
    assert(!tc_diskd_step(&s, &o, 2000001) && named(&s, "inspect", "observation"));
    o.mounts = 0;
    o.disk_work = 1;
    assert(!tc_diskd_step(&s, &o, 2000002) && named(&s, "inspect", "maintenance"));
    o.disk_work = 0;
    assert(tc_diskd_step(&s, &o, 2000003) == TC_DISKD_LAUNCH && named(&s, "launching", "none"));
}
static void adopt_loopback_owner(void) {
    struct tc_diskd s;
    tc_diskd_init(&s, 0);
    struct tc_diskd_observation o = one(LOOPBACK, 1, &s);
    o.drained = 0; /* a running smbd does not hold back adoption */
    assert(tc_diskd_step(&s, &o, 1) == TC_DISKD_PROBE && named(&s, "starting", "none"));
    o = one(LOOPBACK, 1, &s);
    assert(!tc_diskd_step(&s, &o, 2)); /* one probe at a time */
    tc_diskd_probed(&s, s.probe_token, 1, 3);
    assert(tc_diskd_ready(&s));
    assert(!tc_diskd_step(&s, &o, 4) && named(&s, "ready", "none"));
}
static void launch_and_failed_launch(void) {
    struct tc_diskd s;
    tc_diskd_init(&s, 0);
    struct tc_diskd_observation o = none();
    o.launch = 1; /* our earlier child has not exec'd yet */
    assert(!tc_diskd_step(&s, &o, 1) && named(&s, "inspect", "none"));
    o.launch = 0;
    assert(tc_diskd_step(&s, &o, 2) == TC_DISKD_LAUNCH);
    long long now = 3;
    const unsigned delays[] = {1, 3, 12, 12};
    for (size_t i = 0; i < 4; i++) {
        tc_diskd_launch_exited(&s, now);
        assert(named(&s, "inspect", "relaunch_wait") && s.retry_at == now + delays[i] * TC_STORAGE_RETRY_MS);
        assert(!tc_diskd_step(&s, &o, s.retry_at - 1) && named(&s, "inspect", "relaunch_wait"));
        now = s.retry_at;
        assert(tc_diskd_step(&s, &o, now) == TC_DISKD_LAUNCH);
    }
    /* A child that vanished without our exit report goes back to inspect. */
    assert(!tc_diskd_step(&s, &o, now + 1) && s.phase == TC_DISKD_INSPECT);
    /* A launch that execs as a stock role is taken over like any stock owner. */
    tc_diskd_init(&s, 0);
    o = none();
    assert(tc_diskd_step(&s, &o, 1) == TC_DISKD_LAUNCH);
    o = one(STOCK, 0, &s);
    assert(tc_diskd_step(&s, &o, 2) == TC_DISKD_TERM && s.phase == TC_DISKD_STOPPING);
}
static void probe_replies(void) {
    struct tc_diskd s;
    tc_diskd_init(&s, 0);
    struct tc_diskd_observation o = one(LOOPBACK, 1, &s);
    long long now = 1;
    assert(tc_diskd_step(&s, &o, now) == TC_DISKD_PROBE);
    o = one(LOOPBACK, 1, &s);
    /* No answer: asked again 5 s after it ended, never backing off. */
    tc_diskd_probed(&s, s.probe_token, 0, now);
    assert(s.no_answers == 1 && s.probe_at == now + TC_STORAGE_RETRY_MS);
    assert(!tc_diskd_step(&s, &o, s.probe_at - 1) && named(&s, "starting", "none"));
    now = s.probe_at;
    assert(tc_diskd_step(&s, &o, now) == TC_DISKD_PROBE);
    /* The second in a row, here a probe that ran out its 20 s, sets the reason. */
    tc_diskd_probed(&s, s.probe_token, 0, now + 20000);
    assert(s.probe_at == now + 20000 + TC_STORAGE_RETRY_MS);
    assert(!tc_diskd_step(&s, &o, now + 20001) && named(&s, "starting", "no_answer") && s.since == now + 20001);
    long long since = s.since;
    for (int i = 0; i < 3; i++) {
        now = s.probe_at;
        assert(tc_diskd_step(&s, &o, now) == TC_DISKD_PROBE);
        tc_diskd_probed(&s, s.probe_token, 0, now);
        assert(s.probe_at == now + TC_STORAGE_RETRY_MS);
        assert(!tc_diskd_step(&s, &o, now + 1) && named(&s, "starting", "no_answer") && s.since == since);
    }
    /* An answer ends it and forgets earlier launch failures. */
    now = s.probe_at;
    assert(tc_diskd_step(&s, &o, now) == TC_DISKD_PROBE);
    s.launch_failures = 2;
    tc_diskd_probed(&s, s.probe_token, 1, now);
    assert(named(&s, "ready", "none") && !s.no_answers && !s.launch_failures && !s.probe_at);
    /* A late reply cannot move READY. */
    tc_diskd_probed(&s, s.probe_token, 0, now + 1);
    assert(named(&s, "ready", "none"));
}
static void owner_exit_retires_generation(void) {
    struct tc_diskd s;
    ready(&s); /* ready at 4 */
    unsigned generation = s.generation, token = s.probe_token;
    struct tc_diskd_observation o = none();
    o.valid = 0; /* the replacement's role is unknown, but the exit is not */
    assert(tc_diskd_step(&s, &o, 10) == TC_DISKD_RETIRE && named(&s, "inspect", "observation"));
    assert(s.generation == generation + 1 && s.probe_token == token + 1 && !tc_diskd_ready(&s));
    /* A reply from the retired owner is ignored. */
    tc_diskd_probed(&s, token, 1, 11);
    assert(s.phase == TC_DISKD_INSPECT);
    /* Our diskd exited: the next launch waits, like one after a launch that
     * never exec'd. */
    o.valid = 1;
    assert(!tc_diskd_step(&s, &o, 12) && named(&s, "inspect", "relaunch_wait"));
    assert(s.retry_at == 10 + TC_STORAGE_RETRY_MS && tc_diskd_step(&s, &o, s.retry_at) == TC_DISKD_LAUNCH);
    /* One that answers and then dies, even after ten minutes in ready, waits
     * 5 s each time: the answer reset the count. */
    long long now = s.retry_at;
    for (int i = 0; i < 3; i++) {
        o = one(LOOPBACK, 1, &s);
        assert(tc_diskd_step(&s, &o, now + 1) == TC_DISKD_PROBE);
        tc_diskd_probed(&s, s.probe_token, 1, now + 2);
        o = one(LOOPBACK, 1, &s);
        assert(!tc_diskd_step(&s, &o, now + 2 + 600000) && tc_diskd_ready(&s));
        o = none();
        assert(tc_diskd_step(&s, &o, now + 3 + 600000) == TC_DISKD_RETIRE);
        assert(s.retry_at == now + 3 + 600000 + TC_STORAGE_RETRY_MS && named(&s, "inspect", "relaunch_wait"));
        now = s.retry_at;
        assert(tc_diskd_step(&s, &o, now) == TC_DISKD_LAUNCH);
    }
    /* One that dies before it ever answers backs off further: 15 s after
     * the 5 s already counted, then 60 s. */
    const unsigned delays[] = {3, 12, 12};
    for (size_t i = 0; i < 3; i++) {
        o = one(LOOPBACK, 1, &s);
        assert(tc_diskd_step(&s, &o, now + 1) == TC_DISKD_PROBE);
        o = none();
        assert(tc_diskd_step(&s, &o, now + 2) == TC_DISKD_RETIRE);
        assert(s.retry_at == now + 2 + delays[i] * TC_STORAGE_RETRY_MS && named(&s, "inspect", "relaunch_wait"));
        now = s.retry_at;
        assert(tc_diskd_step(&s, &o, now) == TC_DISKD_LAUNCH);
    }
    /* A stock owner exiting after its TERM is the handoff: launch at once. */
    tc_diskd_init(&s, 0);
    o = one(STOCK, 0, &s);
    assert(tc_diskd_step(&s, &o, 1) == TC_DISKD_TERM);
    o = none();
    assert(tc_diskd_step(&s, &o, 2) == (TC_DISKD_RETIRE | TC_DISKD_LAUNCH) && named(&s, "launching", "none"));
    /* A reused PID is another owner. */
    ready(&s);
    struct tc_diskd_identity reused = {LOOPBACK.pid, LOOPBACK.born + 1};
    o = one(reused, 1, &s);
    assert(tc_diskd_step(&s, &o, 20) == (TC_DISKD_RETIRE | TC_DISKD_PROBE) && s.phase == TC_DISKD_STARTING);
    assert(s.owner.born == reused.born);
}
static void reaped_child_retires_at_once(void) {
    struct tc_diskd s;
    ready(&s);
    unsigned generation = s.generation;
    struct tc_diskd_identity other = {LOOPBACK.pid, LOOPBACK.born + 1};
    assert(!tc_diskd_owner_exited(&s, other, 5) && tc_diskd_ready(&s));
    assert(tc_diskd_owner_exited(&s, LOOPBACK, 6) == TC_DISKD_RETIRE && s.generation == generation + 1);
    assert(named(&s, "inspect", "relaunch_wait") && !s.owner.pid);
    /* The next sample, still showing it alive, cannot bring it back. */
    struct tc_diskd_observation o = one(LOOPBACK, 1, &s);
    assert(tc_diskd_step(&s, &o, 7) == TC_DISKD_PROBE && s.generation == generation + 1);
}
static void conflict_in_every_phase(void) {
    for (int phase = TC_DISKD_INSPECT; phase <= TC_DISKD_READY; phase++) {
        struct tc_diskd s;
        tc_diskd_init(&s, 0);
        s.phase = phase;
        s.owner = phase == TC_DISKD_INSPECT || phase == TC_DISKD_LAUNCHING ? (struct tc_diskd_identity){0}
                  : phase == TC_DISKD_QUIESCE || phase == TC_DISKD_STOPPING ? STOCK : LOOPBACK;
        int loopback = phase >= TC_DISKD_STARTING;
        struct tc_diskd_observation o = none();
        o.owners = 2;
        o.current_present = 1;
        unsigned generation = s.generation, token = s.probe_token;
        /* Ready and starting both wait in starting: nothing that needs ready
         * starts while two diskd run. Other phases keep theirs. */
        enum tc_diskd_phase during = loopback ? TC_DISKD_STARTING : (enum tc_diskd_phase)phase;
        assert(!tc_diskd_step(&s, &o, 5) && s.phase == during && !tc_diskd_ready(&s));
        assert(named(&s, tc_diskd_phase_name(during), "conflict") && s.since == 5 && s.generation == generation);
        assert(loopback ? s.probe_token != token : s.probe_token == token);
        assert(!tc_diskd_step(&s, &o, 6) && s.since == 5);
        if (!s.owner.pid)
            continue;
        /* One owner left. The same one continues; a loopback owner must
         * answer again, since the other diskd's exit may have taken its names. */
        struct tc_diskd kept = s;
        o = one(s.owner, loopback, &s);
        unsigned actions = tc_diskd_step(&kept, &o, 7);
        enum tc_diskd_phase expected = phase == TC_DISKD_QUIESCE ? TC_DISKD_STOPPING : during;
        assert(kept.phase == expected && kept.reason == TC_DISKD_NONE && kept.generation == generation);
        assert(actions == (loopback ? TC_DISKD_PROBE : phase == TC_DISKD_QUIESCE ? TC_DISKD_TERM : 0u));
        o = one(OTHER, 1, &s);
        assert(tc_diskd_step(&s, &o, 7) & TC_DISKD_RETIRE);
        assert(s.generation == generation + 1 && s.owner.pid == OTHER.pid && s.phase == TC_DISKD_STARTING);
    }
}
static void answers_from_before_a_conflict_do_not_count(void) {
    struct tc_diskd s;
    tc_diskd_init(&s, 0);
    struct tc_diskd_observation o = one(LOOPBACK, 1, &s);
    assert(tc_diskd_step(&s, &o, 1) == TC_DISKD_PROBE);
    unsigned started = s.probe_token;
    o = one(LOOPBACK, 1, &s);
    o.owners = 2;
    tc_diskd_step(&s, &o, 2);
    /* Answered by the registry the second diskd may yet clear. */
    tc_diskd_probed(&s, started, 1, 3);
    assert(named(&s, "starting", "conflict"));
    o.owners = 1;
    assert(tc_diskd_step(&s, &o, 4) == TC_DISKD_PROBE && s.probe_token != started);
    tc_diskd_probed(&s, started, 1, 5);
    assert(s.phase == TC_DISKD_STARTING);
    tc_diskd_probed(&s, s.probe_token, 1, 6);
    assert(named(&s, "ready", "none"));
    /* From ready: a conflict takes it back to starting at once, and only an
     * answer after it brings ready back. */
    o.owners = 2;
    assert(!tc_diskd_step(&s, &o, 10) && named(&s, "starting", "conflict") && !tc_diskd_ready(&s));
    o.owners = 1;
    assert(tc_diskd_step(&s, &o, 11) == TC_DISKD_PROBE && named(&s, "starting", "none"));
    tc_diskd_probed(&s, s.probe_token, 1, 12);
    assert(named(&s, "ready", "none"));
}
static void probe_failure_reasons(void) {
    struct tc_diskd s;
    tc_diskd_init(&s, 0);
    struct tc_diskd_observation o = one(LOOPBACK, 1, &s);
    long long now = 1;
    assert(tc_diskd_step(&s, &o, now) == TC_DISKD_PROBE);
    o = one(LOOPBACK, 1, &s);
    tc_diskd_probed(&s, s.probe_token, 0, now);
    now = s.probe_at;
    assert(tc_diskd_step(&s, &o, now) == TC_DISKD_PROBE && named(&s, "starting", "none"));
    tc_diskd_probed(&s, s.probe_token, 0, now);
    /* A disk check explains a silent start: no restart advice while it runs. */
    o.disk_work = 1;
    assert(!tc_diskd_step(&s, &o, now + 1) && named(&s, "starting", "maintenance"));
    o.disk_work = 0;
    assert(!tc_diskd_step(&s, &o, now + 2) && named(&s, "starting", "no_answer") && s.since == now + 2);
}
static void stopping_waits_for_disk_work(void) {
    struct tc_diskd s;
    tc_diskd_init(&s, 0);
    struct tc_diskd_observation o = one(STOCK, 0, &s);
    assert(tc_diskd_step(&s, &o, 1) == TC_DISKD_TERM);
    o = one(STOCK, 0, &s);
    o.disk_work = 1; /* its TERM waits for the check to return */
    assert(!tc_diskd_step(&s, &o, 2) && named(&s, "stopping", "maintenance"));
    o.disk_work = 0;
    assert(!tc_diskd_step(&s, &o, 3) && named(&s, "stopping", "none") && s.since == 3);
}
static void ready_survives_audit_failure_and_helpers(void) {
    struct tc_diskd s;
    ready(&s);
    struct tc_diskd_observation o = one(LOOPBACK, 1, &s);
    o.valid = 0;
    assert(!tc_diskd_step(&s, &o, 10) && named(&s, "ready", "observation") && tc_diskd_ready(&s));
    o.valid = 1;
    o.disk_work = 1; /* diskd's own fsck before a mount */
    o.drained = 0;
    assert(!tc_diskd_step(&s, &o, 11) && named(&s, "ready", "none"));
    o.kernel_valid = 0;
    assert(!tc_diskd_step(&s, &o, 12) && named(&s, "ready", "observation") && tc_diskd_ready(&s));
}

int main(void) {
    boot_takeover_from_stock();
    adopt_loopback_owner();
    launch_and_failed_launch();
    probe_replies();
    owner_exit_retires_generation();
    reaped_child_retires_at_once();
    conflict_in_every_phase();
    answers_from_before_a_conflict_do_not_count();
    probe_failure_reasons();
    stopping_waits_for_disk_work();
    ready_survives_audit_failure_and_helpers();
    return 0;
}

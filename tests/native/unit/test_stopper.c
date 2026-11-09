/* The foreign-process stopper (service/stopper.c), on a simulated clock. */
#include "service/stopper.h"
#include <assert.h>
#include <signal.h>
#include <string.h>

static const struct tc_stop_ours OURS = {.smb_group = 500, .rsync_group = 600, .discovery = 700, .telemetry = 800};
static struct tc_process_table table;
static struct tc_stop_signal out[TC_PROCESS_MAX];
static unsigned blocked;

static void rows(const struct tc_process_info *p, size_t n) {
    table.count = n;
    memcpy(table.processes, p, n * sizeof(p[0]));
}
static struct tc_process_info proc(pid_t pid, pid_t parent, pid_t group, enum tc_process_role role) {
    return (struct tc_process_info){pid, parent, group, (unsigned long long)pid * 10, role};
}

static void ours_are_never_targets(void) {
    struct tc_stopper s = {0};
    const struct tc_process_info p[] = {
        proc(501, 1, 500, TC_PROC_SMBD),      proc(502, 501, 500, TC_PROC_SMBD), /* a worker */
        proc(601, 1, 600, TC_PROC_RSYNC),     proc(700, 1, 700, TC_PROC_DISCOVERY),
        proc(701, 700, 700, TC_PROC_WCIFSND), proc(800, 1, 800, TC_PROC_TELEMETRY),
        proc(229, 190, 15, TC_PROC_DISKD),    proc(286, 1, 286, TC_PROC_DISKD_LOOPBACK),
    };
    rows(p, sizeof(p) / sizeof(p[0]));
    assert(tc_stopper_step(&s, &table, &OURS, 0, out, &blocked) == 0 && blocked == 0 && s.count == 0);
}
static void each_role_blocks_its_own(void) {
    const struct {
        struct tc_process_info p;
        unsigned block;
    } cases[] = {
        {{901, 190, 15, 9010, TC_PROC_WCIFSFS}, BLOCK_SMB | BLOCK_DISCOVERY},
        {{902, 190, 15, 9020, TC_PROC_WCIFSND}, BLOCK_DISCOVERY},
        {{903, 1, 903, 9030, TC_PROC_SMBD}, BLOCK_SMB},
        {{904, 1, 904, 9040, TC_PROC_RSYNC}, BLOCK_RSYNC},
        {{905, 1, 905, 9050, TC_PROC_DISCOVERY}, BLOCK_DISCOVERY},
        {{906, 1, 906, 9060, TC_PROC_TELEMETRY}, BLOCK_TELEMETRY},
    };
    for (size_t i = 0; i < sizeof(cases) / sizeof(cases[0]); i++) {
        struct tc_stopper s = {0};
        rows(&cases[i].p, 1);
        assert(tc_stopper_step(&s, &table, &OURS, 0, out, &blocked) == 1);
        assert(blocked == cases[i].block && out[0].pid == cases[i].p.pid && out[0].sig == SIGTERM);
        assert(out[0].role == cases[i].p.role && out[0].group == cases[i].p.group && !out[0].first_kill);
    }
    /* With none of ours running, every copy is foreign. */
    struct tc_stopper s = {0};
    const struct tc_stop_ours none = {0, 0, 0, 0};
    const struct tc_process_info p[] = {proc(701, 700, 700, TC_PROC_WCIFSND), proc(501, 1, 500, TC_PROC_SMBD)};
    rows(p, 2);
    assert(tc_stopper_step(&s, &table, &none, 0, out, &blocked) == 2 && blocked == (BLOCK_DISCOVERY | BLOCK_SMB));
}
static void term_then_kill_logged_once(void) {
    struct tc_stopper s = {0};
    const struct tc_process_info p[] = {proc(904, 1, 904, TC_PROC_RSYNC)};
    rows(p, 1);
    /* TERM at every audit until TC_STALE_KILL_MS after first seen. */
    assert(tc_stopper_step(&s, &table, &OURS, 1000, out, &blocked) == 1 && out[0].sig == SIGTERM);
    assert(tc_stopper_step(&s, &table, &OURS, 1000 + TC_STALE_KILL_MS - 1, out, &blocked) == 1);
    assert(out[0].sig == SIGTERM && out[0].waited_ms == TC_STALE_KILL_MS - 1);
    /* Then SIGKILL, logged the first time only. */
    assert(tc_stopper_step(&s, &table, &OURS, 1000 + TC_STALE_KILL_MS, out, &blocked) == 1);
    assert(out[0].sig == SIGKILL && out[0].first_kill && out[0].waited_ms == TC_STALE_KILL_MS);
    assert(tc_stopper_step(&s, &table, &OURS, 2000 + TC_STALE_KILL_MS, out, &blocked) == 1);
    assert(out[0].sig == SIGKILL && !out[0].first_kill && blocked == BLOCK_RSYNC);
    /* Gone from the table: forgotten, and nothing blocks. */
    rows(p, 0);
    assert(tc_stopper_step(&s, &table, &OURS, 3000 + TC_STALE_KILL_MS, out, &blocked) == 0 && !blocked && !s.count);
}
static void telemetry_is_never_killed(void) {
    struct tc_stopper s = {0};
    const struct tc_process_info p[] = {proc(906, 1, 906, TC_PROC_TELEMETRY)};
    rows(p, 1);
    for (long long now = 0; now < 10 * TC_STALE_KILL_MS; now += 1000) {
        assert(tc_stopper_step(&s, &table, &OURS, now, out, &blocked) == 1);
        assert(out[0].sig == SIGTERM && !out[0].first_kill && blocked == BLOCK_TELEMETRY);
    }
}
static void reused_pid_is_a_new_target(void) {
    struct tc_stopper s = {0};
    struct tc_process_info p = proc(904, 1, 904, TC_PROC_RSYNC);
    rows(&p, 1);
    assert(tc_stopper_step(&s, &table, &OURS, 0, out, &blocked) == 1);
    /* The first exits; another process gets its PID before the next audit. */
    p.born += 1;
    rows(&p, 1);
    assert(tc_stopper_step(&s, &table, &OURS, TC_STALE_KILL_MS, out, &blocked) == 1);
    assert(out[0].sig == SIGTERM && out[0].waited_ms == 0);
}
static void several_at_once(void) {
    struct tc_stopper s = {0};
    const struct tc_process_info p[] = {proc(901, 190, 15, TC_PROC_WCIFSFS), proc(904, 1, 904, TC_PROC_RSYNC),
                                        proc(501, 1, 500, TC_PROC_SMBD)};
    rows(p, 3);
    assert(tc_stopper_step(&s, &table, &OURS, 0, out, &blocked) == 2 && s.count == 2);
    assert(blocked == (BLOCK_SMB | BLOCK_DISCOVERY | BLOCK_RSYNC));
    /* One goes; the other keeps its first sighting. */
    rows(&p[1], 1);
    assert(tc_stopper_step(&s, &table, &OURS, TC_STALE_KILL_MS, out, &blocked) == 1 && out[0].sig == SIGKILL);
}

int main(void) {
    ours_are_never_targets();
    each_role_blocks_its_own();
    term_then_kill_logged_once();
    telemetry_is_never_killed();
    reused_pid_is_a_new_target();
    several_at_once();
    return 0;
}

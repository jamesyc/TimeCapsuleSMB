#include "health.h"

static const char *const probe_result_names[] = {"none", "ok", "closed", "silent", "down", "absent"};

/* snprintf at json + *used, failing instead of truncating. */
static int put(char *json, size_t cap, size_t *used, const char *format, ...) {
    va_list args;
    int n;
    if (*used >= cap) return -1;
    va_start(args, format);
    n = vsnprintf(json + *used, cap - *used, format, args);
    va_end(args);
    if (n < 0 || (size_t)n >= cap - *used) return -1;
    *used += (size_t)n;
    return 0;
}

/* Kernel names are short ASCII; anything else becomes '?', so no JSON
 * escaping is needed. */
static const char *safe(const char *text, char *out, size_t size) {
    size_t i;
    for (i = 0; text[i] && i + 1 < size; i++)
        out[i] = text[i] >= 0x20 && text[i] < 0x7f && text[i] != '"' && text[i] != '\\' ? text[i] : '?';
    out[i] = 0;
    return out;
}

static int put_long_or_null(char *json, size_t cap, size_t *used, const char *key, long value, int have) {
    return have ? put(json, cap, used, ",\"%s\":%ld", key, value) : put(json, cap, used, ",\"%s\":null", key);
}

static int put_snapshot(char *json, size_t cap, size_t *used, const char *key, const struct tc_snapshot *s,
                        long long pagesize) {
    char name[TC_PROC_COMM];
    size_t i;
    if (!s->read) return put(json, cap, used, ",\"%s\":null", key);
    if (put(json, cap, used, ",\"%s\":{\"at\":%ld,\"n\":%u,\"system\":%u,\"zombie\":%u,\"blocked\":%u", key, s->at,
            s->procs, s->system, s->zombies, s->blocked) ||
        (s->smbd_children >= 0 ? put(json, cap, used, ",\"smbd_children\":%d", s->smbd_children)
                               : put(json, cap, used, ",\"smbd_children\":null")) ||
        put(json, cap, used, ",\"groups\":["))
        return -1;
    for (i = 0; i < s->groups; i++)
        if (put(json, cap, used, "%s[\"%s\",%u,%u]", i ? "," : "", safe(s->group[i].comm, name, sizeof(name)),
                s->group[i].depth, s->group[i].count))
            return -1;
    if (put(json, cap, used, "],\"waits\":[")) return -1;
    for (i = 0; i < s->waits; i++)
        if (put(json, cap, used, "%s[\"%s\",%u]", i ? "," : "", safe(s->wait[i].wmesg, name, sizeof(name)),
                s->wait[i].count))
            return -1;
    if (put(json, cap, used, "],\"largest_kb\":[")) return -1;
    for (i = 0; i < s->largest; i++)
        if (put(json, cap, used, "%s[\"%s\",%llu]", i ? "," : "", safe(s->large[i].comm, name, sizeof(name)),
                (unsigned long long)s->large[i].rss_pages * (unsigned long long)pagesize / 1024ULL))
            return -1;
    if (put(json, cap, used, "],\"most_fds\":[")) return -1;
    for (i = 0; i < s->most_fds; i++)
        if (put(json, cap, used, "%s[\"%s\",%u]", i ? "," : "", safe(s->fd[i].comm, name, sizeof(name)), s->fd[i].fds))
            return -1;
    return put(json, cap, used, "]}");
}

/* ,"key":[names of the bits set] */
static int put_names(char *json, size_t cap, size_t *used, const char *key, unsigned bits) {
    size_t i;
    int first = 1;
    if (put(json, cap, used, ",\"%s\":[", key)) return -1;
    for (i = 0; i < TC_EV_COUNT; i++)
        if (bits & (1u << i)) {
            if (put(json, cap, used, "%s\"%s\"", first ? "" : ",", tc_health_event_names[i])) return -1;
            first = 0;
        }
    return put(json, cap, used, "]");
}

/* Times are seconds on this process's monotonic clock: comparable within
 * one telemetry process, not across a restart. */
int tc_health_append_json(char *json, size_t cap, size_t used, const struct tc_health *h, unsigned acp_timeouts,
                          unsigned acp_budget_exhausted, unsigned *token) {
    const struct tc_counters *c = &h->now;
    struct tc_failure_counts f;
    long long pagesize = c->have_mem && c->pagesize > 0 ? c->pagesize : 4096;
    size_t i;
    tc_failure_counts(&f);
    if (put(json, cap, &used, ",\"health\":{\"sampled_at\":%ld", h->current.at) ||
        put_long_or_null(json, cap, &used, "maxproc", c->maxproc, c->maxproc > 0) ||
        put_snapshot(json, cap, &used, "procs", &h->current, pagesize) ||
        put_snapshot(json, cap, &used, "procs_peak", &h->peak, pagesize) ||
        put_snapshot(json, cap, &used, "procs_first_high", &h->first_high, pagesize) ||
        put(json, cap, &used, ",\"procs_near_full_samples\":%u,\"procs_high_events\":%u,\"files_high_events\":%u",
            h->near_full, h->procs.events, h->files.events) ||
        put(json, cap, &used, ",\"files\":{\"n\":") ||
        (c->files >= 0 ? put(json, cap, &used, "%d", c->files) : put(json, cap, &used, "null")) ||
        put_long_or_null(json, cap, &used, "max", c->maxfiles, c->maxfiles > 0) ||
        put_long_or_null(json, cap, &used, "peak", h->peak_files, c->files >= 0 || h->peak_files > 0) ||
        put_long_or_null(json, cap, &used, "peak_at", h->peak_files_at, h->peak_files > 0) || put(json, cap, &used, "}"))
        return -1;
    if (c->have_mem) {
        if (put(json, cap, &used,
                ",\"mem\":{\"page_kb\":%lld,\"free\":%lld,\"min_free\":%lld,\"min_free_at\":%ld,\"freetarg\":%lld,"
                "\"active\":%lld,\"inactive\":%lld,\"anon\":%lld,\"file\":%lld,\"fltnoram\":%lld,\"pdscans\":%lld}",
                pagesize / 1024, c->free, h->min_free, h->min_free_at, c->freetarg, c->active, c->inactive, c->anon,
                c->file, c->fltnoram, c->pdscans))
            return -1;
    } else if (put(json, cap, &used, ",\"mem\":null")) {
        return -1;
    }
    if (c->have_mbuf) {
        if (put(json, cap, &used, ",\"mbuf\":{\"drops\":%lu,\"waits\":%lu", c->mbuf_drops, c->mbuf_waits) ||
            put_long_or_null(json, cap, &used, "clusters_max", c->nmbclusters, c->nmbclusters > 0) ||
            put(json, cap, &used, "}"))
            return -1;
    } else if (put(json, cap, &used, ",\"mbuf\":null")) {
        return -1;
    }
    /* bytes: -1 unreadable, 0 an empty buffer. Each pattern: null (never),
     * or when this process first and last saw it in new text and how often,
     * and whether it was already there at the first read. */
    if (put(json, cap, &used, ",\"kmsg\":{\"bytes\":%ld", h->kmsg.bytes)) return -1;
    for (i = 0; i < TC_KMSG_PATTERNS; i++) {
        const struct tc_kmsg_sighting *s = &h->kmsg.seen[i];
        if (!s->n && !s->at_start) {
            if (put(json, cap, &used, ",\"%s\":null", tc_kmsg_names[i])) return -1;
            continue;
        }
        if (put(json, cap, &used, ",\"%s\":{", tc_kmsg_names[i]) ||
            (s->first ? put(json, cap, &used, "\"first\":%ld,\"last\":%ld", s->first, s->last)
                      : put(json, cap, &used, "\"first\":null,\"last\":null")) ||
            put(json, cap, &used, ",\"n\":%u,\"at_start\":%s}", s->n, s->at_start ? "true" : "false"))
            return -1;
    }
    if (put(json, cap, &used, "},\"probes\":{")) return -1;
    for (i = 0; i < TC_PROBES; i++) {
        const struct tc_probe_track *p = &h->probe[i];
        if (put(json, cap, &used,
                "%s\"%s\":{\"last\":\"%s\",\"streak\":%u,\"ok\":%u,\"closed\":%u,\"silent\":%u,\"down\":%u",
                i ? "," : "", tc_probe_names[i], probe_result_names[p->last], p->streak, p->ok, p->closed, p->silent,
                p->down) ||
            put_long_or_null(json, cap, &used, "first_fail_at", p->first_fail_at, p->first_fail_at != 0) ||
            put_long_or_null(json, cap, &used, "last_fail_at", p->last_fail_at, p->first_fail_at != 0) ||
            put(json, cap, &used, "}"))
            return -1;
    }
    if (put(json, cap, &used, "},\"probe_rounds_skipped\":%u", h->probe_rounds_skipped) ||
        put(json, cap, &used, ",\"fork_failures\":{\"eagain\":%u,\"enomem\":%u,\"other\":%u}", f.fork_eagain,
            f.fork_enomem, f.fork_other) ||
        put(json, cap, &used, ",\"socket_failures\":{\"emfile\":%u,\"enfile\":%u,\"enobufs\":%u,\"other\":%u}",
            f.socket_emfile, f.socket_enfile, f.socket_enobufs, f.socket_other) ||
        put(json, cap, &used, ",\"acp_timeouts\":%u,\"acp_budget_exhausted\":%u", acp_timeouts, acp_budget_exhausted) ||
        put_names(json, cap, &used, "triggers", h->pending) || put_names(json, cap, &used, "news", h->news) ||
        put(json, cap, &used, "}"))
        return -1;
    *token = h->news;
    return (int)used;
}

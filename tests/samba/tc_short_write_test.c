/* Patch 0072's short-WRITE check, frame walk, idle and end decisions, as compiled into
 * smbd: the runner extracts them from the patched smb2_server.c. A short
 * WRITE is built the way macOS sends one, its data cut off with the next
 * requests behind it; a correct client's WRITE must never be taken for one. */
#include "includes.h"
#include "smbd/smbd.h"
#include "smbd/globals.h"
#include "../libcli/smb/smb_common.h"

#include "tc_short_write.inc"

#define CHECK(x) do { if (!(x)) { fprintf(stderr, "%s:%d: %s\n", __FILE__, __LINE__, #x); exit(90); } } while (0)

#define SESSION 0x0000a00000000041ULL
#define WRITE_MID 100
#define LOW 90
#define RANGE 512
#define WRITE_DATA_OFS (SMB2_HDR_BODY + 0x30)

struct buf {
	uint8_t b[16384];
	size_t len;
};

static void header(uint8_t *h, uint16_t opcode, uint64_t mid, uint64_t session, uint16_t charge)
{
	memset(h, 0, SMB2_HDR_BODY);
	memcpy(h, "\xfeSMB", 4);
	SSVAL(h, SMB2_HDR_LENGTH, SMB2_HDR_BODY);
	SSVAL(h, SMB2_HDR_CREDIT_CHARGE, charge);
	SSVAL(h, SMB2_HDR_OPCODE, opcode);
	SBVAL(h, SMB2_HDR_MESSAGE_ID, mid);
	SBVAL(h, SMB2_HDR_SESSION_ID, session);
}

/* A WRITE PDU (no NBT header) announcing data_len bytes of data. */
static void write_pdu(struct buf *out, uint64_t mid, uint16_t charge, size_t data_len)
{
	uint8_t *body = out->b + SMB2_HDR_BODY;

	header(out->b, SMB2_OP_WRITE, mid, SESSION, charge);
	memset(body, 0, 0x30);
	SSVAL(body, 0x00, 0x31);
	SSVAL(body, 0x02, WRITE_DATA_OFS);
	SIVAL(body, 0x04, data_len);
	out->len = WRITE_DATA_OFS;
}

static void data(struct buf *out, uint8_t byte, size_t n)
{
	CHECK(out->len + n <= sizeof(out->b));
	memset(out->b + out->len, byte, n);
	out->len += n;
}

/* An NBT frame holding one request with body_len bytes after its header. */
static void frame(struct buf *out, uint16_t opcode, uint64_t mid, uint64_t session, size_t body_len)
{
	uint8_t *nbt = out->b + out->len;
	size_t pdu = SMB2_HDR_BODY + body_len;

	CHECK(out->len + NBT_HDR_SIZE + pdu <= sizeof(out->b));
	nbt[0] = 0x00;
	nbt[1] = (pdu >> 16) & 0xff;
	nbt[2] = (pdu >> 8) & 0xff;
	nbt[3] = pdu & 0xff;
	header(nbt + NBT_HDR_SIZE, opcode, mid, session, 1);
	memset(nbt + NBT_HDR_SIZE + SMB2_HDR_BODY, 0x5a, body_len);
	out->len += NBT_HDR_SIZE + pdu;
}

/* The header of the last frame added, whose body is body_len bytes. */
static uint8_t *last_header(struct buf *out, size_t body_len)
{
	return out->b + out->len - body_len - SMB2_HDR_BODY;
}

static enum tc_short_write check(const struct buf *pdu, size_t *resume)
{
	struct tc_scan scan = { .from = 0 };

	*resume = 0;
	return tc_short_write_check(pdu->b, pdu->len, true, LOW, RANGE, &scan, resume);
}

/* The PDU checked as smbd checks a WRITE while it arrives, chunk bytes at a
 * time, complete with its last bytes when completes is set: the first verdict
 * that is not whole, or the last, and how many bytes had arrived then. */
static enum tc_short_write check_arriving(const struct buf *pdu, size_t chunk, bool completes,
					  size_t *resume, size_t *seen)
{
	struct tc_scan scan = { .from = 0 };
	enum tc_short_write found;
	size_t len = 0;

	do {
		len = MIN(len + chunk, pdu->len);
		*resume = 0;
		found = tc_short_write_check(pdu->b, len, completes && len == pdu->len, LOW, RANGE,
					     &scan, resume);
	} while (found == TC_WRITE_WHOLE && len < pdu->len);
	*seen = len;
	return found;
}

/* Arriving in any chunks, a PDU gets the verdict it gets whole. */
static void same_arriving(const struct buf *pdu)
{
	static const size_t chunks[] = { 1, 2, 3, 5, 64, 100, 1000, 16384 };
	enum tc_short_write whole;
	size_t i, resume, at, seen;

	whole = check(pdu, &at);
	for (i = 0; i < ARRAY_SIZE(chunks); i++) {
		CHECK(check_arriving(pdu, chunks[i], true, &resume, &seen) == whole);
		CHECK(whole != TC_WRITE_SHORT || resume == at);
	}
}

/* The bytes a short WRITE's PDU holds: the WRITE as announced, but with
 * cut bytes of its data missing, so the stream behind it fills the rest. */
static size_t short_write(struct buf *pdu, size_t data_len, const struct buf *stream, size_t cut)
{
	size_t at;

	write_pdu(pdu, WRITE_MID, 1, data_len);
	data(pdu, 0x41, data_len - cut);
	at = pdu->len;
	CHECK(cut <= stream->len);
	memcpy(pdu->b + pdu->len, stream->b, cut);
	pdu->len += cut;
	return at;
}

/* Whatever a correct client writes is written. */
static void case_whole(void)
{
	struct buf pdu, stream = { .len = 0 };
	size_t resume;

	write_pdu(&pdu, WRITE_MID, 1, 8192);
	data(&pdu, 0x41, 8192);
	CHECK(check(&pdu, &resume) == TC_WRITE_WHOLE);

	/* Headers in the data that are not this client's next request: another
	 * session, a stale ID below the window, one past it, the WRITE's own
	 * ID, a response, a wrong structure size. */
	write_pdu(&pdu, WRITE_MID, 1, 0);
	data(&pdu, 0x41, 300);
	frame(&pdu, SMB2_OP_KEEPALIVE, WRITE_MID + 1, SESSION + 1, 4);
	data(&pdu, 0x41, 300);
	frame(&pdu, SMB2_OP_KEEPALIVE, LOW - 1, SESSION, 4);
	data(&pdu, 0x41, 300);
	frame(&pdu, SMB2_OP_KEEPALIVE, LOW + RANGE, SESSION, 4);
	data(&pdu, 0x41, 300);
	frame(&pdu, SMB2_OP_KEEPALIVE, WRITE_MID, SESSION, 4);
	data(&pdu, 0x41, 300);
	frame(&pdu, SMB2_OP_KEEPALIVE, WRITE_MID + 2, SESSION, 4);
	SIVAL(last_header(&pdu, 4), SMB2_HDR_FLAGS, SMB2_HDR_FLAG_REDIRECT);
	data(&pdu, 0x41, 300);
	frame(&pdu, SMB2_OP_KEEPALIVE, WRITE_MID + 3, SESSION, 4);
	SSVAL(last_header(&pdu, 4), SMB2_HDR_LENGTH, 65);
	data(&pdu, 0x41, 300);
	SIVAL(pdu.b + SMB2_HDR_BODY, 0x04, pdu.len - WRITE_DATA_OFS);
	CHECK(check(&pdu, &resume) == TC_WRITE_WHOLE);

	/* Not a WRITE, encrypted, or too small to hold a next request. */
	frame(&stream, SMB2_OP_KEEPALIVE, WRITE_MID + 1, SESSION, 4);
	short_write(&pdu, 4096, &stream, stream.len);
	SSVAL(pdu.b, SMB2_HDR_OPCODE, SMB2_OP_READ);
	CHECK(check(&pdu, &resume) == TC_WRITE_WHOLE);
	short_write(&pdu, 4096, &stream, stream.len);
	memcpy(pdu.b, "\xfdSMB", 4);
	CHECK(check(&pdu, &resume) == TC_WRITE_WHOLE);
	write_pdu(&pdu, WRITE_MID, 1, 8);
	CHECK(check(&pdu, &resume) == TC_WRITE_WHOLE);
}

/* The next request found where the cut is, whole or cut off. */
static void case_short(void)
{
	struct buf pdu, stream = { .len = 0 };
	size_t at, cut, resume;

	frame(&stream, SMB2_OP_CREATE, WRITE_MID + 1, SESSION, 120);
	at = short_write(&pdu, 8192, &stream, stream.len);
	CHECK(check(&pdu, &resume) == TC_WRITE_SHORT && resume == at);
	/* Every cut that leaves at least the next request's header. */
	for (cut = NBT_HDR_SIZE + SMB2_HDR_BODY; cut <= stream.len; cut++) {
		at = short_write(&pdu, 8192, &stream, cut);
		CHECK(check(&pdu, &resume) == TC_WRITE_SHORT && resume == at);
	}
	/* Less than a header of it: not found (written, as before 0072). */
	at = short_write(&pdu, 8192, &stream, NBT_HDR_SIZE + SMB2_HDR_BODY - 1);
	CHECK(check(&pdu, &resume) == TC_WRITE_WHOLE);
	/* The cut right at the start of the data. */
	at = short_write(&pdu, stream.len, &stream, stream.len);
	CHECK(at == WRITE_DATA_OFS && check(&pdu, &resume) == TC_WRITE_SHORT && resume == at);
}

/* Several requests behind the cut, the last cut off at every length, with
 * message IDs out of order (macOS queues internal requests at the head) and
 * a CANCEL repeating an old ID. */
static void case_several(void)
{
	struct buf pdu, stream = { .len = 0 };
	size_t at, cut, resume, second;

	frame(&stream, SMB2_OP_CLOSE, WRITE_MID + 3, SESSION, 24);
	frame(&stream, SMB2_OP_KEEPALIVE, WRITE_MID + 1, SESSION, 4);
	second = stream.len;
	frame(&stream, SMB2_OP_CANCEL, LOW - 50, SESSION, 4);
	frame(&stream, SMB2_OP_CREATE, WRITE_MID + 2, SESSION, 200);
	for (cut = second; cut <= stream.len; cut++) {
		at = short_write(&pdu, 8192, &stream, cut);
		CHECK(check(&pdu, &resume) == TC_WRITE_SHORT && resume == at);
	}
}

/* An NBT keepalive between requests is skipped like smbd skips it. */
static void case_keepalive(void)
{
	struct buf pdu, stream = { .len = 0 };
	size_t at, resume;

	frame(&stream, SMB2_OP_KEEPALIVE, WRITE_MID + 1, SESSION, 4);
	memcpy(stream.b + stream.len, "\x85\x00\x00\x00", 4);
	stream.len += 4;
	frame(&stream, SMB2_OP_KEEPALIVE, WRITE_MID + 2, SESSION, 4);
	at = short_write(&pdu, 4096, &stream, stream.len);
	CHECK(check(&pdu, &resume) == TC_WRITE_SHORT && resume == at);
}

/* A header that matches in the data before the real cut, but is not
 * followed by requests, is skipped; with no real cut after it, the WRITE is
 * still refused but where the next request starts is unknown. */
static void case_false_match(void)
{
	struct buf pdu, stream = { .len = 0 };
	size_t at, resume;

	frame(&stream, SMB2_OP_KEEPALIVE, WRITE_MID + 2, SESSION, 4);
	write_pdu(&pdu, WRITE_MID, 1, 0);
	data(&pdu, 0x41, 200);
	frame(&pdu, SMB2_OP_KEEPALIVE, WRITE_MID + 1, SESSION, 4);
	data(&pdu, 0x41, 200);
	at = pdu.len;
	memcpy(pdu.b + pdu.len, stream.b, stream.len);
	pdu.len += stream.len;
	SIVAL(pdu.b + SMB2_HDR_BODY, 0x04, pdu.len - WRITE_DATA_OFS + 1000);
	CHECK(check(&pdu, &resume) == TC_WRITE_SHORT && resume == at);

	/* The matching header is followed by plain data. */
	stream.len = 0;
	frame(&stream, SMB2_OP_KEEPALIVE, WRITE_MID + 1, SESSION, 4);
	data(&stream, 0x42, 300);
	short_write(&pdu, 8192, &stream, stream.len);
	CHECK(check(&pdu, &resume) == TC_WRITE_SHORT_LOST);
	/* Or by a header of another session. */
	stream.len = 0;
	frame(&stream, SMB2_OP_KEEPALIVE, WRITE_MID + 1, SESSION, 4);
	frame(&stream, SMB2_OP_KEEPALIVE, WRITE_MID + 2, SESSION + 1, 4);
	short_write(&pdu, 8192, &stream, stream.len);
	CHECK(check(&pdu, &resume) == TC_WRITE_SHORT_LOST);
}

/* A multi-credit WRITE owns the message IDs it charges for; the data may
 * start past the fixed body, and nothing before it can be a next request. */
static void case_ids_and_offset(void)
{
	struct buf pdu, stream = { .len = 0 };
	size_t resume;

	frame(&stream, SMB2_OP_KEEPALIVE, WRITE_MID + 3, SESSION, 4);
	write_pdu(&pdu, WRITE_MID, 4, 0);
	memcpy(pdu.b + pdu.len, stream.b, stream.len);
	pdu.len += stream.len;
	SIVAL(pdu.b + SMB2_HDR_BODY, 0x04, 8192);
	CHECK(check(&pdu, &resume) == TC_WRITE_WHOLE);
	SSVAL(pdu.b, SMB2_HDR_CREDIT_CHARGE, 3);
	CHECK(check(&pdu, &resume) == TC_WRITE_SHORT && resume == WRITE_DATA_OFS);

	/* A DataOffset past that header: the header is not in the data. */
	SSVAL(pdu.b + SMB2_HDR_BODY, 0x02, WRITE_DATA_OFS + 8);
	CHECK(check(&pdu, &resume) == TC_WRITE_WHOLE);
}

/* A last request cut off inside its header is checked as far as it goes:
 * its session ID too, once that is there. */
static void case_partial_tail(void)
{
	struct buf pdu, stream = { .len = 0 };
	size_t at, cut, resume;

	frame(&stream, SMB2_OP_KEEPALIVE, WRITE_MID + 1, SESSION, 4);
	frame(&stream, SMB2_OP_CREATE, WRITE_MID + 2, SESSION + 1, 120);
	for (cut = stream.len - 120 - SMB2_HDR_BODY; cut < stream.len - 120; cut++) {
		at = short_write(&pdu, 8192, &stream, cut);
		if (cut - (stream.len - 120 - SMB2_HDR_BODY) < SMB2_HDR_SESSION_ID + 8) {
			CHECK(check(&pdu, &resume) == TC_WRITE_SHORT && resume == at);
		} else {
			CHECK(check(&pdu, &resume) == TC_WRITE_SHORT_LOST);
		}
	}
	/* Its magic, once there, must be right too. */
	stream.len = 0;
	frame(&stream, SMB2_OP_KEEPALIVE, WRITE_MID + 1, SESSION, 4);
	frame(&stream, SMB2_OP_CREATE, WRITE_MID + 2, SESSION, 120);
	stream.b[stream.len - 120 - SMB2_HDR_BODY] = 0xfd;
	short_write(&pdu, 8192, &stream, stream.len - 120 - SMB2_HDR_BODY + 3);
	CHECK(check(&pdu, &resume) == TC_WRITE_SHORT);
	short_write(&pdu, 8192, &stream, stream.len - 120 - SMB2_HDR_BODY + 4);
	CHECK(check(&pdu, &resume) == TC_WRITE_SHORT_LOST);
}

/* A WRITE with a request chained after it is never taken for a short one,
 * whatever its data: the chained request carries the session's ID right after
 * the data, and data ending in what looks like an NBT header (a 0x00 byte,
 * then a length of at least 64) would otherwise make it pass for a cut. */
static void case_compound(void)
{
	struct buf pdu;
	size_t resume, close_at, i, len;
	uint32_t seed = 397;
	int trial;

	for (trial = 0; trial < 2000; trial++) {
		len = 8 * (1 + trial % 1000);
		write_pdu(&pdu, WRITE_MID, 1, len);
		for (i = 0; i < len; i++) {
			seed = seed * 1103515245 + 12345;
			pdu.b[pdu.len++] = seed >> 24;
		}
		if (trial % 2 == 0) {
			/* The last data bytes as an NBT header of 128 bytes. */
			memcpy(pdu.b + pdu.len - 4, "\x00\x00\x00\x80", 4);
		}
		close_at = pdu.len;
		SIVAL(pdu.b, SMB2_HDR_NEXT_COMMAND, close_at);
		header(pdu.b + close_at, SMB2_OP_CLOSE, WRITE_MID + 1, SESSION, 1);
		pdu.len += SMB2_HDR_BODY;
		data(&pdu, 0, 24);
		CHECK(check(&pdu, &resume) == TC_WRITE_WHOLE);
		same_arriving(&pdu);
	}
	/* The same WRITE alone, cut, is still found. */
	{
		struct buf stream = { .len = 0 };
		size_t at;

		frame(&stream, SMB2_OP_CLOSE, WRITE_MID + 1, SESSION, 24);
		at = short_write(&pdu, 4096, &stream, stream.len);
		CHECK(check(&pdu, &resume) == TC_WRITE_SHORT && resume == at);
	}
}

/* Matching headers that no requests follow are counted: past
 * TC_SCAN_MAX_MISSES the search stops and the WRITE is refused, so crafted
 * data cannot make the walks quadratic; below it a real cut after them is
 * still found. */
static void case_cap(void)
{
	struct buf pdu, stream = { .len = 0 };
	size_t at, resume;
	int misses, i;

	frame(&stream, SMB2_OP_CREATE, WRITE_MID + 2, SESSION, 120);
	for (misses = TC_SCAN_MAX_MISSES - 1; misses <= TC_SCAN_MAX_MISSES; misses++) {
		write_pdu(&pdu, WRITE_MID, 1, 0);
		for (i = 0; i < misses; i++) {
			data(&pdu, 0x41, 16);
			frame(&pdu, SMB2_OP_KEEPALIVE, WRITE_MID + 1, SESSION, 4);
			data(&pdu, 0x42, 16);
		}
		at = pdu.len;
		memcpy(pdu.b + pdu.len, stream.b, stream.len);
		pdu.len += stream.len;
		SIVAL(pdu.b + SMB2_HDR_BODY, 0x04, pdu.len - WRITE_DATA_OFS + 1000);
		if (misses < TC_SCAN_MAX_MISSES) {
			CHECK(check(&pdu, &resume) == TC_WRITE_SHORT && resume == at);
		} else {
			CHECK(check(&pdu, &resume) == TC_WRITE_SHORT_LOST);
		}
		/* Counted across reads: all but the last byte, then the rest. */
		{
			struct tc_scan scan = { .from = 0 };
			enum tc_short_write found;

			CHECK(tc_short_write_check(pdu.b, pdu.len - 1, false, LOW, RANGE, &scan,
						   &resume) == TC_WRITE_WHOLE);
			CHECK(scan.misses == (unsigned)misses);
			found = tc_short_write_check(pdu.b, pdu.len, true, LOW, RANGE, &scan, &resume);
			CHECK(found == (misses < TC_SCAN_MAX_MISSES ? TC_WRITE_SHORT : TC_WRITE_SHORT_LOST));
		}
	}
}

/* No request is half read when there is none, or when nothing of the next
 * one's NBT header has been read. */
static void case_read_idle(void)
{
	struct smbd_smb2_request_read_state state;
	uint8_t body[100];

	memset(&state, 0, sizeof(state));
	CHECK(tc_desync_read_idle(&state));
	state.req = (struct smbd_smb2_request *)&state;
	state._vector[0] = (struct iovec) { .iov_base = state.hdr.nbt, .iov_len = NBT_HDR_SIZE };
	state.vector = state._vector;
	state.count = 1;
	CHECK(tc_desync_read_idle(&state));
	/* Part of the NBT header read. */
	state._vector[0] = (struct iovec) { .iov_base = state.hdr.nbt + 1, .iov_len = NBT_HDR_SIZE - 1 };
	CHECK(!tc_desync_read_idle(&state));
	/* A body being read, even with exactly 4 bytes of it left. */
	state.pktlen = sizeof(body);
	state._vector[0] = (struct iovec) { .iov_base = body + 96, .iov_len = NBT_HDR_SIZE };
	CHECK(!tc_desync_read_idle(&state));
	/* Nothing left to read in the vector. */
	state.pktlen = 0;
	state.count = 0;
	CHECK(!tc_desync_read_idle(&state));
}

/* The connection ends only once nothing is left to read or answer and nothing
 * has been read or written for a moment, or at the deadline. */
static void case_end_now(void)
{
	const double idle = TC_DESYNC_IDLE_MS / 1000.0, late = TC_DESYNC_CLOSE_SECS;
	bool capped = true;

	CHECK(tc_desync_end_now(0, 0, false, false, true, idle, 1, &capped) && !capped);
	/* Each condition alone holds it. */
	CHECK(!tc_desync_end_now(1, 0, false, false, true, idle, 1, &capped) && !capped);
	CHECK(!tc_desync_end_now(0, 1, false, false, true, idle, 1, &capped));
	CHECK(!tc_desync_end_now(0, 0, true, false, true, idle, 1, &capped));
	CHECK(!tc_desync_end_now(0, 0, false, true, true, idle, 1, &capped));
	CHECK(!tc_desync_end_now(0, 0, false, false, false, idle, 1, &capped));
	/* The client's answer to a reply that just went out is still on its way. */
	CHECK(!tc_desync_end_now(0, 0, false, false, true, idle - 0.001, 1, &capped) && !capped);
	CHECK(!tc_desync_end_now(0, 0, false, false, true, 0, 1, &capped));
	CHECK(TC_DESYNC_IDLE_MS > TC_DESYNC_TICK_MS);
	/* At the deadline, whatever is still pending. */
	CHECK(!tc_desync_end_now(3, 100, true, true, false, 0, late - 0.001, &capped) && !capped);
	CHECK(tc_desync_end_now(3, 100, true, true, false, 0, late, &capped) && capped);
	CHECK(tc_desync_end_now(0, 0, false, false, true, 0, late, &capped) && capped);
	CHECK(tc_desync_end_now(0, 0, false, false, true, idle, late, &capped) && !capped);
	/* macOS fails the WRITE 120 s after it began sending it: the 5 s send
	 * timeout that cut it, the cap, and the 10 s before a WRITE that never
	 * completes shows the client's ECHO must leave 40 s for a stall before
	 * smbd read it. */
	CHECK(5 + TC_DESYNC_CLOSE_SECS + 10 + 40 <= 120);
}

/* smbd checks a WRITE on each read while it arrives, searching each byte once:
 * whatever the chunks, a real cut is found where the whole PDU shows it, and a
 * correct WRITE is never taken for one. A cut WRITE that never completes is
 * found once a request after the cut has wholly arrived, not before. */
static void case_arriving(void)
{
	struct buf pdu, stream = { .len = 0 };
	size_t at, cut, resume, seen, second;

	/* Real cuts: one request, several with a keepalive, the last cut off. */
	frame(&stream, SMB2_OP_CREATE, WRITE_MID + 1, SESSION, 120);
	for (cut = NBT_HDR_SIZE + SMB2_HDR_BODY; cut <= stream.len; cut += 7) {
		short_write(&pdu, 8192, &stream, cut);
		same_arriving(&pdu);
	}
	stream.len = 0;
	frame(&stream, SMB2_OP_CLOSE, WRITE_MID + 3, SESSION, 24);
	stream.b[stream.len++] = 0x85;
	stream.b[stream.len++] = 0;
	stream.b[stream.len++] = 0;
	stream.b[stream.len++] = 0;
	frame(&stream, SMB2_OP_CANCEL, LOW - 50, SESSION, 4);
	frame(&stream, SMB2_OP_CREATE, WRITE_MID + 2, SESSION, 200);
	for (cut = NBT_HDR_SIZE + SMB2_HDR_BODY; cut <= stream.len; cut += 11) {
		short_write(&pdu, 8192, &stream, cut);
		same_arriving(&pdu);
	}

	/* Correct data with headers that are not this client's next request. */
	write_pdu(&pdu, WRITE_MID, 1, 0);
	data(&pdu, 0x41, 300);
	frame(&pdu, SMB2_OP_KEEPALIVE, WRITE_MID + 1, SESSION + 1, 4);
	data(&pdu, 0x41, 300);
	frame(&pdu, SMB2_OP_KEEPALIVE, LOW + RANGE, SESSION, 4);
	data(&pdu, 0x41, 300);
	frame(&pdu, SMB2_OP_KEEPALIVE, WRITE_MID, SESSION, 4);
	data(&pdu, 0x41, 300);
	SIVAL(pdu.b + SMB2_HDR_BODY, 0x04, pdu.len - WRITE_DATA_OFS);
	CHECK(check(&pdu, &resume) == TC_WRITE_WHOLE);
	same_arriving(&pdu);

	/* A header that matches, then plain data, and the real cut later: the
	 * whole PDU skips the first header and finds the cut. */
	stream.len = 0;
	frame(&stream, SMB2_OP_KEEPALIVE, WRITE_MID + 2, SESSION, 4);
	write_pdu(&pdu, WRITE_MID, 1, 0);
	data(&pdu, 0x41, 200);
	frame(&pdu, SMB2_OP_KEEPALIVE, WRITE_MID + 1, SESSION, 4);
	second = pdu.len;
	data(&pdu, 0x41, 200);
	at = pdu.len;
	memcpy(pdu.b + pdu.len, stream.b, stream.len);
	pdu.len += stream.len;
	SIVAL(pdu.b + SMB2_HDR_BODY, 0x04, pdu.len - WRITE_DATA_OFS + 1000);
	CHECK(check(&pdu, &resume) == TC_WRITE_SHORT && resume == at);
	CHECK(check_arriving(&pdu, 7, true, &resume, &seen) == TC_WRITE_SHORT && resume == at);
	/* The price of acting before the WRITE completes: a read that ends
	 * exactly where that header's frame ends takes it for the next request.
	 * It needs this session's ID and a message ID in the window inside the
	 * data; it costs one reconnect. */
	{
		struct buf part = pdu;

		part.len = second;
		CHECK(check_arriving(&part, second, false, &resume, &seen) == TC_WRITE_SHORT);
		CHECK(resume == second - NBT_HDR_SIZE - SMB2_HDR_BODY - 4);
	}

	/* A cut WRITE that never completes: the client sends a CREATE and an
	 * ECHO, fewer bytes than the WRITE is missing, and waits. */
	stream.len = 0;
	frame(&stream, SMB2_OP_CREATE, WRITE_MID + 1, SESSION, 120);
	second = stream.len;
	frame(&stream, SMB2_OP_KEEPALIVE, WRITE_MID + 2, SESSION, 4);
	write_pdu(&pdu, WRITE_MID, 1, 65536);
	data(&pdu, 0x41, 4096);
	at = pdu.len;
	memcpy(pdu.b + pdu.len, stream.b, stream.len);
	pdu.len += stream.len;
	CHECK(pdu.len < WRITE_DATA_OFS + 65536);
	/* Byte by byte: nothing until all of the CREATE is here. */
	CHECK(check_arriving(&pdu, 1, false, &resume, &seen) == TC_WRITE_SHORT);
	CHECK(resume == at && seen == at + second);
	/* Not even the WRITE's fixed part yet. */
	pdu.len = SMB2_HDR_BODY + 8;
	CHECK(check_arriving(&pdu, 1, false, &resume, &seen) == TC_WRITE_WHOLE);
}

int main(int argc, const char **argv)
{
	static const struct {
		const char *name;
		void (*fn)(void);
	} cases[] = {
		{ "whole", case_whole },
		{ "short", case_short },
		{ "several", case_several },
		{ "keepalive", case_keepalive },
		{ "false_match", case_false_match },
		{ "ids_and_offset", case_ids_and_offset },
		{ "partial_tail", case_partial_tail },
		{ "end_now", case_end_now },
		{ "arriving", case_arriving },
		{ "compound", case_compound },
		{ "cap", case_cap },
		{ "read_idle", case_read_idle },
	};
	size_t i;

	CHECK(argc == 2);
	for (i = 0; i < ARRAY_SIZE(cases); i++) {
		if (strcmp(argv[1], cases[i].name) == 0) {
			cases[i].fn();
			printf("ok %s\n", argv[1]);
			return 0;
		}
	}
	fprintf(stderr, "unknown case %s\n", argv[1]);
	return 2;
}

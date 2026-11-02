/* Host test for Samba patch 0043's interface list parser
 * (build/patches/samba4x/overlay/lib/socket/tc_netbsd_iflist.c), fed the
 * NET_RT_IFLIST dumps captured on both devices (tests/native/fixtures/iflist).
 * The Samba helpers it calls are shims here: talloc maps to malloc, and
 * make_netmask() and make_bcast() build the masks interfaces.c would.
 * samba_iflist_shim/ stands in for the NetBSD headers it includes.
 *
 * Prints one line per interface: name, index, family, address, and the prefix
 * its netmask holds. */
#include <arpa/inet.h>
#include <errno.h>
#include <net/if.h>
#include <netinet/in.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>

typedef void TALLOC_CTX;
#define talloc_zero_array(ctx, type, n) ((type *)calloc((n), sizeof(type)))
#define talloc_array(ctx, type, n) ((type *)malloc((n) * sizeof(type)))
#define TALLOC_FREE(p) do { free(p); (p) = NULL; } while (0)
#define ZERO_STRUCT(x) memset(&(x), 0, sizeof(x))
#define ZERO_STRUCTP(x) do { if ((x) != NULL) memset((x), 0, sizeof(*(x))); } while (0)
#ifndef MIN
#define MIN(a, b) ((a) < (b) ? (a) : (b))
#endif
#define FSCTL_NET_IFACE_NONE_CAPABLE 0
#ifdef __linux__
/* Linux sockaddrs have no length field; the parser sets it, the test never reads it. */
#define sin_len sin_zero[7]
#define sin6_len sin6_scope_id
#endif

struct iface_struct {
	char name[16];
	int if_index;
	uint64_t linkspeed;
	uint32_t capability;
	unsigned int flags;
	struct sockaddr_storage ip;
	struct sockaddr_storage netmask;
	struct sockaddr_storage bcast;
};

static bool make_netmask(struct sockaddr_storage *out, const struct sockaddr_storage *in,
			 unsigned long prefix)
{
	uint8_t *bytes;
	size_t length, i;

	memset(out, 0, sizeof(*out));
	out->ss_family = in->ss_family;
	if (in->ss_family == AF_INET) {
		bytes = (uint8_t *)&((struct sockaddr_in *)out)->sin_addr;
		length = 4;
	} else {
		bytes = (uint8_t *)&((struct sockaddr_in6 *)out)->sin6_addr;
		length = 16;
	}
	if (prefix > length * 8) {
		return false;
	}
	for (i = 0; i < prefix; i++) {
		bytes[i / 8] |= (uint8_t)(0x80 >> (i % 8));
	}
	return true;
}

static void make_bcast(struct sockaddr_storage *out, const struct sockaddr_storage *ip,
		       const struct sockaddr_storage *mask)
{
	*out = *ip;
	(void)mask;
}

#include "tc_netbsd_iflist.c"

static unsigned int prefix_of(const struct sockaddr_storage *mask)
{
	const uint8_t *bytes = mask->ss_family == AF_INET ?
		(const uint8_t *)&((const struct sockaddr_in *)mask)->sin_addr :
		(const uint8_t *)&((const struct sockaddr_in6 *)mask)->sin6_addr;
	size_t length = mask->ss_family == AF_INET ? 4 : 16, i;
	unsigned int prefix = 0;

	for (i = 0; i < length * 8; i++) {
		if (bytes[i / 8] & (0x80 >> (i % 8))) {
			prefix++;
		}
	}
	return prefix;
}

int main(int argc, char **argv)
{
	struct iface_struct *ifaces = NULL;
	static uint8_t buf[65536];
	size_t length;
	FILE *f;
	int n, i;

	if (argc != 2 || (f = fopen(argv[1], "rb")) == NULL) {
		return 2;
	}
	length = fread(buf, 1, sizeof(buf), f);
	fclose(f);
	n = tc_netbsd_parse_iflist(NULL, buf, length, &ifaces);
	printf("count %d\n", n);
	for (i = 0; i < n; i++) {
		char text[INET6_ADDRSTRLEN];
		const struct sockaddr_storage *ip = &ifaces[i].ip;
		const void *addr = ip->ss_family == AF_INET ?
			(const void *)&((const struct sockaddr_in *)ip)->sin_addr :
			(const void *)&((const struct sockaddr_in6 *)ip)->sin6_addr;

		inet_ntop(ip->ss_family, addr, text, sizeof(text));
		printf("iface %s %d %s %s %u\n", ifaces[i].name, ifaces[i].if_index,
		       ip->ss_family == AF_INET ? "inet" : "inet6", text, prefix_of(&ifaces[i].netmask));
	}
	free(ifaces);
	return 0;
}

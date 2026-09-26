/* Included by interfaces.c (patch 0043) on NetBSD. */

#include <sys/sysctl.h>
#include <net/route.h>

/*
 * AirPort Time Capsule kernels and the SDK used to build Samba disagree on
 * the NetBSD routing-message ABI.  In particular, the NetBSD 4 appliance
 * emits a 152-byte if_msghdr while the SDK describes 144 bytes.  libc
 * getifaddrs() consequently reads sockaddr_dl from inside if_data, while
 * Samba's SIOCGIFCONF replacement assumes fixed-size struct ifreq entries
 * even though that kernel returns the old variable-length form.
 *
 * Parse NET_RT_IFLIST by its on-wire RTM_VERSION instead.  This is the same
 * boundary used by the appliance service's native interface collector: it
 * never interprets if_data, and it obtains names/indexes directly from the
 * matching sockaddr_dl rather than calling the affected libc helpers.
 */
#define TC_NETBSD_RTM_NEWADDR 0xc
#define TC_NETBSD_AF_LINK 18
#define TC_NETBSD_AF_INET 2
#define TC_NETBSD_AF_INET6 24
#define TC_NETBSD_RTA_NETMASK 2
#define TC_NETBSD_RTA_IFA 5

struct tc_netbsd_iflist_layout {
	unsigned int ifinfo_type;
	size_t ifa_header;
	size_t ifam_index_offset;
	size_t roundup;
};

struct tc_netbsd_link {
	char name[16];
	unsigned int index;
	int flags;
};

static unsigned int tc_netbsd_read_u16(const uint8_t *p)
{
	uint16_t value;
	memcpy(&value, p, sizeof(value));
	return value;
}

static unsigned int tc_netbsd_read_u32(const uint8_t *p)
{
	uint32_t value;
	memcpy(&value, p, sizeof(value));
	return value;
}

static bool tc_netbsd_iflist_layout(unsigned int version,
				    struct tc_netbsd_iflist_layout *layout)
{
	if (version == 3) {
		layout->ifinfo_type = 0xf;
		layout->ifa_header = 20;
		layout->ifam_index_offset = 12;
		layout->roundup = 4;
		return true;
	}
	if (version == 4) {
		layout->ifinfo_type = 0x14;
		layout->ifa_header = 24;
		layout->ifam_index_offset = 16;
		layout->roundup = 8;
		return true;
	}
	return false;
}

static unsigned int tc_netbsd_prefix_from_mask(const uint8_t *mask,
						size_t length)
{
	unsigned int prefix = 0;
	size_t i;

	for (i = 0; i < length; i++) {
		int bit;
		for (bit = 7; bit >= 0; bit--) {
			if ((mask[i] >> bit) & 1) {
				prefix++;
			} else {
				return prefix;
			}
		}
	}
	return prefix;
}

static struct tc_netbsd_link *tc_netbsd_find_link(
	struct tc_netbsd_link *links,
	size_t count,
	unsigned int index)
{
	size_t i;
	for (i = 0; i < count; i++) {
		if (links[i].index == index) {
			return &links[i];
		}
	}
	return NULL;
}

static void tc_netbsd_parse_ifinfo(const uint8_t *msg,
				   size_t msglen,
				   struct tc_netbsd_link *links,
				   size_t *count,
				   size_t capacity)
{
	struct tc_netbsd_link *link;
	unsigned int index = tc_netbsd_read_u16(msg + 12);
	size_t p;

	link = tc_netbsd_find_link(links, *count, index);
	if (link == NULL) {
		if (*count >= capacity) {
			return;
		}
		link = &links[(*count)++];
		ZERO_STRUCTP(link);
		link->index = index;
	}
	link->flags = (int)tc_netbsd_read_u32(msg + 8);

	/* sockaddr_dl follows an ABI-sized if_data. Scan for the entry whose
	 * embedded index matches this RTM_IFINFO instead of assuming the SDK's
	 * if_msghdr size. */
	for (p = 16; p + 8 <= msglen; p++) {
		size_t sdl_len = msg[p];
		size_t nlen;
		size_t alen;
		if (msg[p + 1] != TC_NETBSD_AF_LINK ||
		    tc_netbsd_read_u16(msg + p + 2) != index) {
			continue;
		}
		if (sdl_len < 8 || sdl_len > msglen - p) {
			continue;
		}
		nlen = msg[p + 5];
		alen = msg[p + 6];
		if (8 + nlen + alen > sdl_len) {
			continue;
		}
		if (nlen >= sizeof(link->name)) {
			nlen = sizeof(link->name) - 1;
		}
		memcpy(link->name, msg + p + 8, nlen);
		link->name[nlen] = '\0';
		return;
	}
}

static int tc_netbsd_parse_newaddr(const uint8_t *msg,
				    size_t msglen,
				    const struct tc_netbsd_iflist_layout *layout,
				    struct tc_netbsd_link *links,
				    size_t link_count,
				    struct iface_struct *ifaces,
				    size_t *count,
				    size_t capacity)
{
	unsigned int index = tc_netbsd_read_u16(msg + layout->ifam_index_offset);
	unsigned int rta = tc_netbsd_read_u32(msg + 4);
	struct tc_netbsd_link *link = tc_netbsd_find_link(links, link_count, index);
	struct sockaddr_storage address;
	unsigned int prefix = 0;
	size_t p = layout->ifa_header;
	bool have_address = false;
	bool have_mask = false;
	int bit;

	if (link == NULL || link->name[0] == '\0' || !(link->flags & IFF_UP)) {
		return 0;
	}
	ZERO_STRUCT(address);

	for (bit = 0; bit < 8 && p + 2 <= msglen; bit++) {
		size_t sa_len;
		size_t family;
		size_t consumed;
		if (!(rta & (1u << bit))) {
			continue;
		}
		sa_len = msg[p];
		family = msg[p + 1];
		consumed = sa_len == 0 ? layout->roundup :
			((sa_len + layout->roundup - 1) / layout->roundup) *
			 layout->roundup;
		if (consumed < layout->roundup) {
			consumed = layout->roundup;
		}
		if (sa_len > msglen - p) {
			return -1;
		}
		if (bit == TC_NETBSD_RTA_NETMASK) {
			if (family == TC_NETBSD_AF_INET6 && sa_len >= 24) {
				prefix = tc_netbsd_prefix_from_mask(msg + p + 8, 16);
			} else if (sa_len > 4 && sa_len <= 8) {
				prefix = tc_netbsd_prefix_from_mask(msg + p + 4,
							     sa_len - 4);
			} else if (sa_len > 8) {
				size_t mask_len = sa_len - 8;
				if (mask_len > 16) {
					mask_len = 16;
				}
				prefix = tc_netbsd_prefix_from_mask(msg + p + 8,
							     mask_len);
			} else {
				prefix = 0;
			}
			have_mask = true;
		} else if (bit == TC_NETBSD_RTA_IFA) {
			if (family == TC_NETBSD_AF_INET && sa_len >= 8) {
				struct sockaddr_in *sin =
					(struct sockaddr_in *)&address;
				ZERO_STRUCTP(sin);
				sin->sin_len = sizeof(*sin);
				sin->sin_family = AF_INET;
				memcpy(&sin->sin_addr, msg + p + 4, 4);
				have_address = true;
			} else if (family == TC_NETBSD_AF_INET6 && sa_len >= 24) {
				struct sockaddr_in6 *sin6 =
					(struct sockaddr_in6 *)&address;
				ZERO_STRUCTP(sin6);
				sin6->sin6_len = sizeof(*sin6);
				sin6->sin6_family = AF_INET6;
				memcpy(&sin6->sin6_addr, msg + p + 8, 16);
				have_address = true;
			}
		}
		p += consumed;
	}

	if (!have_address || *count >= capacity) {
		return 0;
	}
	if (address.ss_family == AF_INET6) {
		struct sockaddr_in6 *sin6 = (struct sockaddr_in6 *)&address;
		if (IN6_IS_ADDR_LINKLOCAL(&sin6->sin6_addr) ||
		    IN6_IS_ADDR_V4COMPAT(&sin6->sin6_addr)) {
			/* Preserve Samba's existing getifaddrs policy. Wildcard SMB
			 * listeners still accept connections to link-local addresses. */
			return 0;
		}
	}

	ZERO_STRUCT(ifaces[*count]);
	ifaces[*count].flags = link->flags;
	ifaces[*count].ip = address;
	if (!make_netmask(&ifaces[*count].netmask,
			  &ifaces[*count].ip,
			  have_mask ? prefix :
			  (address.ss_family == AF_INET ? 32 : 128))) {
		return -1;
	}
	if (address.ss_family == AF_INET6) {
		ZERO_STRUCT(ifaces[*count].bcast);
	} else if (link->flags & (IFF_BROADCAST | IFF_LOOPBACK)) {
		make_bcast(&ifaces[*count].bcast,
			   &ifaces[*count].ip,
			   &ifaces[*count].netmask);
	} else {
		return 0;
	}
	if (strlcpy(ifaces[*count].name,
		    link->name,
		    sizeof(ifaces[*count].name)) >= sizeof(ifaces[*count].name)) {
		return 0;
	}
	ifaces[*count].if_index = link->index;
	ifaces[*count].linkspeed = 1000 * 1000 * 1000;
	ifaces[*count].capability = FSCTL_NET_IFACE_NONE_CAPABLE;
	(*count)++;
	return 0;
}

static int tc_netbsd_parse_iflist(TALLOC_CTX *mem_ctx,
				  const uint8_t *buf,
				  size_t length,
				  struct iface_struct **pifaces)
{
	struct tc_netbsd_link *links = NULL;
	struct iface_struct *ifaces = NULL;
	size_t link_capacity = 0;
	size_t address_capacity = 0;
	size_t link_count = 0;
	size_t iface_count = 0;
	size_t p;

	*pifaces = NULL;
	for (p = 0; p < length;) {
		struct tc_netbsd_iflist_layout layout;
		size_t msglen;
		unsigned int version;
		unsigned int type;
		if (length - p < 4) {
			return -1;
		}
		msglen = tc_netbsd_read_u16(buf + p);
		version = buf[p + 2];
		type = buf[p + 3];
		if (msglen < 4 || msglen > length - p ||
		    !tc_netbsd_iflist_layout(version, &layout)) {
			return -1;
		}
		if (type == layout.ifinfo_type) {
			link_capacity++;
		} else if (type == TC_NETBSD_RTM_NEWADDR) {
			address_capacity++;
		}
		p += msglen;
	}
	if (link_capacity == 0 || address_capacity == 0) {
		return 0;
	}
	links = talloc_zero_array(mem_ctx, struct tc_netbsd_link, link_capacity);
	ifaces = talloc_zero_array(mem_ctx, struct iface_struct, address_capacity);
	if (links == NULL || ifaces == NULL) {
		TALLOC_FREE(links);
		TALLOC_FREE(ifaces);
		errno = ENOMEM;
		return -1;
	}

	for (p = 0; p < length;) {
		struct tc_netbsd_iflist_layout layout;
		size_t msglen = tc_netbsd_read_u16(buf + p);
		unsigned int version = buf[p + 2];
		unsigned int type = buf[p + 3];
		if (!tc_netbsd_iflist_layout(version, &layout)) {
			TALLOC_FREE(links);
			TALLOC_FREE(ifaces);
			return -1;
		}
		if (type == layout.ifinfo_type) {
			if (msglen < 16) {
				TALLOC_FREE(links);
				TALLOC_FREE(ifaces);
				return -1;
			}
			tc_netbsd_parse_ifinfo(buf + p,
						msglen,
						links,
						&link_count,
						link_capacity);
		}
		p += msglen;
	}

	for (p = 0; p < length;) {
		struct tc_netbsd_iflist_layout layout;
		size_t msglen = tc_netbsd_read_u16(buf + p);
		unsigned int version = buf[p + 2];
		unsigned int type = buf[p + 3];
		if (!tc_netbsd_iflist_layout(version, &layout)) {
			TALLOC_FREE(links);
			TALLOC_FREE(ifaces);
			return -1;
		}
		if (type == TC_NETBSD_RTM_NEWADDR) {
			if (msglen < layout.ifa_header ||
			    tc_netbsd_parse_newaddr(buf + p,
						     msglen,
						     &layout,
						     links,
						     link_count,
						     ifaces,
						     &iface_count,
						     address_capacity) != 0) {
				TALLOC_FREE(links);
				TALLOC_FREE(ifaces);
				return -1;
			}
		}
		p += msglen;
	}
	TALLOC_FREE(links);
	if (iface_count == 0) {
		TALLOC_FREE(ifaces);
		return 0;
	}
	*pifaces = ifaces;
	return (int)iface_count;
}

static int tc_netbsd_get_interfaces(TALLOC_CTX *mem_ctx,
				     struct iface_struct **pifaces)
{
	int mib[6] = { CTL_NET, PF_ROUTE, 0, 0, NET_RT_IFLIST, 0 };
	int attempt;

	*pifaces = NULL;
	for (attempt = 0; attempt < 4; attempt++) {
		uint8_t *buf;
		size_t length = 0;
		int result;
		if (sysctl(mib, 6, NULL, &length, NULL, 0) != 0) {
			return -1;
		}
		length += length / 4 + 256;
		buf = talloc_array(mem_ctx, uint8_t, length);
		if (buf == NULL) {
			errno = ENOMEM;
			return -1;
		}
		if (sysctl(mib, 6, buf, &length, NULL, 0) == 0) {
			result = tc_netbsd_parse_iflist(mem_ctx,
							 buf,
							 length,
							 pifaces);
			TALLOC_FREE(buf);
			return result;
		}
		TALLOC_FREE(buf);
		if (errno != ENOMEM) {
			return -1;
		}
	}
	return -1;
}

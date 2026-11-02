/* Stands in for NetBSD's <net/route.h> in tests/native/unit/test_samba_iflist.c:
 * the parser reads the routing messages by their on-wire layout, not this header. */
#ifndef PF_ROUTE
#define PF_ROUTE 17
#endif

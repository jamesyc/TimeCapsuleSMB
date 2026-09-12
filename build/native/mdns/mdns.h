#ifndef TC_MDNS_H
#define TC_MDNS_H
#include "config.h"
#include "records.h"
#include "printer.h"
#include "dns_wire.h"
#include "transport.h"
#include "responder.h"
#include "announce.h"
#include "runtime.h"
#include "diagnostics.h"
#define fprintf timestamped_fprintf
#define perror timestamped_perror
#endif

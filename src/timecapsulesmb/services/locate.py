"""Finding an AirPort again after it moved to another address.

AirPorts on a home network get their address from DHCP, which can hand out a
new one when the device restarts. Since v3.0, 51 of 259 installs whose reboot
wait timed out later reached their device at a new address on the same
network, as did 10 of 23 that found nothing at the saved address. AirPort
Utility finds base stations the same way this does: it browses _airport._tcp
and knows each one by its AirPort MAC (waMA), which the device advertises in
that record and returns over network ACP.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal

from timecapsulesmb.core.net import (
    endpoint_host,
    ipv4_literal,
    ipv6_literal,
    is_link_local_ipv6,
    normalize_endpoint_host,
    same_scoped_ip,
)
from timecapsulesmb.device import probe
from timecapsulesmb.discovery.bonjour import AIRPORT_SERVICE, BonjourQuery
from timecapsulesmb.discovery.devices import device_candidates_from_records
from timecapsulesmb.discovery.models import normalize_airport_mac
from timecapsulesmb.services.acp_diagnostics import address_summary
from timecapsulesmb.services.callbacks import OperationCallbacks


# A browse always runs its full timeout; 2 s found both LAN devices every time
# on 2026-10-08.
LOCATE_BROWSE_SECONDS = 2.0

LocateOutcome = Literal["found", "not_found", "not_moved", "password_rejected"]


@dataclass(frozen=True)
class LocateResult:
    """Where the AirPort is now.

    `found`: it answered at `host` with the password. `not_moved`: its record
    still lists the current address, which is only slow (ACPd busy, or still
    booting). `password_rejected`: it answered at `address` but refused the
    password, as a reset device does.
    """

    outcome: LocateOutcome
    host: str | None = None
    address: str | None = None

    @property
    def rejected_note(self) -> str:
        # Only the record's MAC says it is this device: ACPd refuses the read
        # that would prove it, and the record may be a stale cache entry for an
        # address another AirPort has taken since. So this is said, not acted on.
        return (
            f"An AirPort advertising this device's MAC answered at {self.address} "
            "but rejected the AirPort admin password; it may have been reset."
        )


def locate_airport(
    airport_mac: str,
    password: str,
    *,
    current_host: str,
    trigger: str,
    callbacks: OperationCallbacks | None = None,
    browse: Callable[..., tuple[object, object]] | None = None,
    attempts: int = 2,
) -> LocateResult:
    """Find the AirPort with this MAC at an address other than `current_host`.

    `attempts` reads per candidate address: a device that is up can fail one
    network ACP read and answer the next. The reboot wait reads once, as a
    device still booting fails every read and is looked for again soon.

    A Bonjour address alone is never trusted, as the cache can hold an address
    another device has since taken: an address counts only when its network
    ACP read accepts the password and returns this MAC.
    """
    callbacks = callbacks or OperationCallbacks()
    mac = normalize_airport_mac(airport_mac)
    current = endpoint_host(current_host)
    if not _is_ip_literal(current):
        # A saved hostname resolves to wherever the device is now, so there is
        # no address to compare: it is taken as current. Resolving it to
        # compare would not do, as it can resolve to an IPv6 address the
        # record does not list.
        callbacks.measurement("host_follow", trigger=trigger, result="not_moved", saved_hostname=True)
        return LocateResult("not_moved")
    started = time.monotonic()
    snapshot, _diagnostics = (browse or BonjourQuery().browse)(AIRPORT_SERVICE, timeout=LOCATE_BROWSE_SECONDS)
    candidates = [c for c in device_candidates_from_records(snapshot.resolved) if mac and c.airport_mac == mac]
    browse_sec = time.monotonic() - started
    result = _first_answer(candidates, mac, password, current, attempts)
    fields: dict[str, object] = {
        "trigger": trigger,
        "result": result.outcome,
        "candidates": len(candidates),
        "browse_sec": round(browse_sec, 3),
        "from_scope": address_summary(current).get("scope"),
    }
    if result.address is not None:
        fields["to_scope"] = address_summary(result.address).get("scope")
    callbacks.measurement("host_follow", **fields)
    return result


def _is_ip_literal(host: str) -> bool:
    address = host.partition("%")[0]
    return bool(ipv4_literal(address) or ipv6_literal(address))


def _first_answer(candidates, mac: str | None, password: str, current: str, attempts: int) -> LocateResult:
    rejected: str | None = None
    lists_current = False
    for candidate in candidates:
        # A record that still lists the current address may be the device,
        # only slow (ACPd busy, or still booting), or a cache that kept the old
        # address beside the new one. Its other LAN addresses are tried; its
        # link-local ones are the same device where it was, so they are not.
        here = any(same_scoped_ip(address, current) for address in candidate.addresses)
        lists_current = lists_current or here
        record = replace(candidate.selected_record, ipv4=list(candidate.ipv4), ipv6=list(candidate.ipv6))
        for address in record.acp_addresses():
            if same_scoped_ip(address, current) or (here and is_link_local_ipv6(address)):
                continue
            reading = probe.read_airport_acp(address, password, attempts=attempts)
            if reading.password_matches is False:
                rejected = rejected or address
            elif reading.password_matches and reading.airport_mac == mac:
                return LocateResult("found", host=f"root@{normalize_endpoint_host(address)}", address=address)
    if lists_current:
        return LocateResult("not_moved")
    if rejected is not None:
        return LocateResult("password_rejected", address=rejected)
    return LocateResult("not_found")

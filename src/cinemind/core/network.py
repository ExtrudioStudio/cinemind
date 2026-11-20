"""The VPN kill switch: BitTorrent runs only while the interface it is bound to is up.

``outgoing_interfaces`` names the VPN adapter (``NordLynx``, ``wg0``, ``tun0``) or its address,
comma-separated like libtorrent's setting of the same name. Binding alone isn't enough: when
the VPN drops, its adapter can vanish, and peers, trackers and the DHT would see the real
address once it comes back under another route. So the torrent engine asks :func:`vpn_down`
every few seconds and pauses everything while it says why (see ``vpn_kill_switch``).
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from dataclasses import dataclass

from cinemind.core.config import Config

_IP_FAMILIES = (socket.AF_INET, socket.AF_INET6)


@dataclass(frozen=True)
class Interface:
    name: str
    up: bool
    addresses: tuple[str, ...]


def interfaces() -> list[Interface]:
    """The machine's network interfaces, with their IP addresses and whether each is up."""
    import psutil

    stats = psutil.net_if_stats()
    out = []
    for name, addrs in psutil.net_if_addrs().items():
        ips = tuple(a.address.split("%")[0] for a in addrs if a.family in _IP_FAMILIES)
        out.append(Interface(name, bool(stats.get(name) and stats[name].isup), ips))
    return out


def _is_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return False
    return True


def vpn_down(spec: str, found: list[Interface] | None = None) -> str:
    """Why none of the interfaces in ``spec`` can carry traffic, or ``""`` when one can.

    An entry is an interface name (case-insensitive) or an IP address on an interface. It counts
    only when that interface is up and has an IP address.
    """
    entries = [e.strip() for e in spec.split(",") if e.strip()]
    if not entries:
        return ""
    found = interfaces() if found is None else found
    for entry in entries:
        for iface in found:
            if not (iface.up and iface.addresses):
                continue
            if iface.name.lower() == entry.lower() or (_is_ip(entry) and entry in iface.addresses):
                return ""
    listed = ", ".join(entries)
    return f"the VPN interface ({listed}) is down or missing"


Guard = Callable[[], str]


def vpn_guard(cfg: Config) -> Guard | None:
    """The kill switch's check for this config, or None when it doesn't apply."""
    if not cfg.outgoing_interfaces.strip() or not cfg.vpn_kill_switch:
        return None
    spec = cfg.outgoing_interfaces
    return lambda: vpn_down(spec)

"""The VPN kill switch's interface check (no network needed)."""

from __future__ import annotations

from cinemind.core.config import Config
from cinemind.core.network import Interface, interfaces, vpn_down, vpn_guard

UP = [
    Interface("Wi-Fi", True, ("192.168.1.20",)),
    Interface("NordLynx", True, ("10.5.0.2",)),
    Interface("wg-off", True, ()),  # up, but no address: carries nothing
    Interface("tun0", False, ("10.8.0.6",)),  # has its address, but down
]


def test_an_interface_counts_when_it_is_up_with_an_address():
    assert vpn_down("NordLynx", UP) == ""
    assert vpn_down("nordlynx", UP) == ""  # names are case-insensitive, as on Windows
    assert vpn_down("10.5.0.2", UP) == ""  # or its address
    assert vpn_down("tun0, NordLynx", UP) == ""  # any one of them


def test_a_missing_down_or_addressless_interface_holds_the_switch():
    for spec in ("ProtonVPN", "tun0", "wg-off", "10.8.0.6", "10.9.9.9"):
        assert "down or missing" in vpn_down(spec, UP), spec
    assert "ProtonVPN" in vpn_down("ProtonVPN", [])


def test_the_guard_applies_only_with_an_interface_and_the_switch_on():
    assert vpn_guard(Config()) is None
    assert vpn_guard(Config(outgoing_interfaces="NordLynx", vpn_kill_switch=False)) is None
    guard = vpn_guard(Config(outgoing_interfaces="NoSuchAdapter-cinemind-test"))
    assert guard is not None and "NoSuchAdapter" in guard()


def test_this_machine_has_a_loopback_interface():
    assert any(i.up and ("127.0.0.1" in i.addresses or "::1" in i.addresses)
               for i in interfaces())  # fmt: skip

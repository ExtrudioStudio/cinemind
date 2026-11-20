# 0007: The VPN kill switch pauses BitTorrent; it doesn't fail the job

Status: accepted (Phase 7)

## Context

`outgoing_interfaces` binds libtorrent to the VPN adapter. Binding alone isn't a kill switch.
When the VPN drops, its adapter can disappear, and the session's background traffic keeps
going: DHT, local peer discovery, UPnP and NAT-PMP. When the adapter comes back, it can come
back on a route the binding doesn't cover. Most VPN apps have a kill switch of their own, but
it's a setting the user must remember, and cinemind can't see it.

Downloads run unattended: from the home menu, from `cinemind resume`, from an agent through MCP.
Failing a job when the VPN is down would throw away its place in line and need a person to
restart it. Starting it anyway would leak the very address the setting exists to hide.

## Decision

- **With `outgoing_interfaces` set, BitTorrent runs only while that interface is up with an
  IP address** (`vpn_kill_switch`, on by default). The engine checks every 5 seconds
  (`cinemind.core.network.vpn_down`, with psutil, so adapter names work as Windows shows them).
- **Down at the start: no libtorrent session is created at all.** So no listening socket, no
  DHT bootstrap and no port mapping. Jobs wait, and their files show "waiting for VPN".
- **Down mid-download: the session is paused** and DHT, LSD, UPnP and NAT-PMP are turned off.
  They're turned back on, and the session resumed, when the interface returns.
- **An outage doesn't count against a torrent.** The metadata and stall timers are moved on by
  the time it lasted, so a long outage doesn't read as a dead swarm.
- **A failing check counts as "down".** If the interface list can't be read, nothing starts.
- `cinemind doctor` reports the interface's state and lists the interfaces that are up, so a
  wrong adapter name is easy to fix.

## Consequences

- A job can wait indefinitely for a VPN that never comes back. It's visible
  (`waiting for VPN` in the progress table, the reason in the log), and `cinemind doctor`
  says why. That's preferred to failing silently or leaking.
- Direct HTTP downloads (the Internet Archive's public-domain films) aren't held back. The
  switch protects the address torrent peers and trackers see, and direct downloads don't
  join a swarm.
- psutil became a dependency. It has wheels for every platform CI covers, Alpine included.

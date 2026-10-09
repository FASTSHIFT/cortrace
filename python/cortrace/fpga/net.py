"""net — locate the trace FPGA on whatever link it is plugged into.

The FPGA answers ARP but *not* ICMP, and its IP is fixed in the bitstream
(192.168.10.42, MAC 02:ca:fe:*). Two topologies must both "just work":

  1. Router / switch:  FPGA and PC share one L2 segment via the main NIC
     (ens33, 192.168.10.245). IP routing already reaches .42 -- nothing to do.
  2. Type-C dock / direct attach: a second NIC (enx*) is cabled straight to the
     FPGA. If we naively add an address in the same 192.168.10.0/24 the kernel
     still routes .42 out the *first* NIC (weak-host / ARP-flux), so IP traffic
     silently uses the wrong port.

Industry answer for "same device, either a switched net or a direct cable" is
per-interface discovery + bind-to-device: probe the FPGA's MAC with a raw ARP
who-has on *every* UP ethernet interface, and whichever interface gets the reply
is the one physically wired to it. We then hand callers back that interface so
sockets can SO_BINDTODEVICE to it, sidestepping the routing ambiguity entirely.

This is the passive/PC-only half of the adaptive scheme (no FPGA firmware
change): the FPGA keeps its fixed IP, we just find the right wire. The active
half (FPGA does DHCP -> 169.254 link-local fallback + periodic announce) can be
layered on later; discover_fpga() already returns the observed IP so a future
non-.42 address is handled with no change here.

Usage as a CLI:
  sudo cortrace fpga net                 # scan all ifaces, print where it is
  sudo cortrace fpga net --ip 192.168.10.42

As a library:
  from cortrace.fpga.net import discover_fpga, bind_udp_socket
  info = discover_fpga()          # -> {'iface','ip','mac'} or None
  sock = bind_udp_socket(info)    # UDP socket pinned to that interface
"""

import argparse
import fcntl
import os
import socket
import struct
import sys
import time

DEFAULT_FPGA_IP = "192.168.10.42"
FPGA_MAC_PREFIX = b"\x02\xca\xfe"  # the bitstream's 02:ca:fe:* locally-admin MAC

SIOCGIFHWADDR = 0x8927
SIOCGIFADDR = 0x8915
SIOCGIFFLAGS = 0x8913
IFF_UP = 0x1
IFF_LOOPBACK = 0x8

# cached result for the current process; discovery is not free (raw sockets +
# per-iface timeout) so callers that hit it repeatedly reuse the last answer.
_CACHE = None


def _iface_mac(ifname):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        info = fcntl.ioctl(
            s.fileno(), SIOCGIFHWADDR, struct.pack("256s", ifname.encode()[:15])
        )
        return info[18:24]
    finally:
        s.close()


def _iface_ipv4(ifname):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        info = fcntl.ioctl(
            s.fileno(), SIOCGIFADDR, struct.pack("256s", ifname.encode()[:15])
        )
        return socket.inet_ntoa(info[20:24])
    except OSError:
        return None  # interface has no IPv4 (fine for raw ARP)
    finally:
        s.close()


def _iface_is_up(ifname):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        info = fcntl.ioctl(
            s.fileno(), SIOCGIFFLAGS, struct.pack("256s", ifname.encode()[:15])
        )
        flags = struct.unpack("H", info[16:18])[0]
        return bool(flags & IFF_UP) and not (flags & IFF_LOOPBACK)
    except OSError:
        return False
    finally:
        s.close()


def _ethernet_ifaces():
    """UP, non-loopback interfaces that look like ethernet (have a real MAC)."""
    out = []
    for name in os.listdir("/sys/class/net"):
        if name == "lo":
            continue
        if not _iface_is_up(name):
            continue
        try:
            mac = _iface_mac(name)
        except OSError:
            continue
        if mac == b"\x00" * 6:
            continue
        out.append(name)
    return out


def _arp_probe(ifname, dst_ip, timeout=1.5):
    """Send ARP who-has dst_ip on ifname, return (ip, mac) of the replier or None.

    Requires CAP_NET_RAW (run under sudo). Source IP is the interface's own IPv4
    if it has one, else 0.0.0.0 -- the FPGA replies to who-has regardless."""
    src_mac = _iface_mac(ifname)
    src_ip_str = _iface_ipv4(ifname) or "0.0.0.0"
    try:
        s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0806))
    except PermissionError:
        raise PermissionError("ARP discovery needs root; run under sudo") from None
    s.bind((ifname, 0))
    s.settimeout(timeout)
    src_ip = socket.inet_aton(src_ip_str)
    dst_ip_b = socket.inet_aton(dst_ip)

    pkt = b"\xff\xff\xff\xff\xff\xff" + src_mac + b"\x08\x06"
    pkt += struct.pack("!HHBBH", 1, 0x0800, 6, 4, 1)  # ether, ipv4, req
    pkt += src_mac + src_ip + b"\x00" * 6 + dst_ip_b
    try:
        s.send(pkt)
    except OSError:
        s.close()
        return None

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            data = s.recv(2048)
        except socket.timeout:
            break
        if len(data) < 42 or data[12:14] != b"\x08\x06":
            continue
        op = struct.unpack("!H", data[20:22])[0]
        if op != 2:  # want ARP reply
            continue
        sender_ip = socket.inet_ntoa(data[28:32])
        if sender_ip != dst_ip:
            continue
        sender_mac = data[22:28]
        s.close()
        return sender_ip, sender_mac
    s.close()
    return None


def discover_fpga(ip=DEFAULT_FPGA_IP, ifaces=None, timeout=1.5, use_cache=True):
    """Find which interface the FPGA is wired to.

    Returns {'iface','ip','mac'} for the interface whose ARP probe got a reply,
    or None if no interface saw it. Prefers a replier whose MAC matches the
    bitstream's 02:ca:fe:* prefix when several answer (shouldn't happen on a sane
    net, but a real switch could bridge the probe out of two ports)."""
    global _CACHE  # pylint: disable=global-statement
    if use_cache and _CACHE is not None:
        return _CACHE
    if ifaces is None:
        ifaces = _ethernet_ifaces()

    best = None
    for name in ifaces:
        res = _arp_probe(name, ip, timeout=timeout)
        if res is None:
            continue
        found_ip, mac = res
        info = {"iface": name, "ip": found_ip, "mac": mac}
        if mac[:3] == FPGA_MAC_PREFIX:
            _CACHE = info  # exact vendor match wins immediately
            return info
        if best is None:
            best = info
    _CACHE = best
    return best


def bind_udp_socket(info, rcvbuf=None):
    """UDP socket pinned to the discovered interface via SO_BINDTODEVICE.

    Pass the dict from discover_fpga(); this removes the same-subnet routing
    ambiguity by forcing egress out the wired interface. Needs root for
    SO_BINDTODEVICE. If info is None, returns a plain unbound UDP socket (falls
    back to normal kernel routing -- correct for the single-NIC/router case)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if rcvbuf:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
    if info and info.get("iface"):
        try:
            s.setsockopt(
                socket.SOL_SOCKET,
                socket.SO_BINDTODEVICE,
                (info["iface"] + "\0").encode(),
            )
        except PermissionError:
            print(
                "[fpga_net] warning: SO_BINDTODEVICE needs root; "
                "using default routing (may egress wrong NIC)",
                file=sys.stderr,
            )
    return s


def resolve_ip(cli_ip=None):
    """Convenience for scripts: return the FPGA IP to talk to.

    If discovery finds it, return the observed IP (future-proof for a non-.42
    link-local address); else fall back to the CLI/default. Swallows the
    permission error so non-root callers still work in the router topology."""
    try:
        info = discover_fpga(ip=cli_ip or DEFAULT_FPGA_IP)
    except PermissionError:
        return cli_ip or DEFAULT_FPGA_IP
    if info:
        return info["ip"]
    return cli_ip or DEFAULT_FPGA_IP


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--ip", default=DEFAULT_FPGA_IP, help="FPGA IP to ARP-probe")
    ap.add_argument(
        "--timeout", type=float, default=1.5, help="per-interface ARP timeout (s)"
    )
    ap.add_argument("--iface", help="probe only this interface")
    a = ap.parse_args(argv)

    ifaces = [a.iface] if a.iface else _ethernet_ifaces()
    print(f"probing {a.ip} on: {', '.join(ifaces)}")
    try:
        info = discover_fpga(ip=a.ip, ifaces=ifaces, timeout=a.timeout, use_cache=False)
    except PermissionError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if info is None:
        print("FPGA not found on any interface")
        return 1
    mac = ":".join(f"{b:02x}" for b in info["mac"])
    print(f"FPGA at {info['ip']} on {info['iface']} (mac {mac})")
    print(f"  -> bind sockets to '{info['iface']}' (SO_BINDTODEVICE) for direct-attach")
    return 0

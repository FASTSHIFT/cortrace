import pytest
from cortrace.fpga import net

MAC_FPGA = b"\x02\xca\xfe\x00\x00\x01"
MAC_OTHER = b"\xaa\xbb\xcc\x00\x00\x02"


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    monkeypatch.setattr(net, "_CACHE", None)


def probe_table(table):
    return lambda name, ip, timeout=1.5: table.get(name)


def test_discover_prefers_vendor_mac(monkeypatch):
    monkeypatch.setattr(
        net,
        "_arp_probe",
        probe_table(
            {"eth0": ("192.168.10.42", MAC_OTHER), "eth1": ("192.168.10.42", MAC_FPGA)}
        ),
    )
    info = net.discover_fpga(ifaces=["eth0", "eth1"])
    assert info["iface"] == "eth1" and info["mac"] == MAC_FPGA


def test_discover_falls_back_to_first_replier(monkeypatch):
    monkeypatch.setattr(
        net, "_arp_probe", probe_table({"eth0": ("192.168.10.42", MAC_OTHER)})
    )
    assert net.discover_fpga(ifaces=["eth0", "eth1"])["iface"] == "eth0"


def test_discover_none_and_cache(monkeypatch):
    calls = []

    def probe(name, _ip, timeout=1.5):  # pylint: disable=unused-argument
        calls.append(name)

    monkeypatch.setattr(net, "_arp_probe", probe)
    assert net.discover_fpga(ifaces=["eth0"]) is None
    assert net.discover_fpga(ifaces=["eth0"]) is None  # cached None is a result too
    net._CACHE = {"iface": "x", "ip": "1.2.3.4", "mac": b""}
    assert net.discover_fpga(ifaces=["eth0"])["iface"] == "x"
    assert net.discover_fpga(ifaces=["eth0"], use_cache=False) is None


def test_discover_scans_ethernet_ifaces_by_default(monkeypatch):
    monkeypatch.setattr(net, "_ethernet_ifaces", lambda: ["enx1"])
    monkeypatch.setattr(
        net, "_arp_probe", probe_table({"enx1": ("192.168.10.42", MAC_FPGA)})
    )
    assert net.discover_fpga()["iface"] == "enx1"


def test_resolve_ip(monkeypatch):
    monkeypatch.setattr(
        net, "discover_fpga", lambda ip: {"ip": "169.254.1.1", "iface": "e", "mac": b""}
    )
    assert net.resolve_ip("10.0.0.1") == "169.254.1.1"
    monkeypatch.setattr(net, "discover_fpga", lambda ip: None)
    assert net.resolve_ip("10.0.0.1") == "10.0.0.1"
    assert net.resolve_ip() == net.DEFAULT_FPGA_IP

    def deny(ip):
        raise PermissionError

    monkeypatch.setattr(net, "discover_fpga", deny)
    assert net.resolve_ip("10.0.0.9") == "10.0.0.9"


def test_bind_udp_socket_without_info_is_plain():
    sock = net.bind_udp_socket(None)
    assert sock.family.name == "AF_INET"
    sock.close()


def test_bind_udp_socket_tolerates_missing_privilege(monkeypatch, capsys):
    class Sock:
        def setsockopt(self, _level, opt, _value):
            if opt == net.socket.SO_BINDTODEVICE:
                raise PermissionError

    monkeypatch.setattr(net.socket, "socket", lambda *a: Sock())
    assert net.bind_udp_socket({"iface": "eth9"}, rcvbuf=1 << 20) is not None
    assert "SO_BINDTODEVICE" in capsys.readouterr().err


def test_main_reports_found_and_missing(monkeypatch, capsys):
    monkeypatch.setattr(
        net,
        "discover_fpga",
        lambda **_k: {"iface": "eth9", "ip": "192.168.10.42", "mac": MAC_FPGA},
    )
    assert net.main(["--iface", "eth9"]) == 0
    assert "FPGA at 192.168.10.42 on eth9" in capsys.readouterr().out
    monkeypatch.setattr(net, "discover_fpga", lambda **_k: None)
    assert net.main(["--iface", "eth9"]) == 1

    def deny(**_k):
        raise PermissionError("need root")

    monkeypatch.setattr(net, "discover_fpga", deny)
    assert net.main(["--iface", "eth9"]) == 2

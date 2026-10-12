"""Unit tests for perfetto_open's testable pure logic (free-port picker and
the host-page HTML that drives the postMessage handoff)."""

import json
import socket

from cortrace import perfetto_open as po


def test_pick_free_port_is_bindable():
    port = po._pick_free_port("127.0.0.1")
    assert 1 <= port <= 65535
    # the port was released, so we can bind it ourselves right after.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
    finally:
        s.close()


def test_host_page_template_has_handshake_pieces():
    # The template must carry the postMessage handshake: PING/PONG and the
    # perfetto buffer payload, and same-origin fetch of the trace.
    tpl = po._HOST_PAGE
    assert "PING" in tpl
    assert "PONG" in tpl
    assert "perfetto" in tpl
    assert "window.open" in tpl
    assert "fetch(TRACE_URL)" in tpl


def test_host_page_substitution_produces_valid_js_strings():
    # Mimic the %(...)s substitution done in serve_and_open and check the JS
    # string literals are valid JSON (that's how we escape them).
    html = po._HOST_PAGE % {
        "origin": json.dumps("https://ui.perfetto.dev"),
        "trace_url": json.dumps("http://127.0.0.1:8099/t.perfetto"),
        "beacon_url": json.dumps("http://127.0.0.1:8099/__delivered"),
        "title": json.dumps("t.perfetto"),
    }
    assert '"https://ui.perfetto.dev"' in html
    assert '"http://127.0.0.1:8099/t.perfetto"' in html
    assert "%(" not in html  # all placeholders substituted


# ---- serve_and_open integration (no browser) ------------------------------
def test_serve_and_open_serves_host_page_trace_and_beacon(tmp_path):
    import threading
    import time
    import urllib.request

    trace = tmp_path / "t.perfetto"
    trace.write_bytes(b"PERFETTO_TEST_BYTES")

    # Run the server with keep=True + do_open=False so it stays up serving on
    # a daemon thread; we fetch host page + trace + beacon to exercise the
    # handler paths, then leave the daemon thread to be torn down at exit.
    port = po._pick_free_port("127.0.0.1")

    def run():
        po.serve_and_open(str(trace), port=port, keep=True, do_open=False, timeout=0)

    base = f"http://127.0.0.1:{port}"
    srv = threading.Thread(target=run, daemon=True)
    srv.start()
    time.sleep(0.4)  # let the server bind

    html = urllib.request.urlopen(base + "/", timeout=5).read().decode()
    assert "PING" in html and "fetch(TRACE_URL)" in html
    body = urllib.request.urlopen(base + "/t.perfetto", timeout=5).read()
    assert body == b"PERFETTO_TEST_BYTES"
    beacon = urllib.request.urlopen(base + "/__delivered", timeout=5)
    assert beacon.status in (200, 204)


def test_serve_and_open_missing_file_returns_error(tmp_path):
    rc = po.serve_and_open(str(tmp_path / "nope.perfetto"), do_open=False)
    assert rc == 2


# ---- _open_browser --------------------------------------------------------
def test_open_browser_uses_xdg_open(monkeypatch):
    launched = {}
    monkeypatch.setattr(
        po.shutil,
        "which",
        lambda name: "/usr/bin/xdg-open" if name == "xdg-open" else None,
    )

    class FakePopen:
        def __init__(self, argv, **_kw):
            launched["argv"] = argv

    monkeypatch.setattr(po.subprocess, "Popen", FakePopen)
    assert po._open_browser("http://x/") is True
    assert launched["argv"][0] == "/usr/bin/xdg-open"
    assert launched["argv"][1] == "http://x/"


def test_open_browser_falls_back_to_webbrowser(monkeypatch):
    monkeypatch.setattr(po.shutil, "which", lambda name: None)
    import webbrowser

    monkeypatch.setattr(webbrowser, "open", lambda url: True)
    assert po._open_browser("http://x/") is True


# ---- main -----------------------------------------------------------------
def test_main_missing_file_returns_2():
    assert po.main(["/nonexistent/nope.perfetto", "--no-open"]) == 2


def test_main_no_open_prints_url(tmp_path, capsys):
    trace = tmp_path / "t.perfetto"
    trace.write_bytes(b"X")
    rc = po.main([str(trace), "--no-open"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "http://127.0.0.1:" in out


# ---- more serve_and_open paths ---------------------------------------------


def test_open_browser_gives_up_when_the_webbrowser_module_fails(monkeypatch):
    monkeypatch.setattr(po.shutil, "which", lambda name: None)
    import webbrowser

    def boom(_url):
        raise RuntimeError("no browser")

    monkeypatch.setattr(webbrowser, "open", boom)
    assert po._open_browser("http://x/") is False


def test_serve_and_open_rejects_an_empty_trace(tmp_path, capsys):
    trace = tmp_path / "e.perfetto"
    trace.write_bytes(b"")
    assert po.serve_and_open(str(trace), do_open=False) == 2
    assert "empty" in capsys.readouterr().err


def test_serve_and_open_reports_a_port_that_is_taken(tmp_path, capsys):
    trace = tmp_path / "t.perfetto"
    trace.write_bytes(b"X")
    busy = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    busy.bind(("127.0.0.1", 0))
    busy.listen(1)
    try:
        port = busy.getsockname()[1]
        assert po.serve_and_open(str(trace), port=port, do_open=False) == 2
    finally:
        busy.close()
    assert "cannot bind" in capsys.readouterr().err


def fetch_when_up(url, paths):
    """Request `paths` from a server that is starting in another thread."""
    import time
    import urllib.error
    import urllib.request

    codes = {}
    for path in paths:
        for _ in range(50):
            try:
                codes[path] = urllib.request.urlopen(url + path, timeout=2).status
                break
            except urllib.error.HTTPError as exc:
                codes[path] = exc.code
                break
            except OSError:
                time.sleep(0.1)
    return codes


def test_serve_and_open_returns_once_the_page_has_the_trace(tmp_path, monkeypatch):
    import threading

    trace = tmp_path / "t.perfetto"
    trace.write_bytes(b"X")
    port = po._pick_free_port("127.0.0.1")
    opened = []
    monkeypatch.setattr(po, "_open_browser", lambda url: opened.append(url) or True)
    codes = {}

    def browser():
        codes.update(
            fetch_when_up(f"http://127.0.0.1:{port}", ["/nope", "/__delivered"])
        )

    thread = threading.Thread(target=browser, daemon=True)
    thread.start()
    assert po.serve_and_open(str(trace), port=port, timeout=10) == 0
    thread.join(5)
    assert opened == [f"http://127.0.0.1:{port}/"]
    assert codes["/nope"] == 404 and codes["/__delivered"] == 204


def test_serve_and_open_times_out_and_prints_the_url_without_a_browser(
    tmp_path, monkeypatch, capsys
):
    trace = tmp_path / "t.perfetto"
    trace.write_bytes(b"X")
    monkeypatch.setattr(po, "_open_browser", lambda url: False)
    assert po.serve_and_open(str(trace), timeout=0.2) == 1
    captured = capsys.readouterr()
    assert "http://127.0.0.1:" in captured.out
    assert "timed out" in captured.err

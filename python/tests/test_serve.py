import os

import pytest
from cortrace import serve


def argv(tmp_path, *extra):
    return ["--out-dir", str(tmp_path), "--elf", "e", "--iface", "eth0"] + list(extra)


def test_parse_requires_output_dir(monkeypatch, capsys):
    monkeypatch.delenv("CORTRACE_OUT_DIR", raising=False)
    with pytest.raises(SystemExit):
        serve.parse_args(["--elf", "e", "--iface", "eth0"])
    assert "--out-dir is required" in capsys.readouterr().err


def test_parse_defaults_and_forwarding(tmp_path):
    a = serve.parse_args(argv(tmp_path, "--port", "9000", "--pynuttx", "/p"))
    assert a.port == 9000 and a.extra == ["--pynuttx", "/p"]
    assert serve.parse_args(argv(tmp_path)).port == 8037


def test_serve_always_streams_the_flat_format(tmp_path, capsys):
    """The Record flow hands TracePackets to the UI; an archive cannot be that."""
    assert serve.parse_args(argv(tmp_path)).merge_format == "flat"
    with pytest.raises(SystemExit):
        serve.parse_args(argv(tmp_path, "--merge-format", "archive"))
    assert "--merge-format flat" in capsys.readouterr().err


def test_capture_callback_runs_the_pipeline_with_a_fresh_tag(monkeypatch, tmp_path):
    a = serve.parse_args(argv(tmp_path, "--fuse"))
    tags = []
    monkeypatch.setattr(
        serve.capture, "run", lambda args, extra=(): tags.append(args.tag) or "/r.pf"
    )
    monkeypatch.setattr(serve.record_bridge, "load_trace_packets", lambda p: [p])
    assert serve.make_capture_cb(a)() == ["/r.pf"]
    assert tags[0] and tags[0] != "None"


def test_main_starts_relay_serves_and_cleans_up(monkeypatch, tmp_path, capsys):
    seen = {}

    def fake_serve(bridge, sock_path):
        seen["sock_dir"] = os.path.dirname(sock_path)
        seen["mode"] = os.stat(seen["sock_dir"]).st_mode & 0o777
        seen["bridge"] = bridge

    monkeypatch.setattr(serve.record_bridge, "serve_forever", fake_serve)
    assert serve.main(argv(tmp_path, "--port", "0", "--quiet")) == 0
    assert seen["mode"] == 0o700
    assert not os.path.exists(seen["sock_dir"])  # removed on exit
    assert "ui.perfetto.dev" in capsys.readouterr().err

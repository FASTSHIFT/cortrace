import pytest
from cortrace import runstore


@pytest.mark.parametrize(
    "n,text",
    [
        (0, "0 B"),
        (999, "999 B"),
        (1500, "1.5 KB"),
        (75_000_000, "75.0 MB"),
        (3e12, "3.0 TB"),
        (5e15, "5000.0 TB"),
    ],
)
def test_human(n, text):
    assert runstore.human(n) == text


def test_estimates_scale_with_duration_and_raw_size():
    assert runstore.estimate_capture_bytes(1) == 75_000_000 * 6
    assert runstore.estimate_decode_bytes(100) == 500


def test_long_capture_needs_an_explicit_flag():
    runstore.check_duration(5.0, allow_long=False)
    with pytest.raises(SystemExit) as e:
        runstore.check_duration(6.0, allow_long=False)
    assert "--allow-long" in str(e.value)
    runstore.check_duration(600, allow_long=True)


def test_free_bytes_walks_up_to_an_existing_directory(tmp_path):
    deep = tmp_path / "a" / "b" / "c"
    assert runstore.free_bytes(str(deep)) == runstore.free_bytes(str(tmp_path))


def test_require_space(monkeypatch, tmp_path):
    monkeypatch.setattr(runstore, "free_bytes", lambda p: 1_000)
    runstore.require_space(str(tmp_path), 900, "x")  # 900 * 1.1 <= 1000
    with pytest.raises(SystemExit) as e:
        runstore.require_space(str(tmp_path), 1_000, "a 1 s capture")
    msg = str(e.value)
    assert "a 1 s capture" in msg and "1.0 KB" in msg and "--out-dir" in msg

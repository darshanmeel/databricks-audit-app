"""Tests for tools/first_run.py: the wrapper that chains tools/export_direct_results.py ->
tools/load_direct_results.py into one command, so the documented first-run flow is
"python tools/first_run.py" then "python -m app.api" (open http://127.0.0.1:8000/) instead of
two separate commands (README.md).

The subprocess layer is fully faked: tools/first_run.py's main() takes an injectable `run`
parameter (the same shape as subprocess.run) that every test replaces with FakeRun below, which
records every call and returns a scripted exit code -- no network, no real export, no load, ever.
tools/first_run.py is loaded by file path (the same importlib.util.spec_from_file_location
technique other test files use), so this runs correctly under a plain
`pytest tests/test_first_run.py -q` regardless of pytest's rootdir/sys.path behaviour.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "first_run.py"
NOW = dt.datetime(2026, 9, 28, 10, 30, 0, tzinfo=dt.timezone.utc)


def _load_module():
    spec = importlib.util.spec_from_file_location("first_run_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# Loading this module at collection time is itself a guarantee: databricks-sql-connector is
# confirmed NOT installed in this checkout (README.md "Install"), so if tools/first_run.py
# imported it (directly, or transitively by importing tools/export_direct_results.py instead of
# spawning it as a subprocess) this import would raise ImportError before a single test ran.
first_run = _load_module()


class FakeResult:
    def __init__(self, returncode: int):
        self.returncode = returncode


class FakeRun:
    """Stands in for subprocess.run: records every (cmd, kwargs) call and returns a scripted
    returncode per call index (0 once the script runs out), so tests can assert exact argv,
    ordering, and exit-code propagation without ever spawning a real process."""

    def __init__(self, returncodes=None):
        self.calls: list[dict] = []
        self._returncodes = list(returncodes) if returncodes is not None else []

    def __call__(self, cmd, **kwargs):
        self.calls.append({"cmd": list(cmd), "kwargs": kwargs})
        idx = len(self.calls) - 1
        rc = self._returncodes[idx] if idx < len(self._returncodes) else 0
        return FakeResult(rc)


def _argv(call: dict) -> list[str]:
    return call["cmd"]


def _expected_out_dir(tmp_path) -> Path:
    return tmp_path / "results" / first_run._stamp(NOW)


# ---------------------------------------------------------------------------
# Ordering and argv
# ---------------------------------------------------------------------------


def test_two_steps_run_in_order_with_right_argv(monkeypatch, tmp_path):
    monkeypatch.setenv("AUDIT_DATA_DIR", str(tmp_path))
    fake = FakeRun()
    rc = first_run.main([], run=fake, now=NOW)
    assert rc == 0
    assert len(fake.calls) == 2

    out_dir = _expected_out_dir(tmp_path)
    export_cmd = _argv(fake.calls[0])
    assert export_cmd[0] == sys.executable
    assert export_cmd[1] == "tools/export_direct_results.py"
    assert export_cmd[export_cmd.index("--out") + 1] == str(out_dir)
    assert "--windows" not in export_cmd

    load_cmd = _argv(fake.calls[1])
    assert load_cmd[1] == "tools/load_direct_results.py"
    assert load_cmd[2] == str(out_dir)

    # Every step runs from the repo root, regardless of the test runner's own cwd (the "works
    # from an unpacked zip" requirement).
    for call in fake.calls:
        assert call["kwargs"]["cwd"] == first_run.ROOT


def test_windows_passthrough(monkeypatch, tmp_path):
    monkeypatch.setenv("AUDIT_DATA_DIR", str(tmp_path))
    fake = FakeRun()
    first_run.main(["--windows", "90"], run=fake, now=NOW)
    export_cmd = _argv(fake.calls[0])
    assert export_cmd[export_cmd.index("--windows") + 1] == "90"


def test_out_dir_is_a_fresh_stamped_folder_under_the_data_folder(monkeypatch, tmp_path):
    monkeypatch.setenv("AUDIT_DATA_DIR", str(tmp_path))
    assert first_run.export_out_dir(NOW) == tmp_path / "results" / "20260928T103000Z"


def test_steps_stream_output_not_captured(monkeypatch, tmp_path):
    """House style (tools/gate.py's _run): subprocess output goes straight to the terminal, never
    captured/parsed."""
    monkeypatch.setenv("AUDIT_DATA_DIR", str(tmp_path))
    fake = FakeRun()
    first_run.main([], run=fake, now=NOW)
    for call in fake.calls:
        kwargs = call["kwargs"]
        assert kwargs.get("capture_output") is not True
        assert "stdout" not in kwargs
        assert "stderr" not in kwargs


# ---------------------------------------------------------------------------
# Failure propagation: stop at the first nonzero step, propagate its exit code.
# ---------------------------------------------------------------------------


def test_nonzero_exit_from_export_stops_chain_and_propagates(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AUDIT_DATA_DIR", str(tmp_path))
    fake = FakeRun(returncodes=[2])
    rc = first_run.main([], run=fake, now=NOW)
    assert rc == 2
    assert len(fake.calls) == 1  # never reached load
    err = capsys.readouterr().err
    assert "step 1" in err
    assert "tools/export_direct_results.py" in err


def test_export_failure_exit_2_points_at_env_example(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AUDIT_DATA_DIR", str(tmp_path))
    fake = FakeRun(returncodes=[2])
    rc = first_run.main([], run=fake, now=NOW)
    assert rc == 2
    err = capsys.readouterr().err
    assert ".env.example" in err


def test_nonzero_exit_from_load_stops_chain_and_propagates(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AUDIT_DATA_DIR", str(tmp_path))
    fake = FakeRun(returncodes=[0, 1])
    rc = first_run.main([], run=fake, now=NOW)
    assert rc == 1
    assert len(fake.calls) == 2
    err = capsys.readouterr().err
    assert "step 2" in err
    assert str(_expected_out_dir(tmp_path)) in err


def test_load_failure_never_prints_success_line(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AUDIT_DATA_DIR", str(tmp_path))
    fake = FakeRun(returncodes=[0, 1])
    first_run.main([], run=fake, now=NOW)
    out = capsys.readouterr().out
    assert first_run.APP_CMD not in out
    assert first_run.APP_URL not in out


# ---------------------------------------------------------------------------
# Success: the final line names the URL to open.
# ---------------------------------------------------------------------------


def test_success_prints_app_command_and_url_as_final_lines(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AUDIT_DATA_DIR", str(tmp_path))
    fake = FakeRun()
    rc = first_run.main([], run=fake, now=NOW)
    assert rc == 0
    out = capsys.readouterr().out
    assert first_run.APP_CMD in out
    lines = [line for line in out.splitlines() if line.strip()]
    assert lines[-1].strip() == f"then open {first_run.APP_URL} in a browser"


# ---------------------------------------------------------------------------
# Credentials: no token-shaped argument accepted or forwarded.
# ---------------------------------------------------------------------------


def test_no_credential_shaped_flag_in_parser():
    parser = first_run.build_parser()
    forbidden = ("token", "password", "secret", "credential")
    for action in parser._actions:  # noqa: SLF001
        for opt in action.option_strings:
            lname = opt.lstrip("-").lower()
            assert not any(bad in lname for bad in forbidden), f"forbidden flag found: {opt}"


def test_token_flag_rejected_by_argparse():
    with pytest.raises(SystemExit):
        first_run.parse_args(["--token", "dapi-should-not-work"])


def test_forwarded_argv_never_contains_a_token_shaped_flag(monkeypatch, tmp_path):
    monkeypatch.setenv("AUDIT_DATA_DIR", str(tmp_path))
    fake = FakeRun()
    first_run.main(["--windows", "30"], run=fake, now=NOW)
    for call in fake.calls:
        for part in _argv(call):
            assert "token" not in part.lower()
            assert "password" not in part.lower()
            assert "secret" not in part.lower()


def test_first_run_module_defines_no_credential_handling():
    forbidden_names = {
        "DATABRICKS_SERVER_HOSTNAME", "DATABRICKS_HTTP_PATH", "DATABRICKS_TOKEN",
        "connect", "_load_dotenv", "scrub_secrets",
    }
    assert forbidden_names.isdisjoint(vars(first_run).keys())

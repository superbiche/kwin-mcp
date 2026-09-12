"""Regression tests that never attach to a desktop or inject real input."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from kwin_mcp.core import AutomationEngine
from kwin_mcp.processes import stop_processes
from kwin_mcp.session import LiveSession, Session, SessionConfig, SessionInfo


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("WAYLAND_DISPLAY", "host-display")
    monkeypatch.setenv("DISPLAY", ":host")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "selected-runtime"))
    monkeypatch.setattr("kwin_mcp.core.process_running", lambda proc: proc.poll() is None)
    result = AutomationEngine()
    result._session = LiveSession("unix:path=/selected-bus", "selected-display", tmp_path)
    result._input = Mock()
    yield result
    result.session_stop()


def test_unicode_uses_selected_display_without_clipboard(engine, monkeypatch, tmp_path):
    monkeypatch.setattr("kwin_mcp.core.shutil.which", lambda _: "/usr/bin/wtype")
    run = Mock(return_value=subprocess.CompletedProcess([], 0, b"", b""))
    monkeypatch.setattr("kwin_mcp.core.subprocess.run", run)
    assert "Typed unicode" in engine.keyboard_type_unicode("é日本語🙂")
    env = run.call_args.kwargs["env"]
    assert env["WAYLAND_DISPLAY"] == str(tmp_path / "selected-runtime/selected-display")
    assert env["XDG_RUNTIME_DIR"] == str(tmp_path / "selected-runtime")
    assert env["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/selected-bus"
    assert "DISPLAY" not in env
    assert engine._wl_copy_proc is None
    engine._input.keyboard_key.assert_not_called()


@pytest.mark.parametrize("wtype_available", [True, False])
def test_unicode_never_bypasses_clipboard_opt_in(engine, monkeypatch, wtype_available):
    monkeypatch.setattr("kwin_mcp.core.shutil.which", lambda _: wtype_available)
    monkeypatch.setattr(
        "kwin_mcp.core.subprocess.run",
        Mock(
            return_value=subprocess.CompletedProcess(
                [], 1, b"", b"Compositor does not support the virtual keyboard protocol"
            )
        ),
    )
    popen = Mock(side_effect=AssertionError("Must not start clipboard owner"))
    monkeypatch.setattr("kwin_mcp.core.subprocess.Popen", popen)
    assert "enable_clipboard=True" in engine.keyboard_type_unicode("é")
    popen.assert_not_called()
    engine._input.keyboard_key.assert_not_called()


def test_unicode_does_not_duplicate_partial_input(engine, monkeypatch):
    engine._clipboard_enabled = True
    monkeypatch.setattr("kwin_mcp.core.shutil.which", lambda _: True)
    monkeypatch.setattr(
        "kwin_mcp.core.subprocess.run",
        Mock(return_value=subprocess.CompletedProcess([], 1, b"", b"connection lost")),
    )
    assert "partial" in engine.keyboard_type_unicode("é")
    engine._input.keyboard_key.assert_not_called()


def test_unicode_fallback_uses_owned_confirmed_clipboard(engine, monkeypatch, tmp_path):
    engine._clipboard_enabled = True
    monkeypatch.setattr("kwin_mcp.core.shutil.which", lambda _: False)
    owner = Mock(pid=123456, returncode=None)
    owner.poll.return_value = None
    popen = Mock(return_value=owner)
    monkeypatch.setattr("kwin_mcp.core.subprocess.Popen", popen)
    monkeypatch.setattr(
        "kwin_mcp.core.subprocess.run",
        Mock(return_value=subprocess.CompletedProcess([], 0, "é".encode(), b"")),
    )
    stopped = Mock()
    monkeypatch.setattr("kwin_mcp.core.stop_processes", stopped)
    assert "Typed unicode" in engine.keyboard_type_unicode("é")
    assert popen.call_args.args[0] == ["wl-copy", "--foreground", "--", "é"]
    assert popen.call_args.kwargs["start_new_session"] is True
    assert popen.call_args.kwargs["env"]["WAYLAND_DISPLAY"] == str(
        tmp_path / "selected-runtime/selected-display"
    )
    engine._input.keyboard_key.assert_called_once_with("ctrl+v")
    engine.session_stop()
    stopped.assert_called_once_with([owner])
    assert engine._wl_copy_proc is None


def test_clipboard_failure_never_pastes(engine, monkeypatch):
    engine._clipboard_enabled = True
    monkeypatch.setattr("kwin_mcp.core.shutil.which", lambda _: False)
    owner = Mock(pid=123456)
    owner.poll.return_value = 1
    monkeypatch.setattr("kwin_mcp.core.subprocess.Popen", Mock(return_value=owner))
    monkeypatch.setattr("kwin_mcp.core.stop_processes", Mock())
    assert "Failed to set clipboard" in engine.keyboard_type_unicode("é")
    engine._input.keyboard_key.assert_not_called()
    assert engine._wl_copy_proc is None


def test_cleanup_continues_after_input_failure(engine):
    session = engine._session
    engine._input.close.side_effect = RuntimeError("broken input close")
    with pytest.raises(ExceptionGroup, match="cleanup failed"):
        engine.session_stop()
    assert not session.is_running
    assert engine._session is None
    engine._input.close.side_effect = None


def test_clipboard_cleanup_without_session(engine, monkeypatch):
    engine.session_stop()
    owner = Mock()
    engine._wl_copy_proc = owner
    stopped = Mock()
    monkeypatch.setattr("kwin_mcp.core.stop_processes", stopped)
    engine.session_stop()
    stopped.assert_called_once_with([owner])


def alive(pid):
    path = Path(f"/proc/{pid}/stat")
    try:
        return path.read_text().split(") ", 1)[1][0] != "Z"
    except FileNotFoundError:
        return False


@pytest.mark.parametrize("virtual", [True, False])
def test_stop_kills_stubborn_app_descendants_but_not_unrelated(tmp_path, virtual):
    marker = tmp_path / "ready"
    script = tmp_path / "app.py"
    script.write_text(
        "import os, signal, time, pathlib\n"
        "child = os.fork()\n"
        "if child == 0:\n"
        " signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f" pathlib.Path({str(marker)!r}).write_text(str(os.getpid()))\n"
        "while True: time.sleep(1)\n"
    )
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
    )
    session = LiveSession("unix:path=/unused", "unused", tmp_path)
    if virtual:
        session = Session()
        session._process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
        )
        session._info = SessionInfo(
            "unix:path=/unused", "unused", session._process.pid, screenshot_dir=tmp_path
        )
        session._runtime_dir = str(tmp_path)
    app = session.launch_app([sys.executable, str(script)])
    child = None
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        child = int(marker.read_text())
        session.stop()
        deadline = time.monotonic() + 2
        while alive(child) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not alive(app.pid)
        assert not alive(child)
        assert unrelated.poll() is None
        session.stop()
    finally:
        session.stop()
        stop_processes([unrelated], timeout=0.1)


def test_existing_socket_is_never_deleted(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    path = tmp_path / "existing"
    path.write_text("sentinel")
    session = Session()
    with pytest.raises(RuntimeError, match="already exists"):
        session.start(SessionConfig(socket_name="existing"))
    session.stop()
    assert path.read_text() == "sentinel"


def test_startup_partial_line_has_deadline_and_cleans_up(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    session = Session()
    monkeypatch.setattr(session, "_build_wrapper_script", lambda _: "printf partial; sleep 30")
    original = session._read_startup
    monkeypatch.setattr(session, "_read_startup", lambda timeout: original(0.1))
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="timed out"):
        session.start(SessionConfig(isolate_home=True))
    assert time.monotonic() - started < 5
    assert session.info is None
    assert session._home_dir is None
    assert session._process is None


def test_failed_initial_app_launch_cleans_session(monkeypatch, tmp_path):
    session = Mock()
    session.start.return_value = SessionInfo("bus", "socket", 123, screenshot_dir=tmp_path)
    session.launch_app.side_effect = FileNotFoundError("missing app")
    monkeypatch.setattr("kwin_mcp.core.Session", Mock(return_value=session))
    engine = AutomationEngine()
    with pytest.raises(FileNotFoundError):
        engine.session_start(app_command="missing-app")
    session.stop.assert_called_once()
    assert engine._session is None
    assert not engine._clipboard_enabled


@pytest.mark.parametrize("shutdown", ["eof", "sigterm"])
def test_mcp_stdio_shutdown_stops_owned_apps(tmp_path, shutdown):
    import json
    import selectors

    marker = tmp_path / "pid"
    script = (
        "from pathlib import Path; import sys; "
        "from kwin_mcp import server; from kwin_mcp.session import LiveSession; "
        f"server._engine._session=LiveSession('unused','unused',Path({str(tmp_path)!r})); "
        "app=server._engine._session.launch_app("
        "[sys.executable,'-c','import time; time.sleep(60)']); "
        f"Path({str(marker)!r}).write_text(str(app.pid)); server.main()"
    )
    with (tmp_path / "transport.log").open("wb") as log:
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=log,
            start_new_session=True,
        )
        assert proc.stdin is not None and proc.stdout is not None
        try:
            request = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "lifecycle-regression", "version": "1"},
                },
            }
            proc.stdin.write((json.dumps(request) + "\n").encode())
            proc.stdin.flush()
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ)
                assert selector.select(15), "MCP initialization timed out"
                response = json.loads(proc.stdout.readline())
                assert "result" in response, response
            pid = int(marker.read_text())
            assert alive(pid)
            if shutdown == "sigterm":
                proc.terminate()
            else:
                proc.stdin.close()
            proc.wait(timeout=10)
            assert not alive(pid)
        finally:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
            proc.stdout.close()
            stop_processes([proc], timeout=0.1)


def test_status_does_not_reap_owned_group_leader(tmp_path):
    session = Session()
    proc = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    session._process = proc
    try:
        deadline = time.monotonic() + 3
        while session.is_running and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not session.is_running
        assert proc.returncode is None
        assert Path(f"/proc/{proc.pid}").exists(), "leader must remain reserved"
        session.stop()
        assert proc.returncode == 0
    finally:
        session.stop()


def test_group_signals_precede_reaping(monkeypatch):
    proc = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    real_killpg = os.killpg
    signals = []

    def checked_signal(pgid, sig):
        assert proc.returncode is None
        assert Path(f"/proc/{pgid}").exists(), "never signal a released PID"
        signals.append(sig)
        real_killpg(pgid, sig)

    monkeypatch.setattr("kwin_mcp.processes.os.killpg", checked_signal)
    stop_processes([proc], timeout=0.05)
    assert signal.SIGTERM in signals and signal.SIGKILL in signals
    assert proc.returncode is not None


def test_already_reaped_pid_is_never_signalled(monkeypatch):
    proc = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    proc.wait(timeout=3)
    killpg = Mock(side_effect=AssertionError("released PID must not be signalled"))
    monkeypatch.setattr("kwin_mcp.processes.os.killpg", killpg)
    stop_processes([proc])
    killpg.assert_not_called()


def test_session_metadata_does_not_grant_directory_ownership(tmp_path):
    borrowed = tmp_path / "borrowed"
    borrowed.mkdir()
    sentinel = borrowed / "keep"
    sentinel.write_text("unrelated data")
    session = Session()
    session._info = SessionInfo("unused", "unused", 0, screenshot_dir=borrowed)
    session.stop()
    assert sentinel.read_text() == "unrelated data"
    live = LiveSession("unused", "unused", borrowed)
    live.stop()
    assert sentinel.read_text() == "unrelated data"


def test_screenshot_directory_has_no_shared_default():
    import inspect

    parameter = inspect.signature(SessionInfo).parameters["screenshot_dir"]
    assert parameter.default is inspect.Parameter.empty


def test_cleanup_failure_preserves_ownership_for_retry(tmp_path, monkeypatch):
    session = Session()
    session._screenshot_dir = tmp_path / "owned"
    session._screenshot_dir.mkdir()
    session._info = SessionInfo("unused", "unused", 0, session._screenshot_dir)
    stop = Mock(side_effect=[RuntimeError("termination failed"), None])
    monkeypatch.setattr("kwin_mcp.session.stop_processes", stop)
    with pytest.raises(RuntimeError, match="termination failed"):
        session.stop()
    assert session.info is not None
    assert session._screenshot_dir.exists()
    session.stop()
    assert session.info is None
    assert not (tmp_path / "owned").exists()


def test_foreign_live_session_requires_absolute_socket(monkeypatch):
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "host-bus")
    engine = AutomationEngine()
    assert "absolute Wayland socket" in engine.session_connect("foreign-bus", "wayland-0")
    assert engine._session is None


def test_foreign_live_helpers_use_absolute_socket_and_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "host"))
    socket = tmp_path / "foreign/wayland-0"
    engine = AutomationEngine()
    engine._session = LiveSession("foreign-bus", str(socket), tmp_path)
    env = engine._session_env()
    assert env["WAYLAND_DISPLAY"] == str(socket)
    assert env["XDG_RUNTIME_DIR"] == str(socket.parent)
    assert env["DBUS_SESSION_BUS_ADDRESS"] == "foreign-bus"
    engine.session_stop()


@pytest.mark.parametrize("grace", [-1, 61, float("nan"), float("inf")])
def test_invalid_shutdown_grace_preserves_session(engine, grace):
    session = engine._session
    with pytest.raises(ValueError, match="grace_seconds"):
        engine.session_stop(grace_seconds=grace)
    assert engine._session is session
    assert session.is_running


def test_shutdown_grace_reaches_session():
    engine = AutomationEngine()
    session = Mock()
    engine._session = session
    engine.session_stop(grace_seconds=12)
    session.stop.assert_called_once_with(grace_seconds=12)


@pytest.mark.parametrize("kwin_present", [True, False])
def test_live_probe_closes_connection(tmp_path, monkeypatch, kwin_present):
    probe = Mock()
    probe.name_has_owner.return_value = kwin_present
    monkeypatch.setattr("kwin_mcp.core.InputBackend", Mock(return_value=Mock()))
    monkeypatch.setattr("kwin_mcp.core.time.sleep", lambda _: None)
    monkeypatch.setattr("dbus.bus.BusConnection", Mock(return_value=probe))
    engine = AutomationEngine()
    result = engine.session_connect("foreign-bus", str(tmp_path / "wayland-0"))
    probe.name_has_owner.assert_called_once_with("org.kde.KWin")
    probe.close.assert_called_once()
    assert ("Connected to live" in result) == kwin_present
    engine.session_stop()


def test_unicode_clipboard_confirmation_preserves_trailing_newline(engine, monkeypatch):
    engine._clipboard_enabled = True
    owner = Mock()
    owner.poll.return_value = None
    monkeypatch.setattr("kwin_mcp.core.shutil.which", lambda _: False)
    monkeypatch.setattr("kwin_mcp.core.subprocess.Popen", Mock(return_value=owner))
    monkeypatch.setattr(
        "kwin_mcp.core.subprocess.run",
        Mock(return_value=subprocess.CompletedProcess([], 0, "é\n".encode(), b"")),
    )
    monkeypatch.setattr("kwin_mcp.core.stop_processes", Mock())
    assert "Typed unicode" in engine.keyboard_type_unicode("é\n")
    engine._input.keyboard_key.assert_called_once_with("ctrl+v")


def test_screenshot_output_directory_is_required():
    import inspect

    from kwin_mcp.screenshot import capture_screenshot_to_file

    assert inspect.signature(capture_screenshot_to_file).parameters["output_dir"].default is (
        inspect.Parameter.empty
    )


def test_cli_stops_owned_app_with_requested_grace(tmp_path):
    marker = tmp_path / "cli-app-pid"
    script = (
        "from pathlib import Path; import sys; from kwin_mcp import cli; "
        "from kwin_mcp.core import AutomationEngine; from kwin_mcp.session import LiveSession; "
        "engine=AutomationEngine(); "
        f"engine._session=LiveSession('unused','unused',Path({str(tmp_path)!r})); "
        "app=engine._session.launch_app([sys.executable,'-c','import time; time.sleep(60)']); "
        f"Path({str(marker)!r}).write_text(str(app.pid)); "
        "original=cli.KwinMcpShell; "
        "cli.KwinMcpShell=lambda **kw: original(engine=engine,**kw); cli.main()"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        input='session_stop {"grace_seconds":0.05}\nquit\n',
        text=True,
        capture_output=True,
        start_new_session=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Disconnected from live session." in result.stdout
    assert "Traceback" not in result.stderr
    assert not alive(int(marker.read_text()))

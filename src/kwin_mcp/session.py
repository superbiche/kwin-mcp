"""KWin Wayland session management.

Manages the lifecycle of KWin Wayland sessions:
- Virtual sessions: isolated via dbus-run-session + kwin_wayland --virtual
- Live sessions: connecting to an existing KWin compositor (real desktop or container)
"""

from __future__ import annotations

import os
import selectors
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from kwin_mcp.processes import process_running, stop_processes


class SessionType(Enum):
    """Type of KWin session."""

    VIRTUAL = "virtual"
    LIVE = "live"


@dataclass
class SessionConfig:
    """Configuration for an isolated KWin session."""

    socket_name: str = ""
    screen_width: int = 1920
    screen_height: int = 1080
    enable_clipboard: bool = False
    keep_screenshots: bool = False
    isolate_home: bool = False
    keep_home: bool = False
    extra_env: dict[str, str] = field(default_factory=dict)


@dataclass
class AppInfo:
    """Tracking info for a launched application."""

    pid: int
    command: str
    log_path: Path
    process: subprocess.Popen[bytes]


@dataclass
class SessionInfo:
    """Runtime information about a running session."""

    dbus_address: str
    wayland_socket: str
    kwin_pid: int
    screenshot_dir: Path
    runtime_dir: str = ""
    home_dir: Path | None = None
    app_pid: int | None = None
    wrapper_pid: int | None = None
    apps: dict[int, AppInfo] = field(default_factory=dict)
    session_type: SessionType = SessionType.VIRTUAL


class Session:
    """An isolated KWin Wayland session.

    Uses dbus-run-session to create an isolated D-Bus session bus,
    then starts kwin_wayland --virtual inside it. Apps use the selected compositor
    but retain the user's filesystem and network access.
    """

    def __init__(self) -> None:
        self._process: subprocess.Popen[bytes] | None = None
        self._info: SessionInfo | None = None
        self._socket_name: str = ""
        self._app_counter: int = 0
        self._config: SessionConfig | None = None
        self._home_dir: Path | None = None
        self._screenshot_dir: Path | None = None
        self._runtime_dir = ""
        self._owns_socket = False

    @property
    def is_running(self) -> bool:
        if self._process is None:
            return False
        return process_running(self._process)

    @property
    def info(self) -> SessionInfo | None:
        return self._info

    @property
    def wayland_socket(self) -> str:
        return self._socket_name

    def _xdg_isolation_env(self) -> dict[str, str]:
        """Build XDG environment overrides for home directory isolation."""
        if self._home_dir is None:
            return {}
        home = str(self._home_dir)
        return {
            "HOME": home,
            "XDG_CONFIG_HOME": str(self._home_dir / ".config"),
            "XDG_DATA_HOME": str(self._home_dir / ".local" / "share"),
            "XDG_CACHE_HOME": str(self._home_dir / ".cache"),
            "XDG_STATE_HOME": str(self._home_dir / ".local" / "state"),
        }

    def start(self, config: SessionConfig | None = None) -> SessionInfo:
        """Start an isolated KWin Wayland session.

        Returns SessionInfo with connection details.
        """
        if self.is_running:
            msg = "Session is already running"
            raise RuntimeError(msg)

        if config is None:
            config = SessionConfig()

        self.stop()
        self._config = config
        self._socket_name = config.socket_name or f"wayland-mcp-{uuid.uuid4().hex}"
        if Path(self._socket_name).name != self._socket_name or self._socket_name in (".", ".."):
            raise ValueError("socket_name must be a filename, not a path")
        self._runtime_dir = config.extra_env.get(
            "XDG_RUNTIME_DIR", os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        )
        socket_path = Path(self._runtime_dir) / self._socket_name
        if any(Path(f"{socket_path}{suffix}").exists() for suffix in ("", ".lock")):
            raise RuntimeError("Wayland socket already exists; refusing to replace it")
        self._owns_socket = True

        try:
            if config.isolate_home:
                self._home_dir = Path(tempfile.mkdtemp(prefix="kwin-mcp-home-"))
                for subdir in (".config", ".local/share", ".local/state", ".cache", ".screenshots"):
                    (self._home_dir / subdir).mkdir(parents=True, exist_ok=True)
            screenshot_dir = (
                self._home_dir / ".screenshots"
                if self._home_dir is not None
                else Path(tempfile.mkdtemp(prefix="kwin-mcp-screenshots-"))
            )
            self._screenshot_dir = screenshot_dir
            self._info = SessionInfo(
                dbus_address="",
                wayland_socket=self._socket_name,
                kwin_pid=0,
                screenshot_dir=screenshot_dir,
                home_dir=self._home_dir,
                runtime_dir=self._runtime_dir,
            )
            # Diagnostics go to a file: an unread stderr pipe can deadlock startup.
            with (screenshot_dir / "session.log").open("ab") as log_file:
                self._process = subprocess.Popen(
                    ["dbus-run-session", "bash", "-c", self._build_wrapper_script(config)],
                    stdout=subprocess.PIPE,
                    stderr=log_file,
                    env=self._build_env(config),
                    start_new_session=True,
                )
            self._info.kwin_pid = self._process.pid
            self._info.wrapper_pid = self._process.pid
            self._info.dbus_address = self._read_startup(timeout=10.0)
            if not socket_path.exists():
                raise RuntimeError("KWin reported ready without creating its Wayland socket")
            return self._info
        except BaseException:
            self.stop()
            raise

    def _read_startup(self, timeout: float) -> str:
        """Read the startup handshake with a deadline, including partial lines."""
        assert self._process is not None and self._process.stdout is not None
        deadline = time.monotonic() + timeout
        pending = b""
        address = ""
        with selectors.DefaultSelector() as selector:
            selector.register(self._process.stdout, selectors.EVENT_READ)
            while time.monotonic() < deadline:
                if not selector.select(max(0, deadline - time.monotonic())):
                    break
                data = os.read(self._process.stdout.fileno(), 4096)
                if not data:
                    break
                pending += data
                if len(pending) > 65536:
                    raise RuntimeError("Session startup output exceeded handshake limit")
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    if line.startswith(b"DBUS_SESSION_BUS_ADDRESS="):
                        address = line.split(b"=", 1)[1].decode()
                    elif line == b"READY" and address:
                        return address
        raise RuntimeError("Session setup failed or timed out before READY")

    def launch_app(self, command: list[str], extra_env: dict[str, str] | None = None) -> AppInfo:
        """Launch an application inside the isolated session.

        Returns AppInfo with pid, command, and log_path.
        """
        if not self.is_running or self._info is None:
            msg = "Session is not running"
            raise RuntimeError(msg)

        env: dict[str, str] = {
            **os.environ,
            "WAYLAND_DISPLAY": self._socket_name,
            "QT_QPA_PLATFORM": "wayland",
            "QT_LINUX_ACCESSIBILITY_ALWAYS_ON": "1",
            "QT_ACCESSIBILITY": "1",
        }
        env.update(self._xdg_isolation_env())
        if extra_env:
            env.update(extra_env)
        if self._info.dbus_address:
            env["DBUS_SESSION_BUS_ADDRESS"] = self._info.dbus_address

        env.pop("DISPLAY", None)
        env.pop("WAYLAND_SOCKET", None)
        env["WAYLAND_DISPLAY"] = self._socket_name
        env["XDG_RUNTIME_DIR"] = self._runtime_dir

        # Create log file for stdout/stderr capture
        if not command:
            raise ValueError("Application command must not be empty")
        app_name = Path(command[0]).stem
        self._app_counter += 1
        log_path = self._info.screenshot_dir / f"app_{app_name}_{self._app_counter}.log"
        with log_path.open("ab") as log_file:
            proc = subprocess.Popen(
                command,
                env=env,
                stdout=log_file,
                stderr=log_file,
                start_new_session=True,
            )

        app_info = AppInfo(
            pid=proc.pid,
            command=" ".join(command),
            log_path=log_path,
            process=proc,
        )
        self._info.app_pid = proc.pid
        self._info.apps[proc.pid] = app_info
        return app_info

    def read_app_log(self, pid: int, last_n_lines: int = 50) -> str:
        """Read the log output of a launched app.

        Args:
            pid: PID of the app (from launch_app).
            last_n_lines: Number of trailing lines to return (0 = all).

        Returns:
            The app's stdout/stderr output.
        """
        if self._info is None:
            msg = "Session is not running"
            raise RuntimeError(msg)

        app = self._info.apps.get(pid)
        if app is None:
            available = list(self._info.apps.keys())
            msg = f"No app with PID {pid}. Available PIDs: {available}"
            raise ValueError(msg)

        if not app.log_path.exists():
            return "(no log output yet)"

        text = app.log_path.read_text(errors="replace")
        if last_n_lines > 0:
            lines = text.splitlines()
            text = "\n".join(lines[-last_n_lines:])
        return text or "(no log output yet)"

    def stop(self, *, grace_seconds: float = 2.0) -> None:
        """Stop the isolated session and clean up all processes."""
        processes = [app.process for app in self._info.apps.values()] if self._info else []
        if self._process is not None:
            processes.append(self._process)
        # Keep ownership and files if termination fails so stop() can retry.
        # Unlinking a live compositor's socket would hide a still-running session.
        stop_processes(processes, timeout=grace_seconds)
        if self._process is not None and self._process.stdout is not None:
            self._process.stdout.close()

        # Clean up home directory and/or screenshot directory
        if self._home_dir is not None:
            keep_home = self._config is not None and self._config.keep_home
            keep_screenshots = self._config is not None and self._config.keep_screenshots
            if not keep_home:
                # Remove entire home dir (includes screenshots)
                if self._home_dir.exists():
                    shutil.rmtree(self._home_dir)
            elif not keep_screenshots:
                # Keep home but remove screenshots subdirectory
                screenshots = self._home_dir / ".screenshots"
                if screenshots.exists():
                    shutil.rmtree(screenshots)
        else:
            # No isolated home — use original screenshot cleanup logic
            keep = self._config is not None and self._config.keep_screenshots
            if not keep and self._screenshot_dir and self._screenshot_dir.exists():
                shutil.rmtree(self._screenshot_dir)

        if self._owns_socket:
            for suffix in ("", ".lock"):
                path = Path(self._runtime_dir) / f"{self._socket_name}{suffix}"
                path.unlink(missing_ok=True)
            self._owns_socket = False

        self._process = None
        self._info = None
        self._home_dir = None
        self._screenshot_dir = None

    def _build_wrapper_script(self, config: SessionConfig) -> str:
        """Build the bash script that runs inside dbus-run-session."""
        launcher = shutil.which("at-spi-bus-launcher") or next(
            (
                path
                for path in ("/usr/libexec/at-spi-bus-launcher", "/usr/lib/at-spi-bus-launcher")
                if os.access(path, os.X_OK)
            ),
            None,
        )
        if launcher is None:
            raise RuntimeError("at-spi-bus-launcher not found; install at-spi2-core")
        socket = shlex.quote(self._socket_name)
        return f"""\
echo "DBUS_SESSION_BUS_ADDRESS=$DBUS_SESSION_BUS_ADDRESS"

# Ensure all child processes are cleaned up on exit
cleanup() {{
    kill $KWIN_PID $AT_SPI_PID 2>/dev/null
    wait $KWIN_PID $AT_SPI_PID 2>/dev/null
}}
trap cleanup EXIT TERM INT HUP

# Start the AT-SPI accessibility bus.
# ATSPI_DBUS_IMPLEMENTATION is set in _build_env() to force dbus-daemon
# instead of dbus-broker (which reuses the host's AT-SPI bus).
{shlex.quote(launcher)} --launch-immediately >&2 &
AT_SPI_PID=$!
sleep 0.2

# Pre-set D-Bus activation environment BEFORE starting KWin.
# When KWin triggers portal auto-activation, portal-kde will get
# WAYLAND_DISPLAY pointing to our isolated compositor socket.
# The socket doesn't exist yet, but portal-kde will be activated
# only after KWin creates it.
dbus-update-activation-environment WAYLAND_DISPLAY={socket} QT_QPA_PLATFORM=wayland >&2

# Start KWin WITHOUT WAYLAND_DISPLAY to prevent nesting attempt.
# KWin with --virtual creates its own compositor, it must not try
# to connect to another compositor as a client.
# Explicitly pass KWIN_ permission env vars to ensure they reach the
# KWin process (environment inheritance through dbus-run-session can be unreliable).
env -u WAYLAND_DISPLAY -u QT_QPA_PLATFORM \
    KWIN_WAYLAND_NO_PERMISSION_CHECKS=1 \
    KWIN_SCREENSHOT_NO_PERMISSION_CHECKS=1 \
    kwin_wayland --virtual --no-lockscreen \
    --width {config.screen_width} --height {config.screen_height} \
    --socket {socket} >&2 &
KWIN_PID=$!

# Wait for KWin socket to appear
for attempt in {{1..80}}; do
    [ -e "$XDG_RUNTIME_DIR"/{socket} ] && break
    kill -0 "$KWIN_PID" 2>/dev/null || exit 1
    sleep 0.1
done
[ -e "$XDG_RUNTIME_DIR"/{socket} ] || exit 1
sleep 0.3

# Signal parent that setup is complete
echo "READY"

# Block until kwin exits
wait $KWIN_PID
"""

    def _build_env(self, config: SessionConfig) -> dict[str, str]:
        """Build the environment for the isolated session."""
        env: dict[str, str] = {
            **os.environ,
            "KDE_FULL_SESSION": "true",
            "KDE_SESSION_VERSION": "6",
            "XDG_SESSION_TYPE": "wayland",
            "XDG_CURRENT_DESKTOP": "KDE",
            "QT_LINUX_ACCESSIBILITY_ALWAYS_ON": "1",
            "QT_ACCESSIBILITY": "1",
            # Force dbus-daemon for the AT-SPI bus instead of dbus-broker.
            # dbus-broker with --scope=user reuses the host's existing AT-SPI bus,
            # breaking accessibility isolation. Verified as REQUIRED.
            "ATSPI_DBUS_IMPLEMENTATION": "dbus-daemon",
            # Allow direct D-Bus screenshot capture without portal authorization.
            # Safe in isolated virtual sessions where there is no user desktop to protect.
            "KWIN_SCREENSHOT_NO_PERMISSION_CHECKS": "1",
            # Allow clients to bind restricted Wayland protocols (e.g. plasma_window_management).
            # Safe in isolated virtual sessions where there is no user desktop to protect.
            "KWIN_WAYLAND_NO_PERMISSION_CHECKS": "1",
        }
        # Remove host display references to avoid kwin connecting to host
        env.pop("WAYLAND_DISPLAY", None)
        env.pop("DISPLAY", None)
        env.pop("WAYLAND_SOCKET", None)

        env.update(self._xdg_isolation_env())
        env.update(config.extra_env)
        env["XDG_RUNTIME_DIR"] = self._runtime_dir
        env.pop("DISPLAY", None)
        env.pop("WAYLAND_SOCKET", None)
        env.pop("WAYLAND_DISPLAY", None)
        return env

    def __enter__(self) -> Session:
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()


class LiveSession:
    """Connection to an existing (non-virtual) KWin session.

    Attaches to a KWin compositor that is already running, such as
    the user's real desktop or a KWin instance inside a container.
    Does NOT manage the compositor lifecycle — stop() only disconnects.
    """

    def __init__(
        self,
        dbus_address: str,
        wayland_socket: str,
        screenshot_dir: Path | None = None,
    ) -> None:
        # A caller-supplied directory is borrowed, never recursively removed.
        # Only directories allocated here are owned by the session.
        self._owned_screenshot_dir: Path | None = None
        if screenshot_dir is None:
            screenshot_dir = Path(tempfile.mkdtemp(prefix="kwin-mcp-screenshots-"))
            self._owned_screenshot_dir = screenshot_dir
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        socket_path = Path(wayland_socket)
        if socket_path.is_absolute():
            runtime_dir = str(socket_path.parent)
        else:
            socket_path = Path(runtime_dir) / socket_path
        self._info = SessionInfo(
            dbus_address=dbus_address,
            wayland_socket=str(socket_path),
            runtime_dir=runtime_dir,
            kwin_pid=0,
            screenshot_dir=screenshot_dir,
            session_type=SessionType.LIVE,
        )
        self._running = True
        self._app_counter: int = 0
        self._keep_screenshots: bool = False

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def info(self) -> SessionInfo | None:
        return self._info if self._running else None

    @property
    def wayland_socket(self) -> str:
        return self._info.wayland_socket

    def launch_app(self, command: list[str], extra_env: dict[str, str] | None = None) -> AppInfo:
        """Launch an application in the live session.

        Returns AppInfo with pid, command, and log_path.
        """
        if not self._running:
            msg = "Session is not running"
            raise RuntimeError(msg)

        env: dict[str, str] = {
            **os.environ,
            "WAYLAND_DISPLAY": self._info.wayland_socket,
            "QT_QPA_PLATFORM": "wayland",
            "QT_LINUX_ACCESSIBILITY_ALWAYS_ON": "1",
            "QT_ACCESSIBILITY": "1",
        }
        if self._info.dbus_address:
            env["DBUS_SESSION_BUS_ADDRESS"] = self._info.dbus_address
        if extra_env:
            env.update(extra_env)
        env.pop("DISPLAY", None)
        env.pop("WAYLAND_SOCKET", None)
        env["WAYLAND_DISPLAY"] = self._info.wayland_socket
        env["XDG_RUNTIME_DIR"] = self._info.runtime_dir
        env["DBUS_SESSION_BUS_ADDRESS"] = self._info.dbus_address

        if not command:
            raise ValueError("Application command must not be empty")
        app_name = Path(command[0]).stem
        self._app_counter += 1
        log_path = self._info.screenshot_dir / f"app_{app_name}_{self._app_counter}.log"
        with log_path.open("ab") as log_file:
            proc = subprocess.Popen(
                command,
                env=env,
                stdout=log_file,
                stderr=log_file,
                start_new_session=True,
            )

        app_info = AppInfo(
            pid=proc.pid,
            command=" ".join(command),
            log_path=log_path,
            process=proc,
        )
        self._info.app_pid = proc.pid
        self._info.apps[proc.pid] = app_info
        return app_info

    def read_app_log(self, pid: int, last_n_lines: int = 50) -> str:
        """Read the log output of a launched app."""
        app = self._info.apps.get(pid)
        if app is None:
            available = list(self._info.apps.keys())
            msg = f"No app with PID {pid}. Available PIDs: {available}"
            raise ValueError(msg)

        if not app.log_path.exists():
            return "(no log output yet)"

        text = app.log_path.read_text(errors="replace")
        if last_n_lines > 0:
            lines = text.splitlines()
            text = "\n".join(lines[-last_n_lines:])
        return text or "(no log output yet)"

    def stop(self, *, keep_screenshots: bool = False, grace_seconds: float = 2.0) -> None:
        """Disconnect from the live session.

        Only cleans up screenshot directory. Does NOT kill KWin or any apps
        that were already running before the connection.
        """
        if not self._running:
            return
        stop_processes([app.process for app in self._info.apps.values()], timeout=grace_seconds)
        self._info.apps.clear()

        if not keep_screenshots and self._owned_screenshot_dir is not None:
            if self._owned_screenshot_dir.exists():
                shutil.rmtree(self._owned_screenshot_dir)
            self._owned_screenshot_dir = None
        self._running = False

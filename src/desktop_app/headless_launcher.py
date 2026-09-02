"""
🖥️ Jarvis Headless Launcher

Entry point for users who want the control centre (browser dashboard) and
nothing else: no tray icon, no chat window, no face widget, no settings GUI.
Shows the splash screen while the same readiness checks `desktop_app.app.main`
runs (single-instance lock, crash bookkeeping, Ollama autostart, unsupported
model warning), then starts the daemon as a plain subprocess and waits for the
control centre to answer before closing the splash. The control centre opens
the browser itself once `webui_open_browser` is on (see
`jarvis/webui/webui.spec.md`); this launcher's job ends at the splash.

See `headless_launcher.spec.md`.
"""

from __future__ import annotations

import atexit
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Optional

from desktop_app.app import (
    OllamaRuntimeOwnership,
    _ollama_runtime_flags,
    _run_setup_wizard,
    _stop_owned_ollama_runtime,
    acquire_single_instance_lock,
    check_model_support,
    check_previous_crash,
    get_existing_instance_pid,
    kill_existing_instance,
    mark_session_clean_exit,
    mark_session_started,
    setup_crash_logging,
    show_crash_report_dialog,
    show_instance_conflict_dialog,
    show_unsupported_model_dialog,
)
from desktop_app.splash_screen import SplashScreen


def _control_centre_url(cfg) -> str:
    host = getattr(cfg, "webui_bind_host", "") or "127.0.0.1"
    if host == "0.0.0.0":
        host = "127.0.0.1"
    port = getattr(cfg, "webui_port", 0) or 5055
    return f"http://{host}:{port}"


def _control_centre_is_up(url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{url}/api/health", timeout=1.5) as resp:
            return resp.status == 200
    except Exception:
        return False


def _wait_for_control_centre(url: str, splash: SplashScreen, app, timeout_sec: float = 45.0) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if _control_centre_is_up(url):
            return True
        splash.set_status("Waiting for the control centre...")
        app.processEvents()
        time.sleep(0.5)
    return False


def _start_ollama(splash: SplashScreen, app) -> OllamaRuntimeOwnership:
    from desktop_app.setup_wizard import check_ollama_cli, check_ollama_server

    ownership = OllamaRuntimeOwnership()
    splash.set_status("Checking Ollama...")
    app.processEvents()
    is_running, _ = check_ollama_server()
    if is_running:
        return ownership

    splash.set_status("Starting Ollama...")
    app.processEvents()
    cli_installed, ollama_path = check_ollama_cli()
    if not cli_installed:
        ollama_path = "ollama"

    try:
        if sys.platform == "win32":
            proc = subprocess.Popen(
                [ollama_path, "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
            )
        else:
            proc = subprocess.Popen(
                [ollama_path, "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        ownership = OllamaRuntimeOwnership(started_by_jarvis=True, launch_method="serve", process=proc)
    except Exception as exc:
        splash.set_status(f"Could not start Ollama: {exc}")
        app.processEvents()
        time.sleep(2)
        return ownership

    for waited in range(30):
        time.sleep(0.5)
        app.processEvents()
        is_running, _ = check_ollama_server()
        if is_running:
            break
        splash.set_status(f"Waiting for Ollama... ({waited // 2}s)")
        app.processEvents()

    return ownership


def main() -> int:
    if sys.platform == "win32" and not getattr(sys, "frozen", False):
        from jarvis.console import force_utf8_console

        force_utf8_console()

    from PyQt6.QtWidgets import QApplication

    if not acquire_single_instance_lock():
        temp_app = QApplication(sys.argv)
        if not show_instance_conflict_dialog():
            return 0
        existing_pid = get_existing_instance_pid()
        if not existing_pid or not kill_existing_instance(existing_pid):
            return 1
        time.sleep(0.5)
        if not acquire_single_instance_lock():
            return 1
        temp_app.quit()
        del temp_app

    previous_crash = check_previous_crash()
    crash_log_file = setup_crash_logging()
    mark_session_started()
    atexit.register(mark_session_clean_exit)

    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    if previous_crash:
        show_crash_report_dialog(previous_crash)

    splash = SplashScreen()
    splash.show()
    splash.set_status("Initialising...")
    app.processEvents()

    from jarvis.config import load_settings

    cfg = load_settings()
    ollama_needed, chat_on_ollama = _ollama_runtime_flags(cfg)

    ollama_ownership = OllamaRuntimeOwnership()
    if ollama_needed:
        ollama_ownership = _start_ollama(splash, app)

    if chat_on_ollama:
        splash.set_status("Checking model compatibility...")
        app.processEvents()
        unsupported_model = check_model_support()
        if unsupported_model:
            splash.hide()
            if show_unsupported_model_dialog(unsupported_model):
                if not _run_setup_wizard():
                    return 0
            splash.show()
            app.processEvents()

    splash.set_status("Starting Jarvis...")
    app.processEvents()

    python_exe = sys.executable
    env = os.environ.copy()
    src_path = Path(__file__).parent.parent
    env["PYTHONPATH"] = (
        f"{src_path}{os.pathsep}{env['PYTHONPATH']}" if "PYTHONPATH" in env else str(src_path)
    )
    env["PYTHONIOENCODING"] = "utf-8"

    from desktop_app.paths import get_log_dir

    log_path = get_log_dir() / "headless_daemon.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "w", encoding="utf-8", errors="replace")

    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    daemon_process = subprocess.Popen(
        [python_exe, "-X", "utf8", "-m", "jarvis.main"],
        stdin=subprocess.PIPE,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        env=env,
        creationflags=creationflags,
    )

    _shutdown_done = False

    def _shutdown() -> None:
        nonlocal _shutdown_done
        if _shutdown_done:
            return
        _shutdown_done = True
        try:
            if daemon_process.poll() is None and daemon_process.stdin:
                daemon_process.stdin.close()
                daemon_process.wait(timeout=60)
        except Exception:
            pass
        finally:
            if daemon_process.poll() is None:
                daemon_process.terminate()
            _stop_owned_ollama_runtime(ollama_ownership)
            log_file.close()

    atexit.register(_shutdown)

    def _signal_handler(signum, frame):
        _shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    url = _control_centre_url(cfg)
    ready = _wait_for_control_centre(url, splash, app)
    splash.set_status("Ready!" if ready else "Still starting - the dashboard will load once it's up")
    app.processEvents()
    time.sleep(0.4)
    splash.close_splash()

    if crash_log_file:
        print(f"Crash logs available at: {crash_log_file}", flush=True)

    daemon_process.wait()
    return daemon_process.returncode or 0


if __name__ == "__main__":
    sys.exit(main())

# Headless Launcher Specification

`desktop_app.headless_launcher` is an alternative entry point to
`desktop_app.app` for users who want the control centre and nothing else: no
tray icon, no chat window, no face widget, no settings GUI. It is a thin
wrapper, not a rewrite: it reuses the exact readiness-check functions
`desktop_app.app.main` calls (single-instance lock, crash bookkeeping, Ollama
autostart, unsupported-model warning) and only replaces the final step, where
`main()` builds `JarvisSystemTray`, with starting the daemon as a plain
subprocess.

## Startup flow

1. Single-instance lock, crash detection/logging, session bookkeeping —
   identical to `desktop_app.app.main`, same lock file, so a tray launch and
   a headless launch can never run at once.
2. Splash screen (`desktop_app.splash_screen.SplashScreen`), shown for the
   whole flow below.
3. Ollama autostart when the configured provider needs it
   (`_ollama_runtime_flags`), same 30-attempt/0.5s poll as the tray path, but
   without the tray's setup-wizard fallback on timeout: a failure to reach
   Ollama is reported on the splash and startup continues regardless, since
   there is no tray to hand off to for manual diagnosis.
4. Unsupported chat model warning (`check_model_support` /
   `show_unsupported_model_dialog`) when chat runs on Ollama, identical to
   the tray path including the setup wizard offer.
5. The daemon (`python -m jarvis.main`) as a subprocess, stdin held open as a
   pipe so the daemon's stdin-EOF shutdown watcher does not fire immediately
   (see `jarvis/daemon.py`'s stdin monitor), stdout/stderr redirected to
   `<log dir>/headless_daemon.log` since there is no `LogViewerWindow` to
   stream them to.
6. Polls `GET {control centre URL}/api/health` until it answers or a 45s
   timeout passes, updating the splash status text throughout, then closes
   the splash. The control centre opens the browser itself once
   `webui_open_browser` is on (`jarvis/webui/webui.spec.md`); this launcher
   never calls a browser-opening API itself, so there is exactly one place
   that owns that decision.
7. Blocks on the daemon subprocess for the remainder of the process
   lifetime, keeping the single-instance lock held for as long as the
   daemon runs.

## Shutdown

`SIGINT`/`SIGTERM` and normal interpreter exit (`atexit`) all reach the same
`_shutdown()` path: close the daemon's stdin (triggers the same graceful
stop the desktop tray's subprocess mode uses), wait up to 60s, then
`terminate()` if it has not exited, then release any Ollama runtime this
session started (`_stop_owned_ollama_runtime`, a no-op when Ollama was
already running before launch).

There is no tray icon and no window, so there is no UI-driven stop control.
Closing the process (killing it from Task Manager, or Ctrl+C on a console
that owns it) is the only way to stop a headless session.

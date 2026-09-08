"""Opt-in registry acceptance against explicit configuration and owned fixtures.

The guard deliberately precedes imports that can initialise application state.
Only safe metadata is printed; screen text and note contents remain in memory.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import sys
from dataclasses import replace


def validate_inputs(config_path: str, workspace_path: str):
    config, workspace = Path(config_path), Path(workspace_path)
    if not config.is_absolute() or not config.is_file():
        raise ValueError("an explicit existing absolute config file is required")
    raw = config.read_bytes()
    if raw.startswith((b'\xef\xbb\xbf', b'\xff\xfe', b'\xfe\xff')):
        raise ValueError("BOM configuration is refused")
    try:
        settings = json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeError) as exc:
        raise ValueError("invalid configuration JSON") from exc
    if not isinstance(settings, dict):
        raise ValueError("configuration must be an object")
    if not workspace.is_absolute() or not workspace.is_dir():
        raise ValueError("an existing absolute workspace is required")
    workspace = workspace.resolve()
    roots = [Path(r'C:\Users\User\SynologyDrive').resolve(), Path(r'\\DS723plus\home').resolve()]
    if not any(workspace == root or root in workspace.parents for root in roots):
        raise ValueError("acceptance workspace must be on NAS or Synology Drive")
    return settings, workspace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--workspace', required=True)
    parser.add_argument('--check', choices=['ocr', 'files', 'nutrition', 'interactions', 'desktop', 'denials', 'vault', 'read-only', 'codex-text', 'claude-text'], required=True)
    parser.add_argument('--vault')
    args = parser.parse_args()
    if os.environ.get('JARVIS_LIVE_ACCEPTANCE') != '1':
        parser.error('JARVIS_LIVE_ACCEPTANCE=1 is required')
    try:
        _, workspace = validate_inputs(args.config, args.workspace)
    except ValueError as exc:
        parser.error(str(exc))
    if args.check == 'vault':
        vault = Path(args.vault or '')
        if not vault.is_absolute() or not (vault / '.obsidian').is_dir():
            parser.error('vault check requires an explicit actual Obsidian vault root')
    # No application imports, directories or output files before validation.
    with tempfile.TemporaryDirectory(prefix='registry-acceptance-', dir=workspace) as owned:
        config = Path(owned) / 'config.json'
        config.write_text(json.dumps({'_config_version': 7, 'voice_debug': False,
                                     'db_path': str(Path(owned) / 'nutrition.sqlite3')}), encoding='utf-8')
        os.environ['JARVIS_CONFIG_PATH'] = str(config)
        os.environ['JARVIS_LLM_ROUTE_STATE_PATH'] = str(Path(owned) / 'routes.json')
        os.environ['TEMP'] = os.environ['TMP'] = owned
        tempfile.tempdir = owned
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import QApplication, QLabel, QDialog
        from PyQt6.QtTest import QTest
        from jarvis.config import load_settings
        from jarvis.security.gate import SecurityGate
        from jarvis.tools.registry import run_tool_with_retries
        app = QApplication([])
        app.setQuitOnLastWindowClosed(False)
        cfg = load_settings()
        rows = []

        def invoke(name, arguments, db=None):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                return run_tool_with_retries(db, cfg, name, arguments, '', '', '', max_retries=1)

        def record(name, good, **metadata):
            row = {'tool': name, 'registry': True, 'status': 'passed' if good else 'failed', **metadata}
            rows.append(row)
            print(json.dumps(row), flush=True)

        def finish():
            (workspace / ('live-tool-' + args.check + '.json')).write_text(json.dumps(rows, indent=2), encoding='utf-8')
            return 0 if all(row['status'] in ('passed', 'expected_denial') for row in rows) else 1

        if args.check in ('codex-text', 'claude-text'):
            from jarvis.llm.codex_subscription import CodexSubscriptionBackend
            from jarvis.reply.engine import _text_tool_call_guidance, _extract_text_tool_call
            gate = SecurityGate(level='critical', channels={}, confirm_channels=[])
            backend = None
            model = 'gpt-5.6-sol'
            previous_claude_dir = os.environ.get('CLAUDE_CONFIG_DIR')
            try:
                if args.check == 'claude-text':
                    from jarvis.llm.claude_subscription import ClaudeSubscriptionBackend
                    import shutil
                    credentials = Path.home() / '.claude' / '.credentials.json'
                    auth_dir = Path(owned) / 'claude-auth'
                    auth_dir.mkdir()
                    shutil.copyfile(credentials, auth_dir / '.credentials.json')
                    os.environ['CLAUDE_CONFIG_DIR'] = str(auth_dir)
                    model = 'claude-sonnet-5'
                    backend = ClaudeSubscriptionBackend()
                else:
                    backend = CodexSubscriptionBackend()
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    text = backend.direct(model,
                        'This is a synthetic tool protocol test. getTime takes an empty JSON object. '
                        + _text_tool_call_guidance(['getTime']),
                        'Call getTime now, using the specified text tool format. Do not answer with a time.',
                        timeout_sec=60)
                    name, arguments, _ = _extract_text_tool_call(text or '', {'getTime'})
                parsed = name == 'getTime' and isinstance(arguments, dict)
                result = invoke(name, arguments) if parsed else None
                record(args.check + '-parser-dispatch', parsed and result is not None and result.success,
                       model=model, parsed=parsed)
            except Exception as exc:
                record(args.check + '-parser-dispatch', False, error_class=type(exc).__name__)
            finally:
                if backend is not None:
                    close = getattr(backend, 'close', None)
                    if close:
                        close()
                if previous_claude_dir is None:
                    os.environ.pop('CLAUDE_CONFIG_DIR', None)
                else:
                    os.environ['CLAUDE_CONFIG_DIR'] = previous_claude_dir
                SecurityGate.reset_instance()
            return finish()

        if args.check == 'read-only':
            from jarvis.memory.db import Database
            from jarvis.tools.registry import configure_system_management_tool, get_cached_mcp_tools, reconfigure_mcp_tools
            from jarvis.tools.external.mcp_runtime import shutdown_runtime
            fixture = Path(__file__).resolve().parents[1] / 'tests/fixtures/mcp_disposable_server.py'
            document = json.loads(config.read_text(encoding='utf-8'))
            document['mcps'] = {'acceptance': {'transport': 'stdio', 'command': sys.executable,
                'args': ['-B', str(fixture), str(Path(owned) / 'starts.txt')], 'timeout_sec': 5}}
            config.write_text(json.dumps(document), encoding='utf-8')
            cfg = replace(load_settings(), system_management_enabled=True, tool_selection_strategy='keyword')
            configure_system_management_tool(cfg)
            gate = SecurityGate(level='critical', channels={}, confirm_channels=[])
            db = Database(str(Path(owned) / 'nutrition.sqlite3'))
            try:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    reconfigure_mcp_tools(cfg.mcps, verbose=False)
                cases = [
                    ('getTime', {}, lambda r: bool(r.reply_text)),
                    ('getWeather', {'location': 'Berlin'}, lambda r: 'Berlin' in (r.reply_text or '')),
                    ('webSearch', {'search_query': 'Python programming language'}, lambda r: 'python' in (r.reply_text or '').lower()),
                    ('fetchWebPage', {'url': 'https://example.com'}, lambda r: 'Example Domain' in (r.reply_text or '')),
                    ('systemManager', {'operation': 'readFile', 'path': str(config)}, lambda r: '_config_version' in (r.reply_text or '')),
                    ('refreshMCPTools', {}, lambda r: len(get_cached_mcp_tools()) == 4),
                    ('toolSearchTool', {'query': 'read a local file'}, lambda r: 'localFiles' in (r.reply_text or '')),
                    ('getExamCountdown', {}, lambda r: bool(r.reply_text)),
                    ('memoryProvenance', {}, lambda r: bool(r.reply_text)),
                    ('stop', {}, lambda r: True),
                ]
                for name, arguments, verify in cases:
                    result = invoke(name, arguments, db)
                    record(name, result.success and verify(result), error_code=str(result.error_code))
            finally:
                db.close()
                shutdown_runtime()
                SecurityGate.reset_instance()
            return finish()

        if args.check == 'vault':
            from jarvis.tools.registry import configure_vault_search_tool
            cfg = replace(cfg, obsidian_vault_path=str(vault), obsidian_read_enabled=True)
            configure_vault_search_tool(cfg)
            gate = SecurityGate(level='critical', channels={}, confirm_channels=[])
            try:
                matched = False
                for _ in range(5):
                    result = invoke('vaultSearch', {'query': 'JARVIS Backend Konsolidierung Uebergabe', 'limit': 10})
                    matched = result.success and '2026-09-07 JARVIS Backend Konsolidierung Uebergabe' in result.reply_text
                    if matched:
                        break
                record('vaultSearch', matched, actual_vault_root=True,
                       index_status=(result.metadata or {}).get('vault_index', {}))
            finally:
                SecurityGate.reset_instance()
            return finish()

        if args.check in ('files', 'nutrition', 'interactions', 'desktop'):
            from desktop_app.security_confirmation import SecurityConfirmationDialog
            from jarvis.security.desktop_confirm import DesktopConfirm
            decisions = []

            def requester(name, arguments, timeout):
                dialog = SecurityConfirmationDialog(name, arguments, timeout)
                clicked = []
                dialog.deny_button.clicked.connect(lambda: clicked.append(False))
                dialog.approve_button.clicked.connect(lambda: clicked.append(True))
                approved = dialog.exec() == QDialog.DialogCode.Accepted
                decisions.append(clicked[-1] if clicked else None)
                return approved

            gate = SecurityGate(level='critical', channels={'desktop': DesktopConfirm(60, requester=requester)},
                                confirm_channels=['desktop'])
            try:
                if args.check == 'files':
                    target = Path(owned) / 'controlled.txt'
                    token = 'JARVIS controlled file acceptance'
                    result = invoke('localFiles', {'operation': 'write', 'path': str(target), 'content': token})
                    record('localFiles.write', result.success and target.is_file() and target.read_text() == token)
                    result = invoke('localFiles', {'operation': 'read', 'path': str(target)})
                    record('localFiles.read', result.success and result.reply_text == token)
                    result = invoke('localFiles', {'operation': 'delete', 'path': str(target)})
                    record('localFiles.delete', result.success and not target.exists(), explicit_decision=decisions[-1] if decisions else None)
                else:
                    if args.check in ('interactions', 'desktop'):
                        from jarvis.tools.registry import configure_computer_interaction_tools, BUILTIN_TOOLS
                        cfg = replace(cfg, computer_interaction_enabled=True, planner_timeout_sec=60.0,
                                      llm_chat_model='gpt-5.6-sol', llm_routes=[{
                                          'name': 'acceptance-codex', 'provider': 'codex_subscription',
                                          'base_url': 'codex-cli', 'model': 'gpt-5.6-sol',
                                          'tier': 'chat', 'timeout_sec': 60}])
                        configure_computer_interaction_tools(cfg)
                        if args.check == 'desktop':
                            import subprocess
                            title = 'JARVIS Acceptance Editor'
                            helper = Path(owned) / 'editor.py'
                            proof = Path(owned) / 'editor-result.txt'
                            ready = Path(owned) / 'editor-ready.txt'
                            helper.write_text(
                                'import sys\nfrom pathlib import Path\n'
                                'from PyQt6.QtWidgets import QApplication,QLineEdit,QWidget,QVBoxLayout\n'
                                'from PyQt6.QtCore import QTimer\n'
                                'app=QApplication([])\nwindow=QWidget()\nfield=QLineEdit("JARVIS EDITOR INITIAL")\n'
                                'window.setWindowTitle("JARVIS Acceptance Editor")\n'
                                'layout=QVBoxLayout(window)\nlayout.addWidget(field)\n'
                                'field.setAccessibleName("Acceptance text")\nwindow.resize(650,100)\n'
                                'field.textChanged.connect(lambda text: Path(sys.argv[1]).write_text(text,encoding="utf-8"))\n'
                                'window.show()\nQTimer.singleShot(300,lambda: Path(sys.argv[2]).write_text("ready"))\n'
                                'QTimer.singleShot(180000,app.quit)\napp.exec()\n', encoding='utf-8')
                            process = subprocess.Popen([sys.executable, '-B', str(helper), str(proof), str(ready)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=0x08000000 if os.name == 'nt' else 0)
                            try:
                                for _ in range(50):
                                    if ready.is_file() or process.poll() is not None:
                                        break
                                    QTest.qWait(100)
                                result = invoke('openOnComputer', {'target': str(Path(owned))})
                                record('openOnComputer', result.success, controlled_directory=True)
                                result = invoke('desktopInteract', {'application': title,
                                    'task': 'Replace the Acceptance text field with JARVIS EDITOR VERIFIED, then read that field back. Work only in this test window.'})
                                matched = proof.is_file() and proof.read_text(encoding='utf-8') == 'JARVIS EDITOR VERIFIED'
                                record('desktopInteract', result.success and matched, controlled_edit_verified=matched,
                                       error_code=str(result.error_code), explicit_decision=decisions[-1] if decisions else None,
                                       helper_ready=ready.is_file(), helper_exit=process.poll(),
                                       window_not_found='No running window matches' in (result.error_message or ''))
                            finally:
                                process.terminate()
                                try:
                                    process.wait(timeout=5)
                                except subprocess.TimeoutExpired:
                                    process.kill()
                                    process.wait(timeout=5)
                                os.environ.pop('JARVIS_ACCEPTANCE_GROQ_KEY', None)
                            return finish()
                        try:
                            result = invoke('browserInteract', {'task': 'In the isolated browser visit https://example.com, read the page and report its heading. Do not follow links.'})
                            controller = BUILTIN_TOOLS['browserInteract']._controller
                            page = controller._page
                            from jarvis.llm import get_llm_backend
                            record('browserInteract', result.success and page is not None and page.title() == 'Example Domain',
                                   explicit_decision=decisions[-1] if decisions else None,
                                   error_code=str(result.error_code), phase=result.phase, execution_succeeded=result.success,
                                   planner_timeout_sec=cfg.planner_timeout_sec,
                                   provider='codex_subscription',
                                   route_health=get_llm_backend(cfg).health_summary())
                        finally:
                            BUILTIN_TOOLS['browserInteract'].close()
                            os.environ.pop('JARVIS_ACCEPTANCE_GROQ_KEY', None)
                        return finish()
                    from jarvis.llm.probe import load_fcc_values
                    from jarvis.memory.db import Database
                    values = load_fcc_values()
                    key = values.get('GROQ_API_KEY', '')
                    if not key:
                        record('logMeal', False, reason='credential_unavailable')
                        return finish()
                    os.environ['JARVIS_ACCEPTANCE_GROQ_KEY'] = key
                    cfg = replace(cfg, llm_routes=[{'name': 'acceptance-groq', 'provider': 'openai_compatible',
                                       'base_url': 'https://api.groq.com/openai/v1', 'model': 'openai/gpt-oss-20b',
                                       'api_key_env': 'JARVIS_ACCEPTANCE_GROQ_KEY', 'tier': 'chat', 'timeout_sec': 30}],
                                  llm_chat_model='openai/gpt-oss-20b')
                    db = Database(str(Path(owned) / 'nutrition.sqlite3'))
                    try:
                        result = invoke('logMeal', {'meal': '100 grams cooked white rice: 130 kcal, 2.7 g protein, 28 g carbohydrates, 0.3 g fat.'}, db)
                        stored = db.conn.execute('SELECT id FROM meals').fetchall()
                        record('logMeal', result.success and len(stored) == 1, temporary_database=True)
                        result = invoke('fetchMeals', {}, db)
                        record('fetchMeals', result.success and len(stored) == 1)
                        if len(stored) == 1:
                            result = invoke('deleteMeal', {'id': stored[0]['id']}, db)
                            remaining = db.conn.execute('SELECT COUNT(*) FROM meals').fetchone()[0]
                            record('deleteMeal', result.success and remaining == 0,
                                   explicit_decision=decisions[-1] if decisions else None)
                    finally:
                        db.close()
                        os.environ.pop('JARVIS_ACCEPTANCE_GROQ_KEY', None)
            finally:
                SecurityGate.reset_instance()
            return finish()

        if args.check == 'denials':
            from jarvis.tools.registry import configure_system_management_tool, configure_computer_interaction_tools
            cfg = replace(cfg, system_management_enabled=True, computer_interaction_enabled=True)
            configure_system_management_tool(cfg)
            configure_computer_interaction_tools(cfg)
            gate = SecurityGate(level='critical', channels={}, confirm_channels=[])
            try:
                for name, arguments in [
                    ('askCrew', {'agent': 'dev', 'task': 'Synthetic test; no external message authorised'}),
                    ('systemManager', {'operation': 'writeFile', 'path': str(Path(owned) / 'denied.txt'), 'content': 'denied'}),
                    ('localFiles', {'operation': 'write', 'path': str(Path(owned) / 'denied.txt'), 'content': 'denied'}),
                    ('browserInteract', {'operation': 'navigate', 'url': 'https://example.com'}),
                    ('desktopInteract', {'operation': 'click', 'target': 'synthetic target'}),
                ]:
                    result = invoke(name, arguments)
                    denied = not result.success and 'denied by security confirmation' in (result.error_message or '').lower()
                    record(name, denied, policy='critical_without_channel',
                           status='expected_denial' if denied else 'failed')
                record('denied_write_absent', not (Path(owned) / 'denied.txt').exists())
            finally:
                SecurityGate.reset_instance()
            return finish()

        label = QLabel('JARVIS OCR ACCEPTANCE 7319')
        label.setStyleSheet('background: white; color: black; font: 36pt Arial; padding: 50px;')
        label.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setGeometry(app.primaryScreen().availableGeometry())
        label.show()
        label.raise_()
        app.processEvents()
        QTest.qWait(750)
        gate = SecurityGate(level='critical', channels={}, confirm_channels=[])
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                result = run_tool_with_retries(None, cfg, 'screenshot', {}, '', '', '', max_retries=1)
            recognised = ' '.join((result.reply_text or '').split())
            matched = 'JARVIS OCR ACCEPTANCE 7319' in recognised
            row = {'tool': 'screenshot', 'registry': True, 'controlled_text_match': matched,
                   'execution_succeeded': result.success, 'error_code': str(result.error_code),
                   'test_tokens': {token: token in recognised for token in ['JARVIS', 'OCR', 'ACCEPTANCE', '7319']},
                   'status': 'passed' if result.success and matched else 'failed'}
        finally:
            label.close()
            SecurityGate.reset_instance()
    (workspace / 'live-tool-ocr.json').write_text(json.dumps(row, indent=2), encoding='utf-8')
    print(json.dumps(row))
    return 0 if row['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())

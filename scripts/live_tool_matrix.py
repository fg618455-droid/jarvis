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
    parser.add_argument('--check', choices=['ocr'], required=True)
    args = parser.parse_args()
    if os.environ.get('JARVIS_LIVE_ACCEPTANCE') != '1':
        parser.error('JARVIS_LIVE_ACCEPTANCE=1 is required')
    try:
        _, workspace = validate_inputs(args.config, args.workspace)
    except ValueError as exc:
        parser.error(str(exc))
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
        from PyQt6.QtWidgets import QApplication, QLabel
        from PyQt6.QtTest import QTest
        from jarvis.config import load_settings
        from jarvis.security.gate import SecurityGate
        from jarvis.tools.registry import run_tool_with_retries
        app = QApplication([])
        label = QLabel('JARVIS OCR ACCEPTANCE 7319')
        label.setStyleSheet('background: white; color: black; font: 36pt Arial; padding: 50px;')
        label.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setGeometry(app.primaryScreen().availableGeometry())
        label.show()
        label.raise_()
        app.processEvents()
        QTest.qWait(750)
        cfg = load_settings()
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

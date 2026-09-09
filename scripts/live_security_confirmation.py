"""Supervised real desktop confirmation through registry and MCP transport."""
import argparse
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from PyQt6.QtWidgets import QApplication, QDialog
from desktop_app.security_confirmation import SecurityConfirmationDialog
from jarvis.security.desktop_confirm import DesktopConfirm
from jarvis.security.gate import SecurityGate
from jarvis.tools.registry import run_tool_with_retries
from jarvis.tools.external.mcp_runtime import shutdown_runtime

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace', required=True)
    parser.add_argument('--decision', choices=['both', 'deny', 'approve'], default='both')
    args = parser.parse_args()
    if os.environ.get('JARVIS_LIVE_ACCEPTANCE') != '1':
        parser.error('JARVIS_LIVE_ACCEPTANCE=1 is required')
    workspace = Path(args.workspace).resolve()
    if not workspace.is_dir():
        parser.error('workspace must exist')
    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
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
    config = {'transport': 'stdio', 'command': sys.executable,
              'args': ['-B', str(Path(__file__).resolve().parents[1] / 'tests/fixtures/mcp_disposable_server.py'),
                       str(workspace / 'supervised-starts.txt')], 'timeout_sec': 5}
    cfg = SimpleNamespace(mcps={'acceptance': config}, voice_debug=True)
    rows = []
    try:
        expected_decisions = [False, True] if args.decision == 'both' else [args.decision == 'approve']
        for expected in expected_decisions:
            result = run_tool_with_retries(None, cfg, 'acceptance__echo',
                       {'test': 'Please select Approve' if expected else 'Please select Deny'}, '', '', '', max_retries=2)
            observed = decisions[-1] if decisions else None
            rows.append({'expected_approval': expected, 'explicit_decision': observed,
                         'execution_succeeded': result.success,
                         'status': 'passed' if observed is expected and result.success is expected else 'unavailable' if observed is None else 'failed'})
            print(json.dumps(rows[-1]), flush=True)
    finally:
        shutdown_runtime()
        SecurityGate.reset_instance()
    (workspace / ('security-confirmation-' + args.decision + '.json')).write_text(json.dumps(rows, indent=2), encoding='utf-8')
    return 0 if all(row['status'] == 'passed' for row in rows) else 1

if __name__ == '__main__':
    raise SystemExit(main())

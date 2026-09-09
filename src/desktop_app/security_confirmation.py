"""Qt dialog and thread bridge for security confirmations."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Any

from PyQt6.QtCore import QObject, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from desktop_app.themes import apply_theme


class SecurityConfirmationDialog(QDialog):
    """Show the exact tool and arguments with deny as the safe default."""

    def __init__(
        self,
        action_name: str,
        action_args: dict[str, Any],
        timeout_seconds: int,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("🔐 Jarvis Security Confirmation")
        self.setMinimumSize(620, 430)
        self.setWindowFlag(self.windowFlags() | self.windowFlags().WindowStaysOnTopHint)
        apply_theme(self)

        layout = QVBoxLayout(self)
        title = QLabel("🔐 Jarvis wants to execute a protected tool")
        title.setStyleSheet("font-size: 17px; font-weight: 600;")
        layout.addWidget(title)
        layout.addWidget(QLabel(f"Tool: {action_name}"))

        arguments = QPlainTextEdit()
        arguments.setReadOnly(True)
        arguments.setPlainText(
            json.dumps(action_args, ensure_ascii=False, indent=2, default=str)[:10_000]
        )
        layout.addWidget(arguments, 1)

        layout.addWidget(QLabel(f"The request is denied automatically after {timeout_seconds} seconds."))
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.deny_button = QPushButton("❌ Deny")
        self.deny_button.clicked.connect(self.reject)
        buttons.addWidget(self.deny_button)
        self.approve_button = QPushButton("✅ Approve")
        self.approve_button.clicked.connect(self.accept)
        buttons.addWidget(self.approve_button)
        layout.addLayout(buttons)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.reject)
        self._timer.start(timeout_seconds * 1000)

    def closeEvent(self, event) -> None:
        self.reject()
        event.accept()


@dataclass
class _PendingRequest:
    action_name: str
    action_args: dict[str, Any]
    timeout_seconds: int
    event: threading.Event = field(default_factory=threading.Event)
    approved: bool = False


class SecurityConfirmationBridge(QObject):
    """Move a blocking daemon-thread request onto the Qt application thread."""

    request_received = pyqtSignal(object)

    def __init__(self, parent_widget: QWidget | None = None) -> None:
        super().__init__()
        self._parent_widget = parent_widget
        self.request_received.connect(self._show_request)

    def request(self, action_name: str, action_args: dict[str, Any], timeout_seconds: int) -> bool:
        pending = _PendingRequest(action_name, action_args, timeout_seconds)
        self.request_received.emit(pending)
        pending.event.wait(timeout_seconds + 1)
        return pending.approved

    @pyqtSlot(object)
    def _show_request(self, pending: _PendingRequest) -> None:
        dialog = SecurityConfirmationDialog(
            pending.action_name,
            pending.action_args,
            pending.timeout_seconds,
            self._parent_widget,
        )
        pending.approved = dialog.exec() == QDialog.DialogCode.Accepted
        pending.event.set()

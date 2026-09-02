"""
⚙️ Jarvis Settings Window

Auto-generated settings UI driven by config metadata.
Reads/writes config.json directly and groups settings by category.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QWidget,
    QLabel, QLineEdit, QSpinBox, QDoubleSpinBox, QCheckBox,
    QComboBox, QScrollArea, QGroupBox, QFormLayout, QPushButton,
    QMessageBox, QSizePolicy, QListWidget, QListWidgetItem,
    QStackedWidget, QSplitter, QInputDialog, QFrame,
    QTableWidget, QTableWidgetItem, QHeaderView,
)
from PyQt6.QtCore import Qt, QSize
from PyQt6.QtGui import QFont

from jarvis.config import (
    get_default_config, load_config,
    default_config_path, _save_json, _load_json,
)
from jarvis.config_metadata import (
    CATEGORIES, CATEGORY_DETAILS, FIELD_METADATA, FieldMeta,
    _is_default_value, choices_for,
)
from jarvis.debug import debug_log
from desktop_app.themes import apply_theme
from desktop_app.mcp_catalogue import CATALOGUE, CATALOGUE_BY_NAME, MCPEntry


# ---------------------------------------------------------------------------
# Audio device enumeration
# ---------------------------------------------------------------------------

def get_input_devices() -> List[tuple[str, str]]:
    """Return list of (value, display_name) for available audio input devices.

    Returns [("", "System Default")] if sounddevice is not available.
    """
    devices: List[tuple[str, str]] = [("", "🔧 System Default")]
    try:
        import sounddevice as sd
        for idx, dev in enumerate(sd.query_devices()):
            try:
                max_in = int(dev.get("max_input_channels", 0))
            except Exception:
                max_in = 0
            if max_in > 0:
                name = dev.get("name", f"Device {idx}")
                devices.append((str(idx), f"🎤 {name}"))
    except Exception as e:
        debug_log(f"could not enumerate audio devices: {e}", "settings")
    return devices


# ---------------------------------------------------------------------------
# Widget builders
# ---------------------------------------------------------------------------

class SettingsWindow(QDialog):
    """Auto-generated settings UI driven by config field metadata."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("⚙️ Jarvis Settings")
        self.setMinimumSize(780, 560)
        self.resize(840, 620)
        self._widgets: Dict[str, Any] = {}  # key -> widget
        self._config_path = default_config_path()
        self._current_config = _load_json(self._config_path)
        self._defaults = get_default_config()
        self._merged = {**self._defaults, **self._current_config}

        apply_theme(self)
        self._build_ui()

    # -- UI construction ----------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        # Header
        header = QLabel("⚙️ Settings")
        header.setObjectName("title")
        layout.addWidget(header)

        subtitle = QLabel("Changes are saved to config.json. Restart Jarvis to apply.")
        subtitle.setObjectName("subtitle")
        layout.addWidget(subtitle)

        # Sidebar + content area
        content_layout = QHBoxLayout()
        content_layout.setSpacing(12)

        # Category sidebar
        self._sidebar = QListWidget()
        self._sidebar.setFixedWidth(200)
        self._sidebar.setIconSize(QSize(0, 0))
        content_layout.addWidget(self._sidebar)

        # Stacked content pages
        self._pages = QStackedWidget()
        content_layout.addWidget(self._pages, 1)

        # Build pages from categories
        fields_by_cat: Dict[str, List[FieldMeta]] = {}
        for fm in FIELD_METADATA:
            fields_by_cat.setdefault(fm.category, []).append(fm)

        for cat_key, cat_label in CATEGORIES:
            if cat_key == "mcps":
                page = self._build_mcp_page()
            else:
                cat_fields = fields_by_cat.get(cat_key, [])
                if not cat_fields:
                    continue
                page = self._build_category_tab(cat_key, cat_fields)
            self._pages.addWidget(page)

            item = QListWidgetItem(cat_label)
            item.setSizeHint(QSize(0, 40))
            self._sidebar.addItem(item)

        self._sidebar.currentRowChanged.connect(self._pages.setCurrentIndex)
        self._sidebar.setCurrentRow(0)

        layout.addLayout(content_layout, 1)

        # Button row
        btn_layout = QHBoxLayout()
        btn_layout.setContentsMargins(0, 0, 0, 0)

        reset_btn = QPushButton("↩️ Reset to Defaults")
        reset_btn.setObjectName("danger")
        reset_btn.clicked.connect(self._on_reset)
        btn_layout.addWidget(reset_btn)

        btn_layout.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        save_btn = QPushButton("💾 Save")
        save_btn.setObjectName("primary")
        save_btn.clicked.connect(self._on_save)
        btn_layout.addWidget(save_btn)

        layout.addLayout(btn_layout)

    def _build_category_tab(self, category: str, fields: List[FieldMeta]) -> QWidget:
        """Build a scrollable form for a category's fields."""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        container = QWidget()
        form = QFormLayout(container)
        form.setContentsMargins(16, 16, 16, 16)
        form.setSpacing(14)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        details = CATEGORY_DETAILS.get(category, {})
        if details.get("description"):
            note = QLabel(str(details["description"]))
            note.setWordWrap(True)
            note.setObjectName("subtitle")
            form.addRow(note)

        active_section = None
        for fm in fields:
            if fm.section and fm.section != active_section:
                active_section = fm.section
                section = QLabel(fm.section)
                section.setObjectName("settingsSection")
                section.setStyleSheet(
                    "font-size: 15px; font-weight: 600; padding-top: 12px;"
                )
                form.addRow(section)
            widget = self._create_widget(fm)
            self._widgets[fm.key] = widget

            # Label with tooltip
            label = QLabel(fm.label)
            label.setToolTip(fm.description)
            label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)

            form.addRow(label, widget)

        # Spacer at bottom
        form.addRow(QLabel(""), QLabel(""))

        scroll.setWidget(container)
        return scroll

    def _create_widget(self, fm: FieldMeta) -> QWidget:
        """Create the appropriate input widget for a field."""
        current = self._merged.get(fm.key)

        if fm.field_type == "bool":
            w = QCheckBox()
            w.setChecked(bool(current))
            w.setToolTip(fm.description)
            return w

        if fm.field_type == "int":
            if fm.nullable:
                return self._create_nullable_int(fm, current)
            w = QSpinBox()
            w.setMinimum(int(fm.min_val) if fm.min_val is not None else -999999)
            w.setMaximum(int(fm.max_val) if fm.max_val is not None else 999999)
            w.setSingleStep(int(fm.step) if fm.step else 1)
            if fm.suffix:
                w.setSuffix(f" {fm.suffix}")
            try:
                w.setValue(int(current) if current is not None else 0)
            except (TypeError, ValueError):
                w.setValue(0)
            w.setToolTip(fm.description)
            return w

        if fm.field_type == "float":
            w = QDoubleSpinBox()
            w.setDecimals(3)
            w.setMinimum(fm.min_val if fm.min_val is not None else -999999.0)
            w.setMaximum(fm.max_val if fm.max_val is not None else 999999.0)
            w.setSingleStep(fm.step if fm.step else 0.1)
            if fm.suffix:
                w.setSuffix(f" {fm.suffix}")
            try:
                w.setValue(float(current) if current is not None else 0.0)
            except (TypeError, ValueError):
                w.setValue(0.0)
            w.setToolTip(fm.description)
            return w

        if fm.field_type == "choice":
            w = QComboBox()
            for val, display in choices_for(fm, current):
                w.addItem(display, val)
            # Set current value
            cur_str = str(current) if current is not None else ""
            idx = w.findData(cur_str)
            if idx >= 0:
                w.setCurrentIndex(idx)
            w.setToolTip(fm.description)
            return w

        if fm.field_type == "device":
            w = QComboBox()
            devices = get_input_devices()
            for val, display in devices:
                w.addItem(display, val)
            cur_str = str(current) if current not in (None, "") else ""
            idx = w.findData(cur_str)
            if idx >= 0:
                w.setCurrentIndex(idx)
            w.setToolTip(fm.description)
            return w

        if fm.field_type == "list":
            return self._create_list_widget(fm, current)

        if fm.field_type == "object_list":
            return self._create_object_list_widget(fm, current)

        if fm.field_type == "password":
            w = QLineEdit()
            w.setEchoMode(QLineEdit.EchoMode.Password)
            w.setText(str(current) if current not in (None, "") else "")
            if fm.nullable:
                w.setPlaceholderText("Leave empty for none")
            w.setToolTip(fm.description)
            return w

        # Default: string field
        w = QLineEdit()
        w.setText(str(current) if current not in (None, "") else "")
        if fm.nullable:
            w.setPlaceholderText("Leave empty for default")
        w.setToolTip(fm.description)
        return w

    def _create_nullable_int(self, fm: FieldMeta, current: Any) -> QWidget:
        """Create a combo + spinbox for an int field that can be None."""
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        check = QCheckBox("Custom")
        spin = QSpinBox()
        spin.setMinimum(int(fm.min_val) if fm.min_val is not None else 0)
        spin.setMaximum(int(fm.max_val) if fm.max_val is not None else 999999)
        spin.setSingleStep(int(fm.step) if fm.step else 1)
        if fm.suffix:
            spin.setSuffix(f" {fm.suffix}")

        has_value = current is not None
        check.setChecked(has_value)
        spin.setEnabled(has_value)
        try:
            spin.setValue(int(current) if has_value else 0)
        except (TypeError, ValueError):
            spin.setValue(0)

        check.toggled.connect(spin.setEnabled)

        layout.addWidget(check)
        layout.addWidget(spin, 1)

        # Store both widgets for value extraction
        container._check = check  # type: ignore[attr-defined]
        container._spin = spin  # type: ignore[attr-defined]
        container.setToolTip(fm.description)
        return container

    def _create_list_widget(self, fm: FieldMeta, current: Any) -> QWidget:
        """Create a list editor with add/remove buttons."""
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        list_w = QListWidget()
        list_w.setMinimumHeight(100)
        list_w.setMaximumHeight(160)
        list_w.setToolTip(fm.description)

        # Populate with current values
        if isinstance(current, list):
            for item in current:
                if isinstance(item, str) and item.strip():
                    list_w.addItem(item.strip())

        layout.addWidget(list_w)

        btn_layout = QHBoxLayout()
        btn_layout.setContentsMargins(0, 0, 0, 0)
        btn_layout.setSpacing(6)

        add_btn = QPushButton("+ Add")
        edit_btn = QPushButton("✏️ Edit")
        remove_btn = QPushButton("− Remove")
        btn_layout.addWidget(add_btn)
        btn_layout.addWidget(edit_btn)
        btn_layout.addWidget(remove_btn)
        btn_layout.addStretch()

        layout.addLayout(btn_layout)

        def _on_add():
            text, ok = QInputDialog.getText(
                self, f"Add {fm.label}",
                "Enter value (e.g. 'wrong -> right'):",
            )
            if ok and text.strip():
                list_w.addItem(text.strip())

        def _on_edit():
            item = list_w.currentItem()
            if item is None:
                return
            text, ok = QInputDialog.getText(
                self, f"Edit {fm.label}",
                "Edit value:",
                text=item.text(),
            )
            if ok and text.strip():
                item.setText(text.strip())

        def _on_remove():
            row = list_w.currentRow()
            if row >= 0:
                list_w.takeItem(row)

        add_btn.clicked.connect(_on_add)
        edit_btn.clicked.connect(_on_edit)
        remove_btn.clicked.connect(_on_remove)

        # Store the list widget for value extraction
        container._list_widget = list_w  # type: ignore[attr-defined]
        return container

    def _create_object_list_widget(self, fm: FieldMeta, current: Any) -> QWidget:
        """Create a row-based editor for a list of structured objects."""
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        table = QTableWidget()
        fields = tuple(fm.item_fields or ())
        table.setColumnCount(len(fields))
        table.setHorizontalHeaderLabels([field.label for field in fields])
        table.setMinimumHeight(210)
        table.setToolTip(fm.description)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setStretchLastSection(True)
        self._populate_object_table(table, fields, current)
        layout.addWidget(table)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        add_btn = QPushButton("+ Add")
        remove_btn = QPushButton("− Remove")
        up_btn = QPushButton("↑ Up")
        down_btn = QPushButton("↓ Down")
        for button in (add_btn, remove_btn, up_btn, down_btn):
            buttons.addWidget(button)
        buttons.addStretch()
        layout.addLayout(buttons)

        def _replace(rows: list[dict[str, Any]], selected: int) -> None:
            self._populate_object_table(table, fields, rows)
            if 0 <= selected < table.rowCount():
                table.selectRow(selected)

        def _add() -> None:
            rows = self._object_table_values(table, fields)
            rows.append({
                field.key: (
                    field.default_value
                    if field.default_value is not None
                    else [] if field.field_type == "list"
                    else False if field.field_type == "bool"
                    else ""
                )
                for field in fields
            })
            _replace(rows, len(rows) - 1)

        def _remove() -> None:
            row = table.currentRow()
            if row < 0:
                return
            rows = self._object_table_values(table, fields)
            rows.pop(row)
            _replace(rows, min(row, len(rows) - 1))

        def _move(offset: int) -> None:
            row = table.currentRow()
            target = row + offset
            if row < 0 or target < 0 or target >= table.rowCount():
                return
            rows = self._object_table_values(table, fields)
            rows[row], rows[target] = rows[target], rows[row]
            _replace(rows, target)

        add_btn.clicked.connect(_add)
        remove_btn.clicked.connect(_remove)
        up_btn.clicked.connect(lambda: _move(-1))
        down_btn.clicked.connect(lambda: _move(1))
        container._table_widget = table  # type: ignore[attr-defined]
        container._item_fields = fields  # type: ignore[attr-defined]
        container._add_button = add_btn  # type: ignore[attr-defined]
        container._remove_button = remove_btn  # type: ignore[attr-defined]
        container._move_up_button = up_btn  # type: ignore[attr-defined]
        container._move_down_button = down_btn  # type: ignore[attr-defined]
        return container

    def _populate_object_table(
        self, table: QTableWidget, fields: tuple[FieldMeta, ...], value: Any,
    ) -> None:
        rows = [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []
        table.setRowCount(len(rows))
        for row, item in enumerate(rows):
            for column, field in enumerate(fields):
                cell_value = item.get(field.key)
                if field.field_type == "bool":
                    widget = QCheckBox()
                    widget.setChecked(bool(cell_value))
                    table.setCellWidget(row, column, widget)
                elif field.field_type == "float":
                    widget = QDoubleSpinBox()
                    widget.setMinimum(field.min_val if field.min_val is not None else -999999.0)
                    widget.setMaximum(field.max_val if field.max_val is not None else 999999.0)
                    widget.setSingleStep(field.step or 0.1)
                    widget.setValue(float(cell_value or 0.0))
                    table.setCellWidget(row, column, widget)
                elif field.field_type == "choice":
                    widget = QComboBox()
                    for option, label in choices_for(field, cell_value):
                        widget.addItem(label, option)
                    widget.setCurrentIndex(max(0, widget.findData(str(cell_value or ""))))
                    table.setCellWidget(row, column, widget)
                else:
                    table.setItem(row, column, QTableWidgetItem(str(cell_value or "")))

    @staticmethod
    def _object_table_values(
        table: QTableWidget, fields: tuple[FieldMeta, ...],
    ) -> list[dict[str, Any]]:
        result = []
        for row in range(table.rowCount()):
            item: dict[str, Any] = {}
            for column, field in enumerate(fields):
                widget = table.cellWidget(row, column)
                if field.field_type == "bool":
                    item[field.key] = widget.isChecked()
                elif field.field_type == "float":
                    item[field.key] = round(widget.value(), 3)
                elif field.field_type == "choice":
                    item[field.key] = widget.currentData()
                else:
                    cell = table.item(row, column)
                    item[field.key] = cell.text().strip() if cell else ""
            result.append(item)
        return result

    # -- MCP management page ------------------------------------------------

    def _build_mcp_page(self) -> QWidget:
        """Build the MCP servers management page."""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        # Header
        desc = QLabel(
            "MCP (Model Context Protocol) servers give Jarvis extra tools — "
            "file access, web search, databases, and more."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet("color: #a1a1aa; font-size: 13px;")
        layout.addWidget(desc)

        # Server list
        self._mcp_list = QListWidget()
        self._mcp_list.setMinimumHeight(180)
        self._mcp_list.setMaximumHeight(300)
        layout.addWidget(self._mcp_list)

        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.setContentsMargins(0, 0, 0, 0)
        btn_layout.setSpacing(6)

        add_catalogue_btn = QPushButton("📦 Add from Catalogue")
        add_catalogue_btn.setToolTip("Pick from a list of popular MCP servers")
        add_catalogue_btn.clicked.connect(self._on_mcp_add_catalogue)
        btn_layout.addWidget(add_catalogue_btn)

        add_custom_btn = QPushButton("+ Add Custom")
        add_custom_btn.setToolTip("Manually configure an MCP server")
        add_custom_btn.clicked.connect(self._on_mcp_add_custom)
        btn_layout.addWidget(add_custom_btn)

        edit_btn = QPushButton("✏️ Edit")
        edit_btn.clicked.connect(self._on_mcp_edit)
        btn_layout.addWidget(edit_btn)

        remove_btn = QPushButton("− Remove")
        remove_btn.clicked.connect(self._on_mcp_remove)
        btn_layout.addWidget(remove_btn)

        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        # Details panel for selected server
        self._mcp_detail = QLabel("")
        self._mcp_detail.setWordWrap(True)
        self._mcp_detail.setStyleSheet(
            "background-color: #12141a; border: 1px solid #27272a; "
            "border-radius: 8px; padding: 12px; color: #a1a1aa; font-size: 12px;"
        )
        self._mcp_detail.setMinimumHeight(60)
        layout.addWidget(self._mcp_detail)

        self._mcp_list.currentRowChanged.connect(self._on_mcp_selection_changed)

        # Populate from current config
        self._mcp_configs: Dict[str, Dict] = dict(self._merged.get("mcps", {}) or {})
        self._refresh_mcp_list()

        layout.addStretch()
        scroll.setWidget(container)
        return scroll

    def _refresh_mcp_list(self) -> None:
        """Refresh the MCP server list widget from the in-memory dict."""
        self._mcp_list.clear()
        for name, cfg in self._mcp_configs.items():
            catalogue_entry = CATALOGUE_BY_NAME.get(name)
            if catalogue_entry:
                display = f"{catalogue_entry.display_name}  ({name})"
            else:
                display = f"🔌 {name}"
            self._mcp_list.addItem(display)
        if self._mcp_list.count() == 0:
            self._mcp_detail.setText("No MCP servers configured. Add one to extend Jarvis's capabilities.")
        else:
            self._mcp_list.setCurrentRow(0)

    def _on_mcp_selection_changed(self, row: int) -> None:
        """Update the detail panel when an MCP server is selected."""
        if row < 0 or row >= len(self._mcp_configs):
            self._mcp_detail.setText("")
            return
        name = list(self._mcp_configs.keys())[row]
        cfg = self._mcp_configs[name]
        command = cfg.get("command", "")
        args = " ".join(str(a) for a in cfg.get("args", []))
        env_keys = ", ".join(cfg.get("env", {}).keys()) if cfg.get("env") else "none"
        self._mcp_detail.setText(
            f"<b>Name:</b> {name}<br>"
            f"<b>Command:</b> {command}<br>"
            f"<b>Args:</b> {args}<br>"
            f"<b>Env vars:</b> {env_keys}"
        )

    def _on_mcp_add_catalogue(self) -> None:
        """Show a dialog to pick from the curated catalogue."""
        dlg = _MCPCatalogueDialog(self._mcp_configs, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            for entry, extra_env in dlg.selected_entries_with_env():
                self._mcp_configs[entry.name] = entry.to_config(extra_env=extra_env)
            self._refresh_mcp_list()

    def _on_mcp_add_custom(self) -> None:
        """Show a dialog to manually add an MCP server."""
        dlg = _MCPEditDialog(parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            name, cfg = dlg.get_result()
            if name:
                self._mcp_configs[name] = cfg
                self._refresh_mcp_list()

    def _on_mcp_edit(self) -> None:
        """Edit the selected MCP server."""
        row = self._mcp_list.currentRow()
        if row < 0:
            return
        name = list(self._mcp_configs.keys())[row]
        cfg = self._mcp_configs[name]
        dlg = _MCPEditDialog(name=name, config=cfg, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            new_name, new_cfg = dlg.get_result()
            if new_name:
                if new_name != name:
                    del self._mcp_configs[name]
                self._mcp_configs[new_name] = new_cfg
                self._refresh_mcp_list()

    def _on_mcp_remove(self) -> None:
        """Remove the selected MCP server."""
        row = self._mcp_list.currentRow()
        if row < 0:
            return
        name = list(self._mcp_configs.keys())[row]
        reply = QMessageBox.question(
            self, "🔌 Remove MCP Server",
            f"Remove '{name}'?\n\nYou can always re-add it later.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            del self._mcp_configs[name]
            self._refresh_mcp_list()

    # -- Value extraction ---------------------------------------------------

    def _get_value(self, fm: FieldMeta) -> Any:
        """Extract the current value from a widget."""
        w = self._widgets[fm.key]

        if fm.field_type == "bool":
            return w.isChecked()

        if fm.field_type == "int" and fm.nullable:
            if hasattr(w, '_check') and not w._check.isChecked():
                return None
            return w._spin.value()

        if fm.field_type == "int":
            return w.value()

        if fm.field_type == "float":
            return round(w.value(), 3)

        if fm.field_type in ("choice", "device"):
            val = w.currentData()
            # For sample_rate, convert back to int
            if fm.key == "sample_rate":
                try:
                    return int(val)
                except (TypeError, ValueError):
                    return 16000
            return val if val != "" else None

        if fm.field_type == "list":
            list_w = w._list_widget
            return [list_w.item(i).text() for i in range(list_w.count())]

        if fm.field_type == "object_list":
            return self._object_table_values(w._table_widget, w._item_fields)

        # str
        text = w.text().strip()
        if fm.nullable and text == "":
            return None
        return text

    # -- Actions ------------------------------------------------------------

    def _on_save(self) -> None:
        """Collect values from widgets and save to config.json."""
        # Start from existing config (preserves keys we don't show in UI)
        config = dict(self._current_config)

        for fm in FIELD_METADATA:
            val = self._get_value(fm)
            default_val = self._defaults.get(fm.key)

            # Only write non-default values to keep config.json clean.
            if _is_default_value(val, default_val):
                config.pop(fm.key, None)
            else:
                config[fm.key] = val

        # Save MCP configs (empty dict = no MCPs, omit from config)
        if self._mcp_configs:
            config["mcps"] = dict(self._mcp_configs)
        else:
            config.pop("mcps", None)

        if _save_json(self._config_path, config):
            debug_log("settings saved to config.json", "settings")
            QMessageBox.information(
                self, "✅ Saved",
                "Settings saved. Restart Jarvis for changes to take effect."
            )
            self.accept()
        else:
            QMessageBox.warning(
                self, "⚠️ Error",
                f"Could not save settings to:\n{self._config_path}"
            )

    def _on_reset(self) -> None:
        """Reset all fields to defaults."""
        reply = QMessageBox.question(
            self, "↩️ Reset to Defaults",
            "Reset all settings to their default values?\n\n"
            "This will overwrite your config.json.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self._merged = dict(self._defaults)
        self._current_config = {}

        # Refresh all widgets
        for fm in FIELD_METADATA:
            self._set_widget_value(fm, self._defaults.get(fm.key))

        # Clear MCP configs
        self._mcp_configs = {}
        self._refresh_mcp_list()

        debug_log("settings reset to defaults", "settings")

    def _set_widget_value(self, fm: FieldMeta, value: Any) -> None:
        """Set a widget's value from a config value."""
        w = self._widgets.get(fm.key)
        if w is None:
            return

        if fm.field_type == "bool":
            w.setChecked(bool(value))

        elif fm.field_type == "int" and fm.nullable:
            has_val = value is not None
            w._check.setChecked(has_val)
            w._spin.setEnabled(has_val)
            try:
                w._spin.setValue(int(value) if has_val else 0)
            except (TypeError, ValueError):
                w._spin.setValue(0)

        elif fm.field_type == "int":
            try:
                w.setValue(int(value) if value is not None else 0)
            except (TypeError, ValueError):
                w.setValue(0)

        elif fm.field_type == "float":
            try:
                w.setValue(float(value) if value is not None else 0.0)
            except (TypeError, ValueError):
                w.setValue(0.0)

        elif fm.field_type in ("choice", "device"):
            cur_str = str(value) if value not in (None, "") else ""
            idx = w.findData(cur_str)
            if idx >= 0:
                w.setCurrentIndex(idx)

        elif fm.field_type == "list":
            list_w = w._list_widget
            list_w.clear()
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, str) and item.strip():
                        list_w.addItem(item.strip())

        elif fm.field_type == "object_list":
            self._populate_object_table(w._table_widget, w._item_fields, value)

        else:  # str
            w.setText(str(value) if value not in (None, "") else "")


# ---------------------------------------------------------------------------
# MCP dialogue windows
# ---------------------------------------------------------------------------

class _MCPCatalogueDialog(QDialog):
    """Dialog for picking MCP servers from the curated catalogue."""

    def __init__(self, existing: Dict[str, Dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle("📦 MCP Server Catalogue")
        self.setMinimumSize(480, 420)
        apply_theme(self)

        self._existing = existing
        self._checkboxes: Dict[str, QCheckBox] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        desc = QLabel("Select MCP servers to add. Already-configured servers are shown as checked.")
        desc.setWordWrap(True)
        desc.setStyleSheet("color: #a1a1aa; font-size: 13px;")
        layout.addWidget(desc)

        # Node.js availability warning
        node_warning = QLabel(
            "⚠️  <b>Node.js not found.</b> Most MCP servers require Node.js. "
            "<a href='https://nodejs.org/' style='color: #f59e0b;'>Download Node.js</a> "
            "and restart Jarvis to use them."
        )
        node_warning.setOpenExternalLinks(True)
        node_warning.setWordWrap(True)
        node_warning.setStyleSheet(
            "background: rgba(239, 68, 68, 0.12);"
            "border: 1px solid rgba(239, 68, 68, 0.35);"
            "border-radius: 8px; padding: 10px 14px; color: #fca5a5; font-size: 12px;"
        )
        node_warning.setVisible(not self._is_node_available())
        layout.addWidget(node_warning)

        # Scrollable list of catalogue entries
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        inner = QWidget()
        inner_layout = QVBoxLayout(inner)
        inner_layout.setSpacing(8)

        for entry in CATALOGUE:
            card = QFrame()
            card.setObjectName("card")
            card_layout = QHBoxLayout(card)
            card_layout.setContentsMargins(12, 10, 12, 10)
            card_layout.setSpacing(12)

            cb = QCheckBox()
            already_added = entry.name in existing
            cb.setChecked(already_added)
            if already_added:
                cb.setEnabled(False)
                cb.setToolTip("Already configured")
            self._checkboxes[entry.name] = cb
            card_layout.addWidget(cb)

            text_layout = QVBoxLayout()
            text_layout.setSpacing(2)

            name_label = QLabel(entry.display_name)
            name_label.setStyleSheet("font-weight: bold; font-size: 14px;")
            text_layout.addWidget(name_label)

            desc_label = QLabel(entry.description)
            desc_label.setWordWrap(True)
            desc_label.setStyleSheet("color: #a1a1aa; font-size: 12px;")
            text_layout.addWidget(desc_label)

            if entry.needs_api_key:
                key_label = QLabel(f"🔑 Requires {entry.api_key_env_var}")
                key_label.setStyleSheet("color: #fbbf24; font-size: 11px;")
                text_layout.addWidget(key_label)

            card_layout.addLayout(text_layout, 1)
            inner_layout.addWidget(card)

        inner_layout.addStretch()
        scroll.setWidget(inner)
        layout.addWidget(scroll, 1)

        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)
        add_btn = QPushButton("🔌 Add Selected")
        add_btn.setObjectName("primary")
        add_btn.clicked.connect(self._on_add)
        btn_layout.addWidget(add_btn)
        layout.addLayout(btn_layout)

    def _on_add(self) -> None:
        """Prompt for API keys if needed, then accept."""
        self._collected_env: Dict[str, Dict[str, str]] = {}
        for entry in self._selected_new_entries():
            if entry.needs_api_key and entry.api_key_env_var:
                key, ok = QInputDialog.getText(
                    self,
                    f"🔑 {entry.display_name} API Key",
                    f"Enter your {entry.api_key_env_var}:\n"
                    f"({entry.api_key_hint or ''})",
                )
                if ok and key.strip():
                    self._collected_env[entry.name] = {entry.api_key_env_var: key.strip()}
                else:
                    # User cancelled key entry — skip this entry
                    self._checkboxes[entry.name].setChecked(False)
                    continue
        self.accept()

    @staticmethod
    def _is_node_available() -> bool:
        """Check if Node.js (npx) is available on the system."""
        try:
            from jarvis.tools.external.mcp_client import _resolve_command
            _resolve_command("npx")
            return True
        except (FileNotFoundError, Exception):
            return False

    def _selected_new_entries(self) -> List[MCPEntry]:
        """Return catalogue entries the user selected (excluding already-configured)."""
        result = []
        for name, cb in self._checkboxes.items():
            if cb.isChecked() and cb.isEnabled():
                result.append(CATALOGUE_BY_NAME[name])
        return result

    def selected_entries_with_env(self) -> List[tuple]:
        """Return list of (MCPEntry, extra_env_dict) for each selected entry."""
        collected = getattr(self, "_collected_env", {})
        return [
            (entry, collected.get(entry.name, {}))
            for entry in self._selected_new_entries()
        ]


class _MCPEditDialog(QDialog):
    """Dialog for adding or editing a single MCP server configuration."""

    def __init__(self, name: str = "", config: Optional[Dict] = None, parent=None):
        super().__init__(parent)
        self._is_edit = bool(name)
        self.setWindowTitle("✏️ Edit MCP Server" if self._is_edit else "🔌 Add Custom MCP Server")
        self.setMinimumSize(440, 340)
        apply_theme(self)

        config = config or {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        form = QFormLayout()
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self._name_edit = QLineEdit(name)
        self._name_edit.setPlaceholderText("e.g. filesystem, my-server")
        if self._is_edit:
            self._name_edit.setEnabled(False)
        form.addRow("Name", self._name_edit)

        self._command_edit = QLineEdit(str(config.get("command", "")))
        self._command_edit.setPlaceholderText("e.g. npx, node, python")
        form.addRow("Command", self._command_edit)

        self._args_edit = QLineEdit(" ".join(str(a) for a in config.get("args", [])))
        self._args_edit.setPlaceholderText("e.g. -y @modelcontextprotocol/server-filesystem ~")
        self._args_edit.setToolTip("Space-separated arguments")
        form.addRow("Args", self._args_edit)

        env = config.get("env") or {}
        env_str = " ".join(f"{k}={v}" for k, v in env.items())
        self._env_edit = QLineEdit(env_str)
        self._env_edit.setPlaceholderText("e.g. API_KEY=abc123 (space-separated KEY=VALUE)")
        form.addRow("Env vars", self._env_edit)

        layout.addLayout(form)
        layout.addStretch()

        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)
        save_btn = QPushButton("💾 Save")
        save_btn.setObjectName("primary")
        save_btn.clicked.connect(self._on_save)
        btn_layout.addWidget(save_btn)
        layout.addLayout(btn_layout)

    def _on_save(self) -> None:
        name = self._name_edit.text().strip()
        command = self._command_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "⚠️ Missing Name", "Please enter a server name.")
            return
        if not command:
            QMessageBox.warning(self, "⚠️ Missing Command", "Please enter a command.")
            return
        self.accept()

    def get_result(self) -> tuple:
        """Return (name, config_dict) from the dialog fields."""
        name = self._name_edit.text().strip()
        command = self._command_edit.text().strip()
        args_text = self._args_edit.text().strip()
        args = args_text.split() if args_text else []
        env_text = self._env_edit.text().strip()
        env = {}
        if env_text:
            for pair in env_text.split():
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    env[k] = v

        cfg = {"transport": "stdio", "command": command, "args": args}
        if env:
            cfg["env"] = env
        return name, cfg

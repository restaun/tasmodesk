"""TasmoDesk: native Qt Widgets panel for local Tasmota devices."""

from __future__ import annotations

import queue
import sys
from dataclasses import dataclass
from functools import partial
from typing import Callable

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QCloseEvent, QDesktopServices, QKeyEvent, QMouseEvent, QResizeEvent, QWheelEvent,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFrame, QGridLayout,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QProgressBar,
    QPushButton, QScrollArea, QSizePolicy, QSlider, QTextEdit, QToolButton, QVBoxLayout, QWidget,
)

from tasmota_manager import RELEASE_PAGE_URL, TasmotaError
from tasmota_service import (
    BackupEntry, DeviceEntry, JobEntry, TasmotaService,
)

PAGES = ("Dispositivos", "Configuración", "Actualizaciones")
FILTERS = ("Todos", "Disponibles", "Sin conexión", "Con salidas apagadas", "Pendientes de actualizar")


def label(text: str, style: str = "", *, wrap: bool = True) -> QLabel:
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(wrap)
    if wrap:
        widget.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    widget.setProperty("role", style)
    return widget


def button(text: str, callback: Callable[[], object], *, secondary: bool = False) -> QPushButton:
    widget = QPushButton(text)
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    widget.setProperty("secondary", secondary)
    widget.clicked.connect(lambda _checked=False: callback())
    return widget


def card(parent: QWidget | None = None, margins: int = 16) -> tuple[QFrame, QVBoxLayout]:
    widget = QFrame(parent)
    widget.setProperty("role", "card")
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(margins, margins, margins, margins)
    layout.setSpacing(12)
    return widget, layout


def discard_widget(widget: QWidget, holder: QWidget) -> None:
    # Never use setParent(None): Qt would briefly turn the widget into a visible window.
    widget.hide()
    widget.setParent(holder)
    widget.deleteLater()


def stylesheet(dark: bool) -> str:
    bg, surface, text, muted, line, nav = (
        ("#0d1829", "#1c2b42", "#e3ecf9", "#a2b4cd", "#30445f", "#101f33") if dark
        else ("#f2f5fb", "#ffffff", "#21324a", "#6c7b91", "#e1e7f0", "#132d4e")
    )
    return f"""
    QWidget {{ font-family: 'Segoe UI'; font-size: 13px; color: {text}; }}
    QMainWindow, QWidget#root {{ background: {bg}; }}
    QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; border: none; }}
    QFrame[role="card"] {{ background: {surface}; border: 1px solid {line}; border-radius: 14px; }}
    QFrame#sidebar {{ background: {nav}; border-radius: 18px; }}
    QFrame#sidebar QLabel {{ color: #abc2df; }}
    QFrame#sidebar QLabel[role="brand"] {{ color: white; font-size: 24px; font-weight: 700; }}
    QLabel[role="heading"] {{ font-size: 29px; font-weight: 700; }}
    QLabel[role="title"] {{ font-size: 21px; font-weight: 700; }}
    QLabel[role="count"] {{ font-size: 20px; font-weight: 700; }}
    QLabel[role="muted"] {{ color: {muted}; font-size: 12px; }}
    QLabel[role="online"] {{ color: #15966c; font-size: 12px; }}
    QLabel[role="warning"] {{ color: #b77413; font-size: 12px; }}
    QLabel[role="error"] {{ color: #d94d51; }}
    QPushButton {{ background: #2563eb; color: white; border: 1px solid transparent; border-radius: 9px;
                   padding: 9px 12px; font-weight: 600; }}
    QPushButton:hover {{ background: #1d4ed8; }}
    QPushButton[secondary="true"] {{ background: {bg}; color: {text}; border: 1px solid {line}; }}
    QPushButton:disabled {{ background: {line}; color: {muted}; }}
    QFrame#sidebar QPushButton {{ background: transparent; color: #d5e4f6; text-align: left; }}
    QFrame#sidebar QPushButton:checked {{ background: #2563eb; color: white; }}
    QLineEdit, QComboBox, QTextEdit {{ background: {bg}; border: 1px solid {line};
        border-radius: 8px; padding: 8px; selection-background-color: #2563eb; }}
    QLineEdit:focus, QComboBox:focus, QPushButton:focus {{ border: 1px solid #2563eb; }}
    QComboBox QAbstractItemView {{ background: {surface}; color: {text}; selection-background-color: #2563eb; }}
    QProgressBar {{ background: {bg}; border: 1px solid {line}; border-radius: 6px;
                   min-height: 16px; text-align: center; }}
    QProgressBar::chunk {{ background: #15966c; border-radius: 5px; }}
    QSlider::groove:horizontal {{ background: {line}; height: 5px; border-radius: 2px; }}
    QSlider::handle:horizontal {{ background: #2563eb; width: 16px; margin: -6px 0; border-radius: 8px; }}
    QToolButton {{ color: #2563eb; border: none; padding: 5px 0; text-align: left; }}
    QTextEdit {{ font-family: Consolas; font-size: 12px; }}
    QCheckBox {{ spacing: 8px; }}
    """


@dataclass
class Draft:
    value: str
    saving: bool = False


class DimmerSlider(QSlider):
    committed = Signal(int)

    def __init__(self, value: int) -> None:
        super().__init__(Qt.Orientation.Horizontal)
        self.setRange(0, 100)
        self.setValue(value)
        self.last_committed = value
        self.wheel_timer = QTimer(self)
        self.wheel_timer.setSingleShot(True)
        self.wheel_timer.setInterval(500)
        self.wheel_timer.timeout.connect(self.commit)
        self.sliderPressed.connect(self.wheel_timer.stop)

    def commit(self) -> None:
        self.wheel_timer.stop()
        if self.value() != self.last_committed:
            self.last_committed = self.value()
            self.committed.emit(self.value())

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        super().mouseReleaseEvent(event)
        self.commit()

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        super().keyReleaseEvent(event)
        if not event.isAutoRepeat():
            self.commit()

    def wheelEvent(self, event: QWheelEvent) -> None:
        super().wheelEvent(event)
        if self.value() != self.last_committed:
            self.wheel_timer.start()


class TasmotaWindow(QMainWindow):
    def __init__(self, service: TasmotaService) -> None:
        super().__init__()
        self.service = service
        self.state = service.snapshot()
        self.page = str(self.state["preferences"]["page"])
        self.dark = self.state["preferences"]["theme"] == "dark"
        self.revision = -1
        self.polling = False
        self.drafts: dict[tuple[str, int], Draft] = {}
        self.backup_views: dict[str, list[BackupEntry]] = {}
        self.open_logs: set[str] = set()
        self.cards: dict[str, QFrame] = {}
        self.job_widgets: dict[str, tuple[QProgressBar, QLabel]] = {}
        self.notifications: list[QFrame] = []
        self.setWindowTitle("TasmoDesk")
        self.resize(1180, 820)
        self.setMinimumSize(900, 650)
        self.build_layout()
        self.setStyleSheet(stylesheet(self.dark))
        self.timer = QTimer(self)
        self.timer.setInterval(750)
        self.timer.timeout.connect(self.poll)
        self.timer.start()
        self.poll(force=True)

    def build_layout(self) -> None:
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        # Parentless but never shown: discarded widgets wait here for deleteLater without flashing.
        self.trash = QWidget()
        outer = QHBoxLayout(root)
        outer.setContentsMargins(18, 18, 24, 18)
        outer.setSpacing(22)
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(218)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(18, 26, 18, 22)
        side.setSpacing(12)
        side.addWidget(label("TASMODESK", "brand"))
        side.addWidget(label("Panel local Tasmota"))
        side.addSpacing(24)
        self.navigation: dict[str, QPushButton] = {}
        for page in PAGES:
            control = button(page, partial(self.navigate, page))
            control.setCheckable(True)
            self.navigation[page] = control
            side.addWidget(control)
        side.addWidget(button("Claro / oscuro", self.toggle_theme))
        side.addStretch()
        side.addWidget(label("Red local · sin nube\nWidgets nativos · sin navegador"))
        outer.addWidget(sidebar)
        main = QVBoxLayout()
        main.setSpacing(14)
        outer.addLayout(main, 1)
        header = QHBoxLayout()
        self.heading = label(self.page, "heading")
        header.addWidget(self.heading, 1)
        self.refresh_button = button("Refrescar estados", lambda: self.invoke(self.service.refresh), secondary=True)
        self.discover_button = button("Buscar Tasmotas", lambda: self.invoke(self.service.discover))
        header.addWidget(self.refresh_button)
        header.addWidget(self.discover_button)
        main.addLayout(header)
        self.status = label("", "muted")
        main.addWidget(self.status)
        summary = QHBoxLayout()
        self.counts: list[QLabel] = []
        for text in ("Equipos guardados", "Disponibles", "Actualizándose"):
            frame = QFrame()
            frame.setProperty("role", "card")
            frame.setFixedHeight(46)
            layout = QHBoxLayout(frame)
            layout.setContentsMargins(14, 4, 14, 4)
            layout.setSpacing(10)
            count = label("0", "count", wrap=False)
            self.counts.append(count)
            layout.addWidget(count)
            layout.addWidget(label(text, "muted", wrap=False), 1)
            summary.addWidget(frame, 1)
        main.addLayout(summary)
        toolbar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Buscar por nombre, salida o IP")
        self.search.setAccessibleName("Buscar dispositivos")
        self.search.textChanged.connect(self.render_devices)
        self.filter = QComboBox()
        self.filter.addItems(FILTERS)
        self.filter.setAccessibleName("Filtrar dispositivos")
        self.filter.currentIndexChanged.connect(self.render_devices)
        toolbar.addWidget(self.search, 1)
        toolbar.addWidget(self.filter)
        main.addLayout(toolbar)
        self.device_scroll = QScrollArea()
        self.device_scroll.setWidgetResizable(True)
        self.device_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.device_scroll.viewport().installEventFilter(self)
        content = QWidget()
        self.content_layout = QVBoxLayout(content)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(14)
        self.batch_frame, batch = card()
        batch.addWidget(label("Resumen de actualizaciones", "title"))
        self.batch_text = label("")
        batch.addWidget(self.batch_text)
        self.content_layout.addWidget(self.batch_frame)
        self.build_access()
        self.build_firmware()
        self.grid_widget = QWidget()
        self.grid = QGridLayout(self.grid_widget)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(14)
        self.content_layout.addWidget(self.grid_widget)
        self.content_layout.addStretch()
        self.device_scroll.setWidget(content)
        main.addWidget(self.device_scroll, 1)
        self.alert_scroll = QScrollArea()
        self.alert_scroll.setWidgetResizable(True)
        self.alert_scroll.setMaximumHeight(170)
        alerts = QWidget()
        self.alerts = QVBoxLayout(alerts)
        self.alerts.setContentsMargins(0, 0, 0, 0)
        self.alert_scroll.setWidget(alerts)
        self.alert_scroll.hide()
        main.addWidget(self.alert_scroll)

    def build_access(self) -> None:
        self.access_frame, layout = card()
        layout.addWidget(label("Acceso a los dispositivos", "title"))
        layout.addWidget(label("Opcionales. La contraseña permanece en memoria, no se guarda.", "muted"))
        row = QHBoxLayout()
        self.username = QLineEdit("admin")
        self.username.setAccessibleName("Usuario")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setAccessibleName("Contraseña")
        for text, field in (("Usuario", self.username), ("Contraseña", self.password)):
            column = QVBoxLayout()
            column.addWidget(label(text))
            column.addWidget(field)
            row.addLayout(column, 1)
        row.addWidget(button("Aplicar acceso", lambda: self.invoke(
            lambda: self.service.set_access(self.username.text(), self.password.text())
        )))
        layout.addLayout(row)
        self.auto_refresh = QCheckBox("Refrescar automáticamente cada 60 segundos")
        def save_auto_refresh(value: bool) -> None:
            self.invoke(lambda: self.service.set_preferences({"auto_refresh": value}))
        self.auto_refresh.toggled.connect(save_auto_refresh)
        layout.addWidget(self.auto_refresh)
        self.auto_status = label("", "muted")
        layout.addWidget(self.auto_status)
        self.content_layout.addWidget(self.access_frame)

    def build_firmware(self) -> None:
        self.firmware_frame, layout = card()
        layout.addWidget(label("Firmware oficial", "title"))
        self.firmware_text = label("")
        self.firmware_source = label("", "muted")
        self.firmware_source.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.firmware_text)
        layout.addWidget(self.firmware_source)
        row = QHBoxLayout()
        self.language = QComboBox()
        self.language.addItems(("Español", "English"))
        self.language.setAccessibleName("Idioma")
        self.language.setToolTip("Idioma del firmware")
        def save_language(value: str) -> None:
            self.invoke(lambda: self.service.set_language(value))
        self.language.currentTextChanged.connect(save_language)
        row.addWidget(self.language)
        self.release_button = button("Buscar versión", lambda: self.invoke(self.service.check_release))
        row.addWidget(self.release_button)
        self.update_all = button("Actualizar todos", lambda: self.start_update_ui("", True))
        row.addWidget(self.update_all)
        self.local_all = button("Archivo local…", lambda: self.local_update_ui(""), secondary=True)
        self.local_all.setToolTip("Instalar un .bin local en todos los equipos compatibles")
        row.addWidget(self.local_all)
        row.addWidget(button("Web oficial", lambda: self.invoke(
            lambda: self.open_url(QUrl(RELEASE_PAGE_URL))
        ), secondary=True))
        row.addStretch()
        layout.addLayout(row)
        layout.addWidget(label("Copia individual previa · descarga SHA-256 o archivo local .bin validado · "
                               "confirmación tras el reinicio", "muted"))
        self.content_layout.addWidget(self.firmware_frame)

    def notify(self, text: str, error: bool = False) -> None:
        frame, layout = card(self.alert_scroll.widget())
        row = QHBoxLayout()
        row.addWidget(label(text, "error" if error else ""), 1)
        row.addWidget(button("Cerrar", partial(self.dismiss, frame), secondary=True))
        layout.addLayout(row)
        self.notifications.append(frame)
        self.alerts.addWidget(frame)
        self.alert_scroll.show()

    def dismiss(self, frame: QFrame) -> None:
        self.notifications.remove(frame)
        discard_widget(frame, self.trash)
        self.alert_scroll.setVisible(bool(self.notifications))

    def invoke(self, action: Callable[[], object]) -> bool:
        try:
            action()
        except Exception as error:
            self.notify(str(error), True)
            return False
        self.poll()
        return True

    def navigate(self, page: str) -> None:
        self.page = page
        self.invoke(lambda: self.service.set_preferences({"page": page}))
        self.render_devices()

    def toggle_theme(self) -> None:
        self.dark = not self.dark
        self.setStyleSheet(stylesheet(self.dark))
        self.invoke(lambda: self.service.set_preferences({"theme": "dark" if self.dark else "light"}))

    def matches(self, device: DeviceEntry) -> bool:
        query = self.search.text().strip().casefold()
        text = " ".join([device["name"], device["ip"], *(r["name"] for r in device["relays"])])
        if query and query not in text.casefold():
            return False
        index = self.filter.currentIndex()
        if index == 1:
            return device["available"]
        if index == 2:
            return not device["available"]
        if index == 3:
            return device["available"] and any(r["state"] == "OFF" for r in device["relays"])
        if index == 4:
            return device["file_available"] or device["ota_available"]
        return True

    def render_devices(self) -> None:
        scroll = self.device_scroll.verticalScrollBar().value()
        self.heading.setText(self.page)
        for name, control in self.navigation.items():
            control.setChecked(name == self.page)
        self.access_frame.setVisible(self.page == "Configuración")
        self.firmware_frame.setVisible(self.page == "Actualizaciones")
        self.batch_frame.setVisible(self.page == "Actualizaciones" and bool(self.state["jobs"]))
        self.grid_widget.setUpdatesEnabled(False)
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item is not None and (widget := item.widget()) is not None:
                discard_widget(widget, self.trash)
        self.cards.clear()
        self.job_widgets.clear()
        for device in self.state["devices"]:
            if self.matches(device):
                self.cards[device["ip"]] = self.device_card(device)
        if not self.cards:
            frame, layout = card(self.grid_widget)
            layout.addWidget(label("No hay equipos que coincidan con la búsqueda." if self.state["devices"]
                                   else "No hay equipos guardados. Pulsa Buscar Tasmotas."))
            self.grid.addWidget(frame, 0, 0)
        else:
            self.arrange_cards()
        self.grid_widget.setUpdatesEnabled(True)
        self.revision = self.state["revision"]
        self.device_scroll.verticalScrollBar().setValue(scroll)

    def arrange_cards(self) -> None:
        if not self.cards:
            return
        while self.grid.count():
            self.grid.takeAt(0)
        columns = max(1, self.device_scroll.viewport().width() // 340)
        width = max(1, (self.device_scroll.viewport().width() - 14 * (columns - 1)) // columns)
        frames = list(self.cards.values())
        for frame in frames:
            frame.setMinimumHeight(0)
        # Every card takes the height of the tallest one so all boxes keep the same size.
        def wanted(frame: QFrame) -> int:
            layout = frame.layout()
            if layout is not None and layout.hasHeightForWidth():
                return layout.totalHeightForWidth(width)
            return frame.sizeHint().height()
        height = max(wanted(frame) for frame in frames)
        for index, frame in enumerate(frames):
            frame.setMinimumHeight(height)
            self.grid.addWidget(frame, index // columns, index % columns)
        for column in range(4):
            self.grid.setColumnStretch(column, 1 if column < columns else 0)

    def device_card(self, device: DeviceEntry) -> QFrame:
        frame, layout = card(self.grid_widget)
        ip = device["ip"]
        frame.setObjectName("device-" + ip)
        header = QHBoxLayout()
        header.addWidget(label(device["name"], "title"), 1)
        header.addWidget(label(ip, "muted", wrap=False))
        layout.addLayout(header)
        connection = "Actualizándose" if device["updating"] else "Disponible" if device["available"] else "Sin conexión"
        role = "warning" if device["updating"] else "online" if device["available"] else "muted"
        layout.addWidget(label("● " + connection, role))
        layout.addWidget(label("Última lectura correcta: " + (device["last_read"] or "sin consultar"), "muted"))
        disabled = bool(device["pending"] or device["updating"] or self.state["devices_locked"] or self.state["demo"])
        if not device["available"]:
            layout.addWidget(label(device["error"], "error"))
        elif self.page == "Dispositivos":
            for index, relay in enumerate(device["relays"]):
                if device["dimmer"] is not None and index == len(device["relays"]) - 1:
                    self.dimmer_row(layout, device, relay["name"], disabled)
                else:
                    row = QHBoxLayout()
                    row.addWidget(label(relay["name"]), 1)
                    on = relay["state"] == "ON"
                    row.addWidget(label("Encendido" if on else "Apagado", "online" if on else "muted", wrap=False))
                    control = button("Apagar" if on else "Encender", partial(
                        self.change, ip, "power", "OFF" if on else "ON", relay["number"]
                    ), secondary=on)
                    control.setObjectName(f"power-{ip}-{relay['number']}")
                    control.setAccessibleName(f"{control.text()} {relay['name']} de {device['name']}")
                    control.setEnabled(not disabled)
                    row.addWidget(control)
                    layout.addLayout(row)
            if not device["relays"] and device["dimmer"] is not None:
                self.dimmer_row(layout, device, "Intensidad", disabled)
        elif self.page == "Configuración":
            layout.addWidget(label(
                f"Tasmota {device['version']}\n{device['hardware']}\n"
                f"Actividad {device['uptime']} · Wi-Fi {device['rssi']}% ({device['signal_dbm']} dBm)", "muted"
            ))
            self.name_row(layout, ip, 0, device["name"], disabled)
            for relay in device["relays"]:
                self.name_row(layout, ip, relay["number"], relay["name"], disabled)
            backup = button("Guardar copia de configuración", lambda: self.invoke(
                partial(self.service.backup, ip)
            ))
            backup.setEnabled(not disabled)
            layout.addWidget(backup)
        else:
            locale = {1034: "Español", 2057: "English"}.get(device["language_lcid"] or 0, "sin confirmar")
            layout.addWidget(label(f"Instalado: {device['version']} · {locale}", "muted"))
            row = QHBoxLayout()
            running = any(job["state"] == "running" for job in self.state["jobs"])
            for text, by_file, available in (("Descargar y subir", True, device["file_available"]),
                                            ("OTA", False, device["ota_available"])):
                control = button(text, partial(self.start_update_ui, ip, by_file), secondary=not by_file)
                control.setEnabled(not disabled and available and not running)
                row.addWidget(control)
            local = button("Archivo…", partial(self.local_update_ui, ip), secondary=True)
            local.setObjectName("local-" + ip)
            local.setToolTip("Seleccionar un firmware .bin local para este equipo")
            local.setEnabled(not disabled and not running and not self.state["busy"])
            row.addWidget(local)
            layout.addLayout(row)
        if self.page == "Configuración":
            self.backup_controls(layout, ip)
        if self.page == "Actualizaciones":
            job = next((job for job in self.state["jobs"] if job["ip"] == ip), None)
            if job:
                self.job_card(layout, job)
        layout.addWidget(button("Abrir WebUI", lambda: self.invoke(partial(self.open_webui, ip)), secondary=True))
        return frame

    def dimmer_row(self, layout: QVBoxLayout, device: DeviceEntry, name: str, disabled: bool) -> None:
        row = QHBoxLayout()
        row.addWidget(label(name), 1)
        slider = DimmerSlider(device["dimmer"] or 0)
        slider.setMinimumWidth(100)
        slider.setEnabled(not disabled)
        slider.setObjectName("dimmer-" + device["ip"])
        slider.setAccessibleName("Intensidad de " + device["name"])
        value = label(f"{slider.value()}%", wrap=False)
        def show_value(level: int) -> None:
            value.setText(f"{level}%")
        def commit_value(level: int) -> None:
            self.change(device["ip"], "dimmer", level)
        slider.valueChanged.connect(show_value)
        slider.committed.connect(commit_value)
        row.addWidget(slider, 2)
        row.addWidget(value)
        layout.addLayout(row)

    def name_row(self, layout: QVBoxLayout, ip: str, number: int, name: str, disabled: bool) -> None:
        key = (ip, number)
        draft = self.drafts.get(key)
        layout.addWidget(label(f"Salida {number}" if number else "Dispositivo", "muted"))
        row = QHBoxLayout()
        field = QLineEdit(draft.value if draft else name)
        field.setMinimumWidth(60)
        field.setObjectName(f"name-{ip}-{number}")
        field.setAccessibleName(f"Salida {number}" if number else "Dispositivo")
        field.setEnabled(not disabled)
        field.textEdited.connect(partial(self.edit_name, key, name))
        row.addWidget(field, 1)
        save = button("Guardar", lambda: self.save_name(ip, number, field.text()))
        save.setEnabled(not disabled)
        row.addWidget(save)
        if draft:
            discard = button("Descartar", partial(self.discard_name, key), secondary=True)
            discard.setEnabled(not draft.saving)
            row.addWidget(discard)
        layout.addLayout(row)
        if draft:
            layout.addWidget(label("Guardando…" if draft.saving else "Sin guardar", "warning"))

    def edit_name(self, key: tuple[str, int], original: str, value: str) -> None:
        if value == original:
            self.drafts.pop(key, None)
        else:
            self.drafts[key] = Draft(value)
        self.revision = -1

    def discard_name(self, key: tuple[str, int]) -> None:
        self.drafts.pop(key, None)
        self.render_devices()

    def save_name(self, ip: str, number: int, value: str) -> None:
        draft = Draft(value.strip(), True)
        self.drafts[(ip, number)] = draft
        if not self.change(ip, "relay_name" if number else "name", value, number):
            draft.saving = False
            self.drafts[(ip, number)] = draft
        self.render_devices()

    def change(self, ip: str, operation: str, value: str | int, number: int = 0) -> bool:
        self.cards[ip].setEnabled(False)
        success = self.invoke(partial(self.service.change, ip, operation, value, number))
        self.poll(force=True)
        return success

    def backup_controls(self, layout: QVBoxLayout, ip: str) -> None:
        layout.addWidget(button("Ver copias guardadas", partial(self.show_backups, ip), secondary=True))
        layout.addWidget(button("Abrir carpeta de copias", lambda: self.invoke(self.open_backup_folder), secondary=True))
        if ip in self.backup_views:
            rows = self.backup_views[ip]
            text = "\n\n".join(f"{r['name']}\n{r['modified']} · {r['size'] / 1024:.1f} KB" for r in rows)
            copies = QTextEdit()
            copies.setReadOnly(True)
            copies.setPlainText(text or "No hay copias de este equipo en la carpeta.")
            copies.setMaximumHeight(170)
            layout.addWidget(copies)

    def show_backups(self, ip: str) -> None:
        def action() -> None:
            self.backup_views[ip] = self.service.list_backups(ip)
        if self.invoke(action):
            self.render_devices()

    @staticmethod
    def open_url(url: QUrl) -> None:
        if not QDesktopServices.openUrl(url):
            raise TasmotaError("No se pudo abrir la dirección en el navegador o explorador.")

    def open_webui(self, ip: str) -> None:
        with self.service.lock:
            if ip not in self.service.devices:
                raise TasmotaError("Dispositivo no guardado.")
        self.open_url(QUrl(f"http://{ip}"))

    def open_backup_folder(self) -> None:
        folder = self.service.backup_directory()
        folder.mkdir(parents=True, exist_ok=True)
        self.open_url(QUrl.fromLocalFile(str(folder)))

    def confirm(self, text: str) -> bool:
        dialog = QMessageBox(self)
        dialog.setWindowTitle("Confirmar operación")
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        dialog.setText(text)
        dialog.setStandardButtons(QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
        dialog.setDefaultButton(QMessageBox.StandardButton.Cancel)
        dialog.button(QMessageBox.StandardButton.Ok).setText("Continuar")
        dialog.button(QMessageBox.StandardButton.Cancel).setText("Cancelar")
        return dialog.exec() == QMessageBox.StandardButton.Ok

    def start_update_ui(self, ip: str, by_file: bool) -> None:
        def action() -> None:
            message = self.service.update_confirmation(ip=ip, by_file=by_file)
            if self.confirm(message):
                self.service.start_updates(ip=ip, by_file=by_file)
        self.invoke(action)

    def local_update_ui(self, ip: str) -> None:
        folder = self.service.firmware_directory
        path, _filter = QFileDialog.getOpenFileName(
            self, "Seleccionar firmware Tasmota", str(folder if folder.exists() else folder.parent),
            "Firmware Tasmota (*.bin)",
        )
        if not path:
            return
        def action() -> None:
            message, digest = self.service.local_update_confirmation(path, ip)
            if self.confirm(message):
                self.service.start_local_updates(path, digest, ip)
        self.invoke(action)

    def previous_backup(self, ip: str) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self, "Copia anterior de este equipo", str(self.service.backup_directory()),
            "Configuración Tasmota (*.dmp)",
        )
        if path and self.confirm(f"¿Confirmas que esta copia pertenece a {ip}?\n{path}\n"
                                 "Puede no incluir cambios posteriores."):
            self.invoke(partial(self.service.use_previous_backup, ip, path))

    def job_card(self, layout: QVBoxLayout, job: JobEntry) -> None:
        progress = QProgressBar()
        progress.setRange(0, 100)
        progress.setValue(round(job["progress"]))
        progress.setAccessibleName("Progreso de " + job["ip"])
        message = label(f"{round(job['progress'])}% · {job['message']}")
        self.job_widgets[job["ip"]] = (progress, message)
        layout.addWidget(progress)
        layout.addWidget(message)
        layout.addWidget(label("Estimación temporal; 100% solo al confirmar versión e idioma.", "muted"))
        if job["backup"]:
            layout.addWidget(label("Copia guardada: " + job["backup"], "muted"))
        logs = QTextEdit()
        logs.setReadOnly(True)
        logs.setPlainText("\n".join(job["logs"]))
        logs.setMaximumHeight(220)
        toggle = QToolButton()
        toggle.setText(f"Registro completo ({len(job['logs'])} entradas)")
        toggle.setCheckable(True)
        toggle.setChecked(job["ip"] in self.open_logs)
        layout.addWidget(toggle)
        layout.addWidget(logs)
        logs.setVisible(toggle.isChecked())
        def show(opened: bool) -> None:
            logs.setVisible(opened)
            if opened:
                self.open_logs.add(job["ip"])
            else:
                self.open_logs.discard(job["ip"])
        toggle.toggled.connect(show)
        if job["needs_backup"]:
            layout.addWidget(button("Seleccionar copia anterior de este equipo",
                                    partial(self.previous_backup, job["ip"])))

    def poll(self, *, force: bool = False) -> None:
        if self.polling:
            return
        self.polling = True
        try:
            self.service.auto_refresh_tick()
            self.state = self.service.snapshot()
            self.reconcile_drafts()
            busy = bool(self.state["busy"] or any(d["pending"] or d["updating"] for d in self.state["devices"]))
            self.refresh_button.setEnabled(not busy and not self.state["demo"])
            self.discover_button.setEnabled(not busy and not self.state["demo"] and not self.state["inventory_error"])
            self.release_button.setEnabled(not busy and not self.state["demo"])
            self.update_all.setEnabled(not busy and not self.state["demo"] and bool(self.state["release"]))
            self.local_all.setEnabled(not busy and not self.state["demo"])
            self.status.setText("VISTA DEMO · sin conexión ni cambios" if self.state["demo"]
                                else (self.state["busy"] + " " + self.state["search_progress"]).strip() or "Listo")
            values = (len(self.state["devices"]), sum(d["available"] for d in self.state["devices"]),
                      sum(j["state"] == "running" for j in self.state["jobs"]))
            for control, count in zip(self.counts, values):
                control.setText(str(count))
            counts = {s: sum(j["state"] == s for j in self.state["jobs"]) for s in ("success", "running", "error")}
            self.batch_text.setText(f"{counts['success']} completados · {counts['running']} actualizando · {counts['error']} con error")
            self.firmware_text.setText("Firmware encontrado: Tasmota " + self.state["release"] if self.state["release"]
                                       else "Firmware sin consultar. Pulsa Buscar versión.")
            self.firmware_source.setText(
                f"Archivo: {self.state['release_filename']} · Idioma: {self.state['language']}\n"
                f"Origen OTA: {self.state['release_url']}" if self.state["release"] else ""
            )
            self.language.blockSignals(True)
            self.language.setCurrentText(self.state["language"])
            self.language.blockSignals(False)
            self.auto_refresh.blockSignals(True)
            self.auto_refresh.setChecked(bool(self.state["preferences"]["auto_refresh"]))
            self.auto_refresh.blockSignals(False)
            self.auto_status.setText("Pausado durante las operaciones" if self.state["auto_refresh_paused"]
                                     else "Solo consultas de lectura; se pausa durante actualizaciones")
            focus = QApplication.focusWidget()
            editing = isinstance(focus, (QLineEdit, QComboBox, QSlider)) and self.device_scroll.isAncestorOf(focus)
            dragging = any(s.isSliderDown() for s in self.grid_widget.findChildren(QSlider))
            if force or (self.revision != self.state["revision"] and not editing and not dragging):
                self.render_devices()
            for job in self.state["jobs"]:
                if widgets := self.job_widgets.get(job["ip"]):
                    widgets[0].setValue(round(job["progress"]))
                    widgets[1].setText(f"{round(job['progress'])}% · {job['message']}")
            while True:
                try:
                    kind, message = self.service.events.get_nowait()
                except queue.Empty:
                    break
                self.notify(message, kind == "error")
        except Exception as error:
            self.notify(f"No se pudo consultar la interfaz local: {error}", True)
        finally:
            self.polling = False

    def reconcile_drafts(self) -> None:
        for key, draft in list(self.drafts.items()):
            if not draft.saving:
                continue
            device = next((d for d in self.state["devices"] if d["ip"] == key[0]), None)
            if device is not None and not device["pending"]:
                value = device["name"] if key[1] == 0 else next(
                    (r["name"] for r in device["relays"] if r["number"] == key[1]), None
                )
                if device["available"] and value == draft.value:
                    del self.drafts[key]
                else:
                    draft.saving = False
                self.revision = -1

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if hasattr(self, "grid"):
            self.arrange_cards()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Resize and hasattr(self, "grid"):
            if watched is self.device_scroll.viewport():
                self.arrange_cards()
        return super().eventFilter(watched, event)

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self.service.close():
            event.ignore()
            self.notify("Espera a que terminen las operaciones antes de cerrar.", True)
            return
        self.timer.stop()
        self.trash.deleteLater()
        event.accept()


def main() -> None:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    demo = "--demo" in sys.argv or "--smoke" in sys.argv
    service = TasmotaService(demo=demo)
    window = TasmotaWindow(service)
    window.show()
    if not demo and not service.inventory_error:
        window.invoke(service.refresh)
    smoke_ok = False
    def smoke() -> None:
        nonlocal smoke_ok
        try:
            assert len(window.cards) == 3
            assert len(window.grid_widget.findChildren(QSlider)) == 3
            for page in PAGES:
                window.navigate(page)
                assert window.heading.text() == page
                assert len(window.cards) == 3
            assert "tasmota32-ES.bin" in window.firmware_source.text()
            window.toggle_theme()
            assert window.dark
            assert not window.notifications
            smoke_ok = True
            print("TASMODESK SMOKE OK", flush=True)
        except Exception as error:
            print(f"TASMODESK SMOKE FAILED: {error}", flush=True)
        finally:
            window.close()
    if "--smoke" in sys.argv:
        QTimer.singleShot(1500, smoke)
    exit_code = app.exec()
    if "--smoke" in sys.argv and not smoke_ok:
        raise RuntimeError("No se confirmó la prueba de la interfaz nativa.")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()

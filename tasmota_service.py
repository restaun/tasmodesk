"""UI-independent operations for the Tasmota applications."""

from __future__ import annotations

import queue
import json
import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, TypedDict

from tasmota_manager import (
    DeviceStatus, FirmwareRelease, FirmwareUploadUncertain, RelayStatus,
    TasmotaClient, TasmotaError, FIRMWARE_URL, SPANISH_FIRMWARE_URL,
    OTA_TIMEOUT_SECONDS, discover_tasmotas, download_firmware_file,
    estimated_firmware_progress, fetch_latest_firmware_release,
    firmware_file_update_available, firmware_release_installed,
    firmware_update_available, load_device_inventory, local_firmware_compatible,
    read_local_firmware, save_device_inventory, scan_devices,
    validate_name,
)

PREFERENCE_DEFAULTS: dict[str, str | bool] = {"theme": "light", "page": "Dispositivos", "auto_refresh": True}
AUTO_REFRESH_SECONDS = 60


class BackupEntry(TypedDict):
    name: str
    size: int
    modified: str


class RelayEntry(TypedDict):
    number: int
    name: str
    state: str


class DeviceEntry(TypedDict):
    ip: str
    name: str
    version: str
    hardware: str
    uptime: str
    rssi: int | None
    signal_dbm: int | None
    language_lcid: int | None
    relays: list[RelayEntry]
    dimmer: int | None
    error: str
    last_read: str
    available: bool
    pending: bool
    updating: bool
    ota_available: bool
    file_available: bool


class JobEntry(TypedDict):
    ip: str
    state: str
    message: str
    progress: float
    logs: list[str]
    backup: str


class ServiceSnapshot(TypedDict):
    revision: int
    devices: list[DeviceEntry]
    jobs: list[JobEntry]
    busy: str
    devices_locked: bool
    search_progress: str
    release: str
    release_filename: str
    release_url: str
    language: str
    demo: bool
    inventory_error: str
    preferences: dict[str, str | bool]
    auto_refresh_seconds: int
    auto_refresh_paused: bool


class SharedFirmware:
    """One verified download per batch, including a shared failure result."""
    def __init__(self, path: Path | None = None) -> None:
        self.lock = threading.Lock()
        self.path: Path | None = path
        self.error: str | None = None

    def get(self, release: FirmwareRelease) -> Path:
        with self.lock:
            if self.error:
                raise TasmotaError(self.error)
            if self.path is None:
                try:
                    self.path = download_firmware_file(release)
                except Exception as error:
                    self.error = str(error)
                    raise
            return self.path


BACKUP_NOTE = ("Antes de actualizar se guarda una copia independiente por equipo; si falla, ese "
               "equipo no se actualiza. En Safeboot no se hace copia: la configuración no está activa "
               "y se conserva en el equipo.")


def _safeboot_note(status: DeviceStatus) -> str:
    return " — Safeboot, sin copia" if status.version.casefold().endswith("(safeboot)") else ""


@dataclass
class UpdateJob:
    ip: str
    release: FirmwareRelease
    by_file: bool
    state: str = "running"
    message: str = "Preparando copia de configuración"
    progress: float = 0
    logs: list[str] = field(default_factory=list)
    backup: str = ""
    estimate_started: float = 0
    estimate_duration: int = 1
    estimate_start: int = 0
    estimate_end: int = 0
    credentials: tuple[str, str] = field(default=("admin", ""), repr=False)
    firmware: SharedFirmware = field(default_factory=SharedFirmware, repr=False)
    local_name: str = ""


class TasmotaService:
    def __init__(self, *, demo: bool = False, preferences_path: Path | None = None) -> None:
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=16)
        self.events: queue.Queue[tuple[str, str]] = queue.Queue()
        self.username = "admin"
        self.password = ""
        self.language = "Español"
        self.release: FirmwareRelease | None = None
        self.devices: dict[str, DeviceStatus | str] = {}
        self.jobs: dict[str, UpdateJob] = {}
        self.pending: set[str] = set()
        self.busy = ""
        self.busy_exclusive = False
        self.change_serial: dict[str, int] = {}
        self.firmware_directory = Path.home() / "Documents" / "TasmotaFirmware"
        self.search_progress = ""
        self.inventory_error = ""
        self.revision = 0
        self.demo = demo
        self.last_status: dict[str, DeviceStatus] = {}
        self.last_read: dict[str, str] = {}
        self.last_refresh = time.monotonic()
        self.preferences_path = preferences_path or (
            Path.home() / "Documents" / "TasmotaManager" / "preferences.json"
        )
        self.preferences = dict(PREFERENCE_DEFAULTS)
        self.preferences_error = ""
        if not demo:
            try:
                if self.preferences_path.exists():
                    saved = json.loads(self.preferences_path.read_text(encoding="utf-8"))
                    self._validate_preferences(saved)
                    self.preferences.update(saved)
            except (OSError, ValueError, TasmotaError) as error:
                self.preferences_error = f"No se pudieron cargar las preferencias: {error}"
                self._notify("error", self.preferences_error)
        if demo:
            for octet, name in ((110, "Baño"), (111, "Comedor"), (112, "Cocina")):
                ip = f"192.168.1.{octet}"
                self.devices[ip] = DeviceStatus(
                    ip, name, "15.6.0(release-tasmota32)", "ESP32-D0WD-V3",
                    "2T04:20:00", 85, -50,
                    (RelayStatus(1, "Calefacción", "OFF"), RelayStatus(2, "Luz", "ON"),
                     RelayStatus(3, "Intensidad", "ON")), 35, 2057,
                )
            self.release = FirmwareRelease("15.6.0")
            self.last_status = {ip: status for ip, status in self.devices.items()
                                if isinstance(status, DeviceStatus)}
            self.last_read = {ip: time.strftime("%Y-%m-%d %H:%M:%S") for ip in self.devices}
        else:
            try:
                self.devices = {ip: "Sin consultar" for ip in load_device_inventory()}
            except TasmotaError as error:
                self.inventory_error = str(error)
                self.events.put(("error", str(error)))

    def _touch(self) -> None:
        self.revision += 1

    def _notify(self, kind: str, message: str) -> None:
        self.events.put((kind, message))

    @staticmethod
    def _validate_preferences(values: object) -> None:
        if not isinstance(values, dict) or set(values) - set(PREFERENCE_DEFAULTS):
            raise TasmotaError("Formato de preferencias no válido.")
        if "theme" in values and values["theme"] not in ("light", "dark"):
            raise TasmotaError("Tema no válido.")
        if "page" in values and values["page"] not in ("Dispositivos", "Configuración", "Actualizaciones"):
            raise TasmotaError("Página no válida.")
        if "auto_refresh" in values and type(values["auto_refresh"]) is not bool:
            raise TasmotaError("Refresco automático no válido.")

    def set_preferences(self, values: dict[str, str | bool]) -> None:
        self._validate_preferences(values)
        with self.lock:
            if self.preferences_error:
                raise TasmotaError(self.preferences_error + " No se sobrescribirá el archivo.")
            updated = {**self.preferences, **values}
            temp_path = None
            try:
                if not self.demo:
                    self.preferences_path.parent.mkdir(parents=True, exist_ok=True)
                    with tempfile.NamedTemporaryFile(
                        mode="w", encoding="utf-8", dir=self.preferences_path.parent,
                        prefix=".preferences-", suffix=".tmp", delete=False,
                    ) as stream:
                        temp_path = Path(stream.name)
                        json.dump(updated, stream, ensure_ascii=False)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temp_path, self.preferences_path)
                self.preferences = updated
                self.last_refresh = time.monotonic()
                self._touch()
            except OSError as error:
                raise TasmotaError(f"No se guardaron las preferencias: {error}") from None
            finally:
                if temp_path and temp_path.exists():
                    temp_path.unlink()

    def _record_status(self, ip: str, result: DeviceStatus | str) -> None:
        self.devices[ip] = result
        if isinstance(result, DeviceStatus):
            self.last_status[ip] = result
            self.last_read[ip] = time.strftime("%Y-%m-%d %H:%M:%S")

    def auto_refresh_tick(self) -> None:
        with self.lock:
            if (self.demo or not self.preferences["auto_refresh"] or self.busy or self.pending
                    or self.running_updates() or not self.devices):
                return
            if time.monotonic() - self.last_refresh < AUTO_REFRESH_SECONDS:
                return
            self.refresh()

    def backup_directory(self) -> Path:
        return Path.home() / "Documents" / "TasmotaBackups"

    def list_backups(self, ip: str) -> list[BackupEntry]:
        with self.lock:
            if ip not in self.devices:
                raise TasmotaError("Dispositivo no guardado.")
        folder = self.backup_directory()
        try:
            if not folder.exists():
                return []
            rows: list[BackupEntry] = []
            for path in folder.iterdir():
                if path.is_file() and path.suffix.lower() == ".dmp" and f"_{ip}_" in path.name:
                    stat = path.stat()
                    rows.append({"name": path.name, "size": stat.st_size,
                                 "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime))})
            return sorted(rows, key=lambda row: row["modified"], reverse=True)
        except OSError as error:
            raise TasmotaError(f"No se pudieron consultar las copias: {error}") from None

    def selected_release(self) -> FirmwareRelease | None:
        if self.release is None:
            return None
        return FirmwareRelease(
            self.release.version,
            SPANISH_FIRMWARE_URL if self.language == "Español" else FIRMWARE_URL,
        )

    def snapshot(self) -> ServiceSnapshot:
        with self.lock:
            release = self.selected_release()
            rows: list[DeviceEntry] = []
            for ip, result in sorted(self.devices.items(), key=lambda item: int(item[0].split(".")[-1])):
                known = result if isinstance(result, DeviceStatus) else self.last_status.get(ip)
                row: DeviceEntry = {
                    "ip": ip, "name": known.name if known else ip,
                    "version": known.version if known else "",
                    "hardware": known.hardware if known else "",
                    "uptime": known.uptime if known else "",
                    "rssi": known.rssi if known else None,
                    "signal_dbm": known.signal_dbm if known else None,
                    "language_lcid": known.language_lcid if known else None,
                    "relays": [{"number": r.number, "name": r.name, "state": r.state}
                               for r in known.relays] if known else [],
                    "dimmer": known.dimmer if known else None,
                    "error": result if isinstance(result, str) else "",
                    "last_read": self.last_read.get(ip, ""),
                    "available": isinstance(result, DeviceStatus),
                    "pending": ip in self.pending,
                    "updating": ip in self.jobs and self.jobs[ip].state == "running",
                    "ota_available": bool(release and isinstance(result, DeviceStatus)
                                          and firmware_update_available(result, release)),
                    "file_available": bool(release and isinstance(result, DeviceStatus)
                                           and firmware_file_update_available(result, release)),
                }
                rows.append(row)
            jobs: list[JobEntry] = []
            for job in self.jobs.values():
                progress = job.progress
                if job.state == "running" and job.estimate_started:
                    progress = estimated_firmware_progress(
                        time.monotonic() - job.estimate_started, job.estimate_duration,
                        job.estimate_start, job.estimate_end,
                    )
                jobs.append({
                    "ip": job.ip, "state": job.state, "message": job.message,
                    "progress": progress, "logs": list(job.logs), "backup": job.backup,
                })
            return {
                "revision": self.revision, "devices": rows, "jobs": jobs,
                "busy": self.busy, "search_progress": self.search_progress,
                "devices_locked": bool(self.busy and self.busy_exclusive),
                "release": self.release.version if self.release else "",
                "release_filename": release.filename if release else "",
                "release_url": release.url if release else "",
                "language": self.language, "demo": self.demo,
                "inventory_error": self.inventory_error,
                "preferences": dict(self.preferences),
                "auto_refresh_seconds": AUTO_REFRESH_SECONDS,
                "auto_refresh_paused": bool(self.busy or self.pending or self.running_updates()),
            }

    def set_access(self, username: str, password: str) -> None:
        with self.lock:
            self.username = username.strip() or "admin"
            self.password = password
        self._notify("info", "Acceso aplicado. La contraseña se conserva solo en memoria.")

    def set_language(self, language: str) -> None:
        if language not in ("Español", "English"):
            raise TasmotaError("Selecciona Español o English.")
        with self.lock:
            self.language = language
            self._touch()

    def _client(self) -> TasmotaClient:
        return TasmotaClient(self.username, self.password, timeout=12)

    def _launch(
        self, work: Callable[[], None], *, ip: str = "", global_work: str = "", exclusive: bool = True,
    ) -> None:
        with self.lock:
            if self.demo:
                raise TasmotaError("Vista de demostración: no se envían cambios ni peticiones a equipos.")
            if self.busy and (global_work or self.busy_exclusive):
                raise TasmotaError("Espera a que termine la consulta o búsqueda actual.")
            if global_work and (self.pending or self.running_updates()):
                raise TasmotaError("Espera a que terminen los cambios y actualizaciones.")
            if ip and (ip in self.pending or ip in self.running_updates()):
                raise TasmotaError("Este dispositivo ya tiene una operación en curso.")
            if ip:
                self.pending.add(ip)
                self.change_serial[ip] = self.change_serial.get(ip, 0) + 1
            if global_work:
                self.busy = global_work
                self.busy_exclusive = exclusive
            self._touch()

        def run() -> None:
            try:
                work()
            except Exception as error:
                self._notify("error", str(error))
            finally:
                with self.lock:
                    self.pending.discard(ip)
                    if global_work:
                        self.busy = ""
                        self.busy_exclusive = False
                    self._touch()

        self.executor.submit(run)

    def refresh(self) -> None:
        with self.lock:
            ips, user, password = tuple(self.devices), self.username, self.password
            serials = dict(self.change_serial)

        def work() -> None:
            results = scan_devices(ips, user, password)
            with self.lock:
                for ip, result in results.items():
                    # A change started during the scan has a newer reading than this one.
                    if ip in self.pending or self.change_serial.get(ip, 0) != serials.get(ip, 0):
                        continue
                    self._record_status(ip, result)
        self._launch(work, global_work="Consultando dispositivos", exclusive=False)
        self.last_refresh = time.monotonic()

    def discover(self) -> None:
        if self.inventory_error:
            raise TasmotaError(self.inventory_error + " No se sobrescribirá el inventario.")
        user, password = self.username, self.password

        def progress(done: int, total: int) -> None:
            with self.lock:
                self.search_progress = f"{done}/{total} IP"

        def work() -> None:
            result = discover_tasmotas(user, password, progress)
            with self.lock:
                ips = tuple(set(self.devices) | set(result.devices))
            save_device_inventory(ips)
            with self.lock:
                for ip in ips:
                    self._record_status(ip, result.devices.get(ip) or result.errors.get(ip, "No responde"))
            self._notify("info", f"Búsqueda terminada: {len(result.devices)} Tasmotas encontrados.")
        self._launch(work, global_work="Buscando Tasmotas")

    def check_release(self) -> None:
        def work() -> None:
            release = fetch_latest_firmware_release()
            with self.lock:
                self.release = release
        self._launch(work, global_work="Consultando firmware oficial", exclusive=False)

    def change(self, ip: str, operation: str, value: str | int, number: int = 0) -> None:
        with self.lock:
            status = self.devices.get(ip)
            if not isinstance(status, DeviceStatus):
                raise TasmotaError("El dispositivo no está disponible.")
            if operation == "power":
                if value not in ("ON", "OFF") or number not in [r.number for r in status.relays]:
                    raise TasmotaError("Salida o estado no válido.")
                command = f"Power{number} {value}"
            elif operation == "dimmer":
                if type(value) is not int or not 0 <= value <= 100 or status.dimmer is None:
                    raise TasmotaError("La intensidad debe estar entre 0 y 100 en un equipo Dimmer.")
                command = f"Dimmer {value}"
            elif operation in ("name", "relay_name"):
                if not isinstance(value, str):
                    raise TasmotaError("Nombre no válido.")
                try:
                    name = validate_name(value)
                except ValueError as error:
                    raise TasmotaError(str(error)) from None
                if operation == "relay_name" and number not in [r.number for r in status.relays]:
                    raise TasmotaError("Salida no válida.")
                command = f"DeviceName {name}" if operation == "name" else f"FriendlyName{number} {name}"
            else:
                raise TasmotaError("Operación no reconocida.")
            client = self._client()

        def work() -> None:
            client.command(ip, command)
            result = client.read_status(ip)
            with self.lock:
                self._record_status(ip, result)
        self._launch(work, ip=ip)

    def backup(self, ip: str) -> None:
        with self.lock:
            status = self.devices.get(ip)
            if not isinstance(status, DeviceStatus):
                raise TasmotaError("El dispositivo no está disponible.")
            client = self._client()

        def work() -> None:
            path = client.download_configuration_backup(ip, status.name)
            self._notify("info", f"Copia guardada:\n{path}\nPuede contener credenciales; protégela.")
        self._launch(work, ip=ip)

    def running_updates(self) -> set[str]:
        return {ip for ip, job in self.jobs.items() if job.state == "running"}

    def update_targets(self, *, by_file: bool, ip: str = "", local: bool = False) -> list[DeviceStatus]:
        with self.lock:
            if self.busy or self.pending or self.running_updates():
                raise TasmotaError("Espera a que terminen las operaciones en curso.")
            if local:
                targets = [
                    status for address, status in self.devices.items()
                    if (not ip or address == ip) and isinstance(status, DeviceStatus)
                    and local_firmware_compatible(status)
                ]
                if not targets:
                    raise TasmotaError("No hay equipos ESP32 Tasmota disponibles para este archivo.")
                return targets
            release = self.selected_release()
            if release is None:
                raise TasmotaError("Primero busca las actualizaciones.")
            available = firmware_file_update_available if by_file else firmware_update_available
            targets = [
                status for address, status in self.devices.items()
                if (not ip or address == ip) and isinstance(status, DeviceStatus)
                and available(status, release)
            ]
            if not targets:
                raise TasmotaError("No hay equipos pendientes compatibles con el firmware seleccionado.")
            return targets

    def update_confirmation(self, *, by_file: bool, ip: str = "") -> str:
        targets = self.update_targets(by_file=by_file, ip=ip)
        release = self.selected_release()
        assert release is not None
        return (
            f"Instalar Tasmota {release.version} / {release.filename} en:\n\n"
            + "\n".join(f"{s.name} ({s.ip})" + _safeboot_note(s)
                        + (f" — Wi-Fi débil {s.rssi}%" if s.rssi is not None and s.rssi < 60 else "")
                        for s in targets)
            + "\n\n" + BACKUP_NOTE + " Los demás continúan. Reinicio y comprobación de versión e idioma."
            + ("\nDescarga SHA-256 y subida desde el ordenador." if by_file else "\nOTA desde Internet.")
            + ("\nLos dispositivos se actualizarán en paralelo; más tráfico Wi-Fi."
               "\nSe omiten los no disponibles, incompatibles y ya actualizados." if not ip else "")
            + "\n\n¿Continuar?"
        )

    def start_updates(self, *, by_file: bool, ip: str = "") -> None:
        with self.lock:
            if self.demo:
                raise TasmotaError("Vista de demostración: no se actualizan equipos.")
            targets = self.update_targets(by_file=by_file, ip=ip)
            release = self.selected_release()
            assert release is not None
            client_args = (self.username, self.password)
            firmware = SharedFirmware()
            for status in targets:
                job = UpdateJob(status.ip, release, by_file, credentials=client_args, firmware=firmware)
                self.jobs[status.ip] = job
                self.executor.submit(self._update, status, job, client_args)
            self._touch()

    def local_update_confirmation(self, path: str, ip: str = "") -> tuple[str, str]:
        """Validate a selected image and describe the batch; returns text and SHA-256."""
        file = Path(path)
        image, digest = read_local_firmware(file)
        targets = self.update_targets(by_file=True, ip=ip, local=True)
        return (
            f"Instalar el archivo local {file.name}\n"
            f"Tamaño: {len(image) / 1024:.0f} KB\nSHA-256: {digest}\n\nEn:\n\n"
            + "\n".join(f"{s.name} ({s.ip}) — ahora {s.version}" + _safeboot_note(s) for s in targets)
            + "\n\nEl archivo es una imagen OTA válida para ESP32, pero su versión e idioma no "
            "pueden comprobarse de antemano. Se confirmará cuando el equipo vuelva a responder "
            "fuera de Safeboot y se registrará la versión instalada."
            "\n" + BACKUP_NOTE
            + ("\nLos dispositivos se actualizarán en paralelo." if not ip else "")
            + "\n\n¿Continuar?",
            digest,
        )

    def start_local_updates(self, path: str, digest: str, ip: str = "") -> None:
        file = Path(path)
        with self.lock:
            if self.demo:
                raise TasmotaError("Vista de demostración: no se actualizan equipos.")
            targets = self.update_targets(by_file=True, ip=ip, local=True)
            image, actual = read_local_firmware(file)
            if actual != digest:
                raise TasmotaError("El archivo cambió después de confirmarlo; vuelve a seleccionarlo.")
            # Upload a private copy so the confirmed image cannot change during the batch.
            folder = self.firmware_directory
            folder.mkdir(parents=True, exist_ok=True)
            copy = folder / f"local-{digest[:16]}.bin"
            if not copy.exists() or copy.read_bytes() != image:
                temp = copy.with_suffix(".tmp")
                temp.write_bytes(image)
                os.replace(temp, copy)
            release = self.selected_release() or FirmwareRelease("0.0.0")
            client_args = (self.username, self.password)
            firmware = SharedFirmware(copy)
            for status in targets:
                job = UpdateJob(status.ip, release, True, credentials=client_args,
                                firmware=firmware, local_name=file.name)
                self.jobs[status.ip] = job
                self.executor.submit(self._update, status, job, client_args)
            self._touch()

    def _log(self, job: UpdateJob, message: str, progress: int | None = None) -> None:
        with self.lock:
            job.message = message
            job.logs.append(f"{time.strftime('%H:%M:%S')}  {message}")
            if progress is not None:
                job.progress = progress
                job.estimate_started = 0
            self._touch()

    def _wait_phase(self, job: UpdateJob, seconds: int, start: int, end: int, duration: int) -> float:
        with self.lock:
            job.estimate_started = time.monotonic()
            job.estimate_duration = duration
            job.estimate_start = start
            job.estimate_end = end
            self._touch()
        deadline = time.monotonic() + OTA_TIMEOUT_SECONDS
        time.sleep(seconds)
        return deadline

    def _update(
        self, status: DeviceStatus, job: UpdateJob, client_args: tuple[str, str],
    ) -> None:
        with self.lock:
            job.credentials = client_args
        client = TasmotaClient(*client_args, timeout=12)
        try:
            if status.version.casefold().endswith("(safeboot)"):
                # Safeboot only holds the minimal recovery image; its backup is not useful.
                self._log(job, "Equipo en Safeboot: se omite la copia de configuración", 10)
            else:
                self._log(job, "Guardando copia antes de actualizar", 3)
                backup = client.download_configuration_backup(status.ip, status.name)
                if not isinstance(backup, Path):
                    raise TasmotaError("No se confirmó la copia; no se inicia el firmware.")
                with self.lock:
                    job.backup = str(backup)
                self._log(job, f"Copia disponible: {backup}", 10)
            if job.by_file:
                if job.local_name:
                    self._log(job, f"Usando archivo local verificado {job.local_name}", 20)
                else:
                    self._log(job, f"Descargando {job.release.filename} y verificando SHA-256", 20)
                image = job.firmware.get(job.release)
                self._log(job, f"Imagen verificada: {image}. Preparando Safeboot", 40)
                client.prepare_firmware_upload(status.ip)
                self._log(job, "Subiendo firmware; no se repetirá la subida", 60)
                try:
                    client.upload_firmware_file(status.ip, image)
                    self._log(job, "Archivo aceptado. Esperando reinicio", 60)
                except FirmwareUploadUncertain as error:
                    self._log(job, f"{error} Verificando sin reenviar", 60)
                deadline = self._wait_phase(job, 30, 60, 95, 60)
            else:
                client.command(status.ip, f"OtaUrl {job.release.url}")
                self._log(job, "Enviando Upgrade 1", 10)
                try:
                    client.command(status.ip, "Upgrade 1")
                except TasmotaError as error:
                    self._log(job, f"Orden sin confirmar: {error}. No se repite hasta detectar Safeboot.")
                deadline = self._wait_phase(job, 20, 10, 35, 20)
            second_sent = False
            while time.monotonic() < deadline:
                try:
                    current = client.read_status(status.ip)
                except TasmotaError as error:
                    with self.lock:
                        self._record_status(status.ip, str(error))
                    self._log(job, f"Esperando reinicio: {error}")
                else:
                    with self.lock:
                        self._record_status(status.ip, current)
                        self._touch()
                    if job.local_name and local_firmware_compatible(current) and not (
                            current.version.casefold().endswith("(safeboot)")):
                        self._log(job, f"Instalación confirmada: {current.version} desde {job.local_name}", 100)
                        with self.lock:
                            job.state = "success"
                        return
                    if not job.local_name and firmware_release_installed(current, job.release):
                        self._log(job, f"Instalación confirmada: {current.version} / {job.release.filename}", 100)
                        with self.lock:
                            job.state = "success"
                        return
                    if (not job.by_file and not second_sent
                            and current.version.casefold().endswith("(safeboot)")
                            and "esp32-d0wd-v3" in current.hardware.casefold()):
                        second_sent = True
                        self._log(job, "Safeboot detectado. Segunda orden Upgrade 1, una sola vez", 35)
                        try:
                            client.command(status.ip, "Upgrade 1")
                        except TasmotaError as error:
                            self._log(job, f"Segunda orden sin confirmar: {error}; no se repite.")
                        deadline = self._wait_phase(job, 60, 35, 95, 90)
                        continue
                    self._log(job, f"Responde {current.version}. Esperando versión e idioma seleccionados")
                time.sleep(30)
            raise TasmotaError("No se confirmó versión e idioma tras seis minutos por fase.")
        except Exception as error:
            self._log(job, str(error))
            with self.lock:
                job.state = "error"
            self._notify("error", f"{status.name} ({status.ip}): {error}")
        finally:
            with self.lock:
                self._touch()

    def close(self) -> bool:
        with self.lock:
            if self.running_updates() or self.pending or self.busy:
                return False
        self.executor.shutdown(wait=False, cancel_futures=True)
        return True

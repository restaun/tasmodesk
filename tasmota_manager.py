"""Shared HTTP client, inventory and firmware validation for TasmoDesk."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DISCOVERY_IPS = tuple(f"192.168.1.{octet}" for octet in range(1, 255))
DISCOVERY_WORKERS = 16
DISCOVERY_TIMEOUT_SECONDS = 2
REQUEST_TIMEOUT_SECONDS = 4
MAX_BACKUP_SIZE = 2 * 1024 * 1024
MIN_BACKUP_SIZE = 128
MAX_FIRMWARE_SIZE = 4 * 1024 * 1024
OTA_POLL_INTERVAL_MS = 30_000
OTA_FIRST_WAIT_MS = 20_000
OTA_SECOND_WAIT_MS = 60_000
FIRMWARE_FILE_WAIT_MS = 30_000
OTA_TIMEOUT_SECONDS = 6 * 60
RELEASE_PAGE_URL = "https://ota.tasmota.com/tasmota32/release/"
FIRMWARE_URL = "http://ota.tasmota.com/tasmota32/release/tasmota32.bin"
SPANISH_FIRMWARE_URL = "http://ota.tasmota.com/tasmota32/release/tasmota32-ES.bin"
POWER_KEY = re.compile(r"^POWER([1-9]\d*)$", re.IGNORECASE)
VERSION_PATTERN = re.compile(r"^(\d+)\.(\d+)\.(\d+)")
RELEASE_PATTERN = re.compile(
    r"Release binaries for Tasmota firmware\s+(\d+\.\d+\.\d+)\s+on ESP32",
    re.IGNORECASE,
)


class TasmotaError(Exception):
    """An HTTP, JSON, or Tasmota command error."""


class FirmwareUploadUncertain(TasmotaError):
    """The connection ended without confirming whether the upload completed."""


class WebRequestUnavailable(TasmotaError):
    """A WebUI request failed because the device could not be reached."""


@dataclass(frozen=True)
class RelayStatus:
    number: int
    name: str
    state: str


@dataclass(frozen=True)
class DeviceStatus:
    ip: str
    name: str
    version: str
    hardware: str
    uptime: str
    rssi: int | None
    signal_dbm: int | None
    relays: tuple[RelayStatus, ...]
    dimmer: int | None = None
    language_lcid: int | None = None




@dataclass(frozen=True)
class FirmwareRelease:
    version: str
    url: str = FIRMWARE_URL

    @property
    def filename(self) -> str:
        if self.url == FIRMWARE_URL:
            return "tasmota32.bin"
        if self.url == SPANISH_FIRMWARE_URL:
            return "tasmota32-ES.bin"
        raise TasmotaError("La URL del firmware no coincide con una imagen aprobada.")


def firmware_language_matches(status: DeviceStatus, release: FirmwareRelease) -> bool:
    expected = 1034 if release.filename == "tasmota32-ES.bin" else 2057
    return status.language_lcid == expected


class _PageTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def parse_latest_firmware_release(page: str) -> FirmwareRelease:
    """Read the ESP32 stable version from the official OTA release index."""
    parser = _PageTextParser()
    parser.feed(page)
    text = " ".join(parser.parts)
    match = RELEASE_PATTERN.search(text)
    if match is None:
        raise TasmotaError(
            "No se pudo identificar la versión estable ESP32 en la página oficial."
        )
    return FirmwareRelease(version=match.group(1))


def fetch_latest_firmware_release(
    timeout: int = 12,
) -> FirmwareRelease:
    try:
        with urlopen(RELEASE_PAGE_URL, timeout=timeout) as response:
            page = response.read().decode("utf-8", errors="replace")
    except (TimeoutError, URLError, OSError) as error:
        reason = getattr(error, "reason", error)
        raise TasmotaError(f"No se pudo consultar la página oficial: {reason}") from None
    return parse_latest_firmware_release(page)


def firmware_update_available(
    status: DeviceStatus,
    release: FirmwareRelease,
) -> bool:
    """Only offer the generic ESP32 release to matching ESP32 Tasmota builds."""
    if "esp32-d0wd-v3" not in status.hardware.casefold():
        return False
    if "release-tasmota32" not in status.version.casefold():
        return False
    current = VERSION_PATTERN.match(status.version)
    latest = VERSION_PATTERN.match(release.version)
    return bool(
        current
        and latest
        and (
            tuple(map(int, current.groups())) < tuple(map(int, latest.groups()))
            or (
                tuple(map(int, current.groups())) == tuple(map(int, latest.groups()))
                and status.language_lcid is not None
                and not firmware_language_matches(status, release)
            )
        )
    )


def firmware_file_update_available(
    status: DeviceStatus, release: FirmwareRelease
) -> bool:
    return firmware_update_available(status, release) or (
        "esp32-d0wd-v3" in status.hardware.casefold()
        and status.version.casefold().endswith("(safeboot)")
    )


def validate_esp32_firmware(image: bytes) -> None:
    if not 128 * 1024 <= len(image) <= MAX_FIRMWARE_SIZE:
        raise TasmotaError("El tamaño de la imagen ESP32 no es válido.")
    if (
        image[0] != 0xE9
        or int.from_bytes(image[12:14], "little") != 0
        or image[32:36] != b"\x32\x54\xcd\xab"
    ):
        raise TasmotaError("El archivo no es una imagen OTA para el ESP32 clásico.")


def read_local_firmware(path: Path) -> tuple[bytes, str]:
    """Read a user-selected OTA image and return it with its SHA-256."""
    if path.suffix.casefold() != ".bin":
        raise TasmotaError("Selecciona un archivo de firmware .bin.")
    try:
        with path.open("rb") as stream:
            image = stream.read(MAX_FIRMWARE_SIZE + 1)
    except OSError as error:
        raise TasmotaError(f"No se pudo leer el firmware seleccionado: {error}") from None
    validate_esp32_firmware(image)
    return image, hashlib.sha256(image).hexdigest()


def local_firmware_compatible(status: DeviceStatus) -> bool:
    version = status.version.casefold()
    return "esp32-d0wd-v3" in status.hardware.casefold() and (
        "tasmota32" in version or version.endswith("(safeboot)")
    )


def validate_existing_backup(path: Path) -> Path:
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_BACKUP_SIZE + 1)
    except OSError as error:
        raise TasmotaError(f"No se pudo leer la copia seleccionada: {error}") from None
    if (
        path.suffix.casefold() != ".dmp"
        or not MIN_BACKUP_SIZE <= len(data) <= MAX_BACKUP_SIZE
        or data.lstrip().lower().startswith((b"<", b"{", b"["))
    ):
        raise TasmotaError("Selecciona una copia binaria .dmp de este dispositivo.")
    return path


def download_firmware_file(
    release: FirmwareRelease, directory: Path | None = None
) -> Path:
    """Fetch the versioned official asset and verify its published digest."""
    if re.fullmatch(r"\d+\.\d+\.\d+", release.version) is None:
        raise TasmotaError("La versión de firmware no es válida.")
    asset_url = (
        "https://github.com/arendst/Tasmota/releases/download/"
        f"v{release.version}/{release.filename}"
    )
    metadata_url = (
        "https://api.github.com/repos/arendst/Tasmota/releases/tags/"
        f"v{release.version}"
    )
    temp_path: Path | None = None
    try:
        with urlopen(metadata_url, timeout=30) as response:
            metadata = json.loads(response.read(2 * 1024 * 1024))
        if not isinstance(metadata, dict) or metadata.get("tag_name") != f"v{release.version}":
            raise TasmotaError("No se confirmó la versión en la publicación oficial.")
        assets = metadata.get("assets")
        if not isinstance(assets, list):
            raise TasmotaError("La publicación oficial no contiene una lista de archivos.")
        asset = next(
            (
                item for item in assets
                if isinstance(item, dict) and item.get("name") == release.filename
            ),
            None,
        )
        if asset is None or asset.get("browser_download_url") != asset_url:
            raise TasmotaError(f"No se encontró la imagen OTA oficial {release.filename}.")
        digest = asset.get("digest")
        if not isinstance(digest, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
            raise TasmotaError("La publicación no proporciona un SHA-256 verificable.")
        with urlopen(asset_url, timeout=60) as response:
            image = response.read(MAX_FIRMWARE_SIZE + 1)
        validate_esp32_firmware(image)
        if len(image) != asset.get("size") or hashlib.sha256(image).hexdigest() != digest[7:]:
            raise TasmotaError("La descarga no coincide con el tamaño o SHA-256 oficial.")
        folder = directory or Path.home() / "Documents" / "TasmotaFirmware"
        folder.mkdir(parents=True, exist_ok=True)
        destination = folder / f"{Path(release.filename).stem}-v{release.version}.bin"
        with tempfile.NamedTemporaryFile(dir=folder, suffix=".tmp", delete=False) as stream:
            temp_path = Path(stream.name)
            stream.write(image)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, destination)
        return destination
    except (OSError, URLError, ValueError) as error:
        raise TasmotaError(f"No se pudo descargar el firmware oficial: {error}") from None
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def firmware_release_installed(
    status: DeviceStatus,
    release: FirmwareRelease,
) -> bool:
    """Return true when a supported device reports the target or a newer release."""
    if "esp32-d0wd-v3" not in status.hardware.casefold():
        return False
    if "release-tasmota32" not in status.version.casefold():
        return False
    current = VERSION_PATTERN.match(status.version)
    target = VERSION_PATTERN.match(release.version)
    return bool(
        current
        and target
        and tuple(map(int, current.groups())) >= tuple(map(int, target.groups()))
        and firmware_language_matches(status, release)
    )


def build_command_url(
    ip: str,
    command: str,
    username: str = "",
    password: str = "",
) -> str:
    """Build an encoded local Tasmota HTTP command URL."""
    parameters: dict[str, str] = {}
    if password:
        parameters["user"] = username or "admin"
        parameters["password"] = password
    parameters["cmnd"] = command
    return f"http://{ip}/cm?{urlencode(parameters)}"


def parse_device_status(
    ip: str,
    status_response: dict[str, object],
    relay_response: dict[str, object],
) -> DeviceStatus:
    """Extract the inventory fields from Tasmota Status 0 and Status 11."""
    status = status_response.get("Status")
    firmware = status_response.get("StatusFWR")
    parameters = status_response.get("StatusPRM")
    state = relay_response.get("StatusSTS")
    if not all(isinstance(value, dict) for value in (status, firmware, state)):
        raise TasmotaError("Respuesta incompleta: el dispositivo no parece Tasmota.")

    if not isinstance(status, dict) or not isinstance(firmware, dict):
        raise TasmotaError("Respuesta incompleta: el dispositivo no parece Tasmota.")
    if not isinstance(state, dict):
        raise TasmotaError("Respuesta incompleta: no se recibió el estado de las salidas.")
    parameters = parameters if isinstance(parameters, dict) else {}

    wifi = state.get("Wifi")
    wifi = wifi if isinstance(wifi, dict) else {}
    friendly_names = status.get("FriendlyName")
    if not isinstance(friendly_names, list):
        friendly_names = []

    relays: list[RelayStatus] = []
    for key, value in state.items():
        match = POWER_KEY.fullmatch(str(key))
        if match:
            number = int(match.group(1))
            friendly_index = number - 1
            name = (
                str(friendly_names[friendly_index]).strip()
                if friendly_index < len(friendly_names)
                else ""
            )
            relays.append(
                RelayStatus(
                    number=number,
                    name=name or f"Salida {number}",
                    state=str(value).upper(),
                )
            )
    relays.sort(key=lambda relay: relay.number)

    rssi = wifi.get("RSSI")
    signal_dbm = wifi.get("Signal")
    dimmer = state.get("Dimmer")
    if dimmer is not None and (
        not isinstance(dimmer, int) or isinstance(dimmer, bool) or not 0 <= dimmer <= 100
    ):
        raise TasmotaError("El dispositivo devolvió una intensidad Dimmer no válida.")
    memory = status_response.get("StatusMEM")
    features = memory.get("Features") if isinstance(memory, dict) else None
    language_lcid = None
    if isinstance(features, list) and features:
        locale = features[0]
        # Tasmota 12.x (Safeboot) reports 8 hex digits, e.g. "00000809"; newer builds use 4.
        if not isinstance(locale, str) or re.fullmatch(r"(?:0000)?[0-9A-Fa-f]{4}", locale) is None:
            raise TasmotaError("El dispositivo devolvió un código de idioma no válido.")
        language_lcid = int(locale, 16)
    return DeviceStatus(
        ip=ip,
        name=str(status.get("DeviceName") or "Tasmota"),
        version=str(firmware.get("Version") or "desconocida"),
        hardware=str(firmware.get("Hardware") or "desconocido"),
        uptime=str(parameters.get("Uptime") or state.get("Uptime") or "desconocido"),
        rssi=int(rssi) if isinstance(rssi, (int, float)) else None,
        signal_dbm=int(signal_dbm) if isinstance(signal_dbm, (int, float)) else None,
        relays=tuple(relays),
        dimmer=dimmer,
        language_lcid=language_lcid,
    )


class TasmotaClient:
    def __init__(
        self,
        username: str = "admin",
        password: str = "",
        timeout: int = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self.username = username
        self.password = password
        self.timeout = timeout

    def command(self, ip: str, command: str) -> dict[str, object]:
        if any(character in command for character in ("\r", "\n", "\x00")):
            raise TasmotaError("El comando contiene caracteres no permitidos.")

        url = build_command_url(ip, command, self.username, self.password)
        try:
            with urlopen(url, timeout=self.timeout) as response:
                body = response.read()
        except HTTPError as error:
            if error.code in (401, 403):
                raise TasmotaError(
                    f"{ip}: acceso denegado; revisa el usuario y la contraseña."
                ) from None
            raise TasmotaError(f"{ip}: error HTTP {error.code}.") from None
        except (TimeoutError, URLError, OSError) as error:
            reason = getattr(error, "reason", error)
            raise TasmotaError(f"{ip}: no responde ({reason}).") from None

        try:
            result = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise TasmotaError(f"{ip}: la respuesta no es JSON válido.") from None
        if not isinstance(result, dict):
            raise TasmotaError(f"{ip}: respuesta Tasmota no válida.")
        if str(result.get("Command", "")).lower() in {"unknown", "error"}:
            detail = result.get("Input") or result.get("Command")
            raise TasmotaError(f"{ip}: Tasmota rechazó el comando ({detail}).")
        return result

    def read_status(self, ip: str) -> DeviceStatus:
        status_response = self.command(ip, "Status 0")
        relay_response = self.command(ip, "Status 11")
        return parse_device_status(ip, status_response, relay_response)

    def download_configuration_backup(
        self,
        ip: str,
        device_name: str,
        backup_directory: Path | None = None,
    ) -> Path:
        """Download Tasmota's WebUI .dmp backup and save it atomically."""
        url = f"http://{ip}/dl"
        headers = {"Referer": f"http://{ip}/"}
        if self.password:
            credentials = f"{self.username or 'admin'}:{self.password}".encode(
                "utf-8"
            )
            token = base64.b64encode(credentials).decode("ascii")
            headers["Authorization"] = f"Basic {token}"
        request = Request(url, headers=headers)

        directory = backup_directory or Path.home() / "Documents" / "TasmotaBackups"
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise TasmotaError(
                f"No se pudo crear la carpeta de copias «{directory}»: {error}"
            ) from None

        safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", device_name).strip("_")
        safe_name = safe_name or "Tasmota"
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        destination = directory / f"{safe_name}_{ip}_{timestamp}.dmp"
        temp_path: Path | None = None
        try:
            with urlopen(request, timeout=15) as response:
                content_type = response.headers.get_content_type()
                if content_type in {"text/html", "application/json", "text/plain"}:
                    raise TasmotaError(
                        f"{ip}: /dl devolvió {content_type}, no un respaldo."
                    )
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > MAX_BACKUP_SIZE:
                    raise TasmotaError(
                        f"{ip}: el archivo de respaldo es demasiado grande."
                    )

                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    prefix=".tasmota-backup-",
                    suffix=".tmp",
                    dir=directory,
                    delete=False,
                ) as temp_file:
                    temp_path = Path(temp_file.name)
                    total_size = 0
                    first_bytes = b""
                    while chunk := response.read(64 * 1024):
                        total_size += len(chunk)
                        if total_size > MAX_BACKUP_SIZE:
                            raise TasmotaError(
                                f"{ip}: el archivo de respaldo supera el límite permitido."
                            )
                        if len(first_bytes) < 32:
                            first_bytes += chunk[: 32 - len(first_bytes)]
                        temp_file.write(chunk)
                    if total_size < MIN_BACKUP_SIZE:
                        raise TasmotaError(
                            f"{ip}: la descarga del respaldo está vacía o incompleta."
                        )
                    if first_bytes.lstrip().lower().startswith(
                        (b"<!doctype html", b"<html", b"{", b"[")
                    ):
                        raise TasmotaError(
                            f"{ip}: el servidor devolvió una página de error, no un respaldo."
                        )
                    temp_file.flush()
                    os.fsync(temp_file.fileno())

            os.replace(temp_path, destination)
            return destination
        except HTTPError as error:
            if error.code in (401, 403):
                raise TasmotaError(
                    f"{ip}: acceso denegado al respaldo; revisa usuario y contraseña."
                ) from None
            raise TasmotaError(
                f"{ip}: error HTTP {error.code} al guardar el respaldo."
            ) from None
        except (TimeoutError, URLError, OSError, ValueError) as error:
            reason = getattr(error, "reason", error)
            raise TasmotaError(
                f"{ip}: no se pudo guardar el respaldo ({reason})."
            ) from None
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()

    def start_firmware_upgrade(self, ip: str, firmware_url: str) -> None:
        if firmware_url not in (FIRMWARE_URL, SPANISH_FIRMWARE_URL):
            raise TasmotaError("La URL del firmware no coincide con la imagen aprobada.")
        self.command(ip, f"OtaUrl {firmware_url}")
        self.command(ip, "Upgrade 1")

    def _web_request(
        self, ip: str, path: str, data: bytes | None = None,
        content_type: str | None = None, timeout: int = 12,
    ) -> str:
        headers = {"Referer": f"http://{ip}/up"}
        if self.password:
            token = base64.b64encode(
                f"{self.username or 'admin'}:{self.password}".encode("utf-8")
            ).decode("ascii")
            headers["Authorization"] = f"Basic {token}"
        if content_type is not None:
            headers["Content-Type"] = content_type
        try:
            with urlopen(Request(f"http://{ip}{path}", data=data, headers=headers), timeout=timeout) as response:
                return response.read(256 * 1024).decode("utf-8", errors="replace")
        except HTTPError as error:
            raise TasmotaError(f"{ip}: error HTTP {error.code} en {path}.") from None
        except (OSError, URLError) as error:
            if data is not None:
                raise FirmwareUploadUncertain(
                    f"{ip}: conexión interrumpida durante la subida ({error})."
                ) from None
            raise WebRequestUnavailable(
                f"{ip}: no respondió la WebUI en {path} ({error})."
            ) from None

    def prepare_firmware_upload(self, ip: str) -> None:
        page = self._web_request(ip, "/up")
        if re.search(r"name=['\"]u2['\"]", page) is None:
            raise TasmotaError(f"{ip}: la WebUI no ofrece la subida de firmware.")
        if "return upl(this)" not in page:
            return
        # Follow the same factory-partition handshake used by Tasmota's WebUI.
        deadline = time.monotonic() + 90
        last_error = ""
        while time.monotonic() < deadline:
            try:
                result = self._web_request(ip, "/u4?u4=fct&api=").strip()
            except WebRequestUnavailable as error:
                last_error = str(error)
            else:
                if result == "true":
                    time.sleep(1)
                    self._web_request(ip, "/up")
                    return
                if result != "false":
                    raise TasmotaError(f"{ip}: respuesta inesperada al preparar Safeboot.")
            time.sleep(2)
        raise TasmotaError(f"{ip}: no se confirmó Safeboot tras 90 segundos. {last_error}")

    def upload_firmware_file(self, ip: str, firmware_path: Path) -> None:
        try:
            with firmware_path.open("rb") as stream:
                image = stream.read(MAX_FIRMWARE_SIZE + 1)
        except OSError as error:
            raise TasmotaError(f"No se pudo leer el firmware: {error}") from None
        validate_esp32_firmware(image)
        boundary = uuid.uuid4().hex
        body = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="u2"; '
            'filename="tasmota32.bin"\r\nContent-Type: application/octet-stream\r\n\r\n'
        ).encode("ascii") + image + f"\r\n--{boundary}--\r\n".encode("ascii")
        page = self._web_request(
            ip, f"/u2?fsz={len(image)}", data=body,
            content_type=f"multipart/form-data; boundary={boundary}", timeout=120,
        )
        parser = _PageTextParser()
        parser.feed(page)
        text = " ".join(" ".join(parser.parts).split())
        if re.search(r"\b(?:Upload\s+Successful|Carga\s+Exitosa)\b", text, re.IGNORECASE) is None:
            raise TasmotaError(
                f"{ip}: la WebUI no confirmó «Upload Successful». "
                f"Respuesta: {text[-1000:]}"
            )


def scan_devices(
    ips: tuple[str, ...],
    username: str,
    password: str,
) -> dict[str, DeviceStatus | str]:
    """Scan devices concurrently; preserve a per-IP error for offline devices."""
    results: dict[str, DeviceStatus | str] = {}
    if not ips:
        return results

    def scan(ip: str) -> DeviceStatus:
        return TasmotaClient(username, password).read_status(ip)

    with ThreadPoolExecutor(max_workers=min(DISCOVERY_WORKERS, len(ips))) as executor:
        futures = {executor.submit(scan, ip): ip for ip in ips}
        for future in as_completed(futures):
            ip = futures[future]
            try:
                results[ip] = future.result()
            except TasmotaError as error:
                results[ip] = str(error)
    return results


@dataclass(frozen=True)
class DiscoveryResult:
    devices: dict[str, DeviceStatus]
    errors: dict[str, str]


def discover_tasmotas(
    username: str, password: str,
    progress: Callable[[int, int], None] | None = None,
    ips: tuple[str, ...] = DISCOVERY_IPS,
) -> DiscoveryResult:
    devices: dict[str, DeviceStatus] = {}
    errors: dict[str, str] = {}

    def probe(ip: str) -> DeviceStatus:
        client = TasmotaClient(username, password, timeout=DISCOVERY_TIMEOUT_SECONDS)
        response = client.command(ip, "Status 0")
        firmware = response.get("StatusFWR")
        if (
            not isinstance(firmware, dict)
            or not isinstance(firmware.get("Version"), str)
            or VERSION_PATTERN.match(firmware["Version"]) is None
        ):
            raise TasmotaError(f"{ip}: no se identificó firmware Tasmota.")
        return parse_device_status(ip, response, client.command(ip, "Status 11"))

    with ThreadPoolExecutor(max_workers=DISCOVERY_WORKERS) as executor:
        futures = {executor.submit(probe, ip): ip for ip in ips}
        for completed, future in enumerate(as_completed(futures), 1):
            ip = futures[future]
            try:
                devices[ip] = future.result()
            except TasmotaError as error:
                errors[ip] = str(error)
            if progress is not None:
                progress(completed, len(ips))
    return DiscoveryResult(devices, errors)


def device_inventory_path() -> Path:
    return Path.home() / "Documents" / "TasmotaManager" / "devices.json"


def validate_inventory(ips: object) -> tuple[str, ...]:
    if not isinstance(ips, list) or any(
        not isinstance(ip, str) or ip not in DISCOVERY_IPS for ip in ips
    ):
        raise TasmotaError("La lista guardada contiene direcciones no válidas.")
    return tuple(sorted(set(ips), key=lambda ip: int(ip.rsplit(".", 1)[1])))


def load_device_inventory(path: Path | None = None) -> tuple[str, ...]:
    source = path if path is not None else device_inventory_path()
    try:
        with source.open("r", encoding="utf-8") as stream:
            data = json.load(stream)
    except FileNotFoundError:
        return ()
    except (OSError, ValueError) as error:
        raise TasmotaError(f"No se pudo leer la lista «{source}»: {error}") from None
    if not isinstance(data, dict) or data.get("schema") != 1:
        raise TasmotaError(f"Formato de lista no reconocido en «{source}».")
    return validate_inventory(data.get("devices"))


def save_device_inventory(ips: tuple[str, ...], path: Path | None = None) -> None:
    devices = validate_inventory(list(ips))
    destination = path if path is not None else device_inventory_path()
    temp_path: Path | None = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=destination.parent,
            suffix=".tmp", delete=False,
        ) as stream:
            temp_path = Path(stream.name)
            json.dump({"schema": 1, "devices": devices}, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, destination)
    except OSError as error:
        raise TasmotaError(f"No se pudo guardar la lista «{destination}»: {error}") from None
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def backup_before_firmware_upgrade(
    client: TasmotaClient,
    ip: str,
    device_name: str,
    firmware_url: str,
) -> Path:
    """Never initiate OTA unless the configuration backup was saved."""
    backup_path = client.download_configuration_backup(ip, device_name)
    try:
        client.start_firmware_upgrade(ip, firmware_url)
    except TasmotaError as error:
        raise TasmotaError(
            f"{error}\nLa copia quedó guardada en: {backup_path}"
        ) from None
    return backup_path


def estimated_firmware_progress(
    elapsed: float, duration: float, start: float, end: float
) -> float:
    return start + (end - start) * min(1.0, max(0.0, elapsed / duration))

def validate_name(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("El nombre no puede estar vacío.")
    if any(character in value for character in ("\r", "\n", "\x00", ";")):
        raise ValueError("El nombre contiene caracteres no permitidos.")
    if len(value) > 64:
        raise ValueError("El nombre no puede superar 64 caracteres.")
    return value











import base64
import hashlib
import io
import json
import unittest
from email.message import Message
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, call, patch

from tasmota_manager import (
    DISCOVERY_RETRY_TIMEOUT_SECONDS,
    REQUEST_TIMEOUT_SECONDS,
    DeviceTimeout,
    DeviceStatus,
    FirmwareRelease,
    RelayStatus,
    TasmotaError,
    TasmotaClient,
    FirmwareUploadUncertain,
    WebRequestUnavailable,
    backup_before_firmware_upgrade,
    build_command_url,
    firmware_update_available,
    firmware_release_installed,
    firmware_file_update_available,
    download_firmware_file,
    validate_esp32_firmware,
    validate_existing_backup,
    estimated_firmware_progress,
    DISCOVERY_IPS,
    FIRMWARE_URL,
    SPANISH_FIRMWARE_URL,
    DiscoveryResult,
    discover_tasmotas,
    load_device_inventory,
    save_device_inventory,
    scan_devices,
    parse_latest_firmware_release,
    parse_device_status,
)


class DeviceDiscoveryTests(unittest.TestCase):
    def test_search_covers_entire_selected_subnet(self) -> None:
        self.assertEqual(len(DISCOVERY_IPS), 254)
        self.assertEqual(DISCOVERY_IPS[0], "192.168.1.1")
        self.assertEqual(DISCOVERY_IPS[-1], "192.168.1.254")

    def test_discovers_only_valid_tasmotas_with_read_only_commands(self) -> None:
        def response(client: TasmotaClient, ip: str, command: str) -> dict[str, object]:
            if ip.endswith(".2"):
                return {"status": "ok"}
            if ip.endswith(".3"):
                raise TasmotaError("acceso denegado")
            if command == "Status 0":
                return {"Status": {"DeviceName": "Prueba"},
                        "StatusFWR": {"Version": "15.6.0(release-tasmota32)"}}
            return {"StatusSTS": {}}

        progress = Mock()
        ips = ("192.168.1.1", "192.168.1.2", "192.168.1.3")
        with patch.object(TasmotaClient, "command", autospec=True, side_effect=response) as command:
            result = discover_tasmotas("admin", "", progress, ips)
        self.assertEqual(set(result.devices), {"192.168.1.1"})
        self.assertEqual(set(result.errors), {"192.168.1.2", "192.168.1.3"})
        self.assertEqual(progress.call_count, 3)
        progress.assert_called_with(3, 3)
        self.assertTrue(all(item.args[2] in {"Status 0", "Status 11"} for item in command.call_args_list))

    def test_discovery_retries_timed_out_ips_with_longer_timeout(self) -> None:
        attempts: dict[str, int] = {}

        def response(client: TasmotaClient, ip: str, command: str) -> dict[str, object]:
            if command == "Status 0":
                attempts[ip] = attempts.get(ip, 0) + 1
                if ip.endswith(".4"):
                    raise TasmotaError("rechazado")
                if attempts[ip] == 1 or ip.endswith(".3"):
                    raise DeviceTimeout("timed out")
                return {"Status": {"DeviceName": "Lento"},
                        "StatusFWR": {"Version": "12.1.0(safeboot)"}}
            return {"StatusSTS": {}}

        progress = Mock()
        ips = ("192.168.1.2", "192.168.1.3", "192.168.1.4")
        with patch.object(TasmotaClient, "command", autospec=True, side_effect=response) as command:
            result = discover_tasmotas("admin", "", progress, ips, known=("192.168.1.2",))
        self.assertEqual(set(result.devices), {"192.168.1.2"})
        self.assertEqual(set(result.errors), {"192.168.1.3", "192.168.1.4"})
        self.assertEqual(attempts, {"192.168.1.2": 2, "192.168.1.3": 2, "192.168.1.4": 1})
        progress.assert_called_with(5, 5)
        timeouts = {(item.args[1], item.args[0].timeout) for item in command.call_args_list}
        self.assertIn(("192.168.1.2", REQUEST_TIMEOUT_SECONDS), timeouts)
        self.assertIn(("192.168.1.3", DISCOVERY_RETRY_TIMEOUT_SECONDS), timeouts)

    def test_refresh_retries_once_after_timeout(self) -> None:
        status = Mock()
        with patch.object(TasmotaClient, "read_status", autospec=True,
                          side_effect=[DeviceTimeout("timed out"), status]) as read:
            self.assertEqual(scan_devices(("192.168.1.2",), "admin", ""), {"192.168.1.2": status})
        self.assertEqual(read.call_count, 2)

    def test_inventory_starts_empty_and_persists_sorted_unique_ips(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "devices.json"
            self.assertEqual(load_device_inventory(path), ())
            save_device_inventory(("192.168.1.20", "192.168.1.2", "192.168.1.20"), path)
            self.assertEqual(load_device_inventory(path), ("192.168.1.2", "192.168.1.20"))
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])
            self.assertNotIn("password", path.read_text())

    def test_inventory_rejects_invalid_and_corrupt_content_without_overwriting(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "devices.json"
            path.write_text("corrupt")
            with self.assertRaises(TasmotaError):
                load_device_inventory(path)
            with self.assertRaises(TasmotaError):
                save_device_inventory(("8.8.8.8",), path)
            self.assertEqual(path.read_text(), "corrupt")

    def test_failed_atomic_save_preserves_previous_inventory(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "devices.json"
            save_device_inventory(("192.168.1.2",), path)
            with patch("tasmota_manager.os.replace", side_effect=OSError("denied")):
                with self.assertRaises(TasmotaError):
                    save_device_inventory(("192.168.1.3",), path)
            self.assertEqual(load_device_inventory(path), ("192.168.1.2",))
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])

    def test_refresh_empty_inventory_does_not_create_workers(self) -> None:
        self.assertEqual(scan_devices((), "admin", ""), {})



class BuildCommandUrlTests(unittest.TestCase):
    def test_encodes_spaces_and_authentication_parameters(self) -> None:
        url = build_command_url(
            "192.168.1.50",
            "FriendlyName1 Luz principal",
            "admin",
            "pass & word",
        )
        self.assertEqual(
            url,
            "http://192.168.1.50/cm?user=admin&password=pass+%26+word"
            "&cmnd=FriendlyName1+Luz+principal",
        )

    def test_omits_empty_password_parameters(self) -> None:
        url = build_command_url("192.168.1.50", "Status 0")
        self.assertEqual(url, "http://192.168.1.50/cm?cmnd=Status+0")


class ParseDeviceStatusTests(unittest.TestCase):
    def test_extracts_identity_wifi_and_relay_names(self) -> None:
        result = parse_device_status(
            "192.168.1.50",
            {
                "Status": {
                    "DeviceName": "Jardín",
                    "FriendlyName": ["Calefacción", "Luz"],
                },
                "StatusFWR": {
                    "Version": "15.4.0(release-tasmota32)",
                    "Hardware": "ESP32-D0WD-V3 v3.0",
                },
                "StatusPRM": {"Uptime": "0T00:23:17"},
            },
            {
                "StatusSTS": {
                    "POWER1": "OFF",
                    "POWER2": "ON",
                    "Wifi": {"RSSI": 74, "Signal": -63},
                }
            },
        )
        self.assertEqual(
            result,
            DeviceStatus(
                ip="192.168.1.50",
                name="Jardín",
                version="15.4.0(release-tasmota32)",
                hardware="ESP32-D0WD-V3 v3.0",
                uptime="0T00:23:17",
                rssi=74,
                signal_dbm=-63,
                relays=(
                    RelayStatus(1, "Calefacción", "OFF"),
                    RelayStatus(2, "Luz", "ON"),
                ),
            ),
        )

    def test_rejects_incomplete_status_response(self) -> None:
        with self.assertRaises(TasmotaError):
            parse_device_status("192.168.1.50", {}, {})

    def test_uses_default_output_name_when_not_configured(self) -> None:
        result = parse_device_status(
            "192.168.1.50",
            {"Status": {}, "StatusFWR": {}},
            {"StatusSTS": {"POWER3": "OFF"}},
        )
        self.assertEqual(result.relays[0].name, "Salida 3")


class FirmwareUpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.status = DeviceStatus(
            ip="192.168.1.50",
            name="Jardín",
            version="15.4.0(release-tasmota32)",
            hardware="ESP32-D0WD-V3 v3.0",
            uptime="0T00:23:17",
            rssi=74,
            signal_dbm=-63,
            relays=(),
        )

    def test_reads_latest_stable_esp32_release_heading(self) -> None:
        release = parse_latest_firmware_release(
            "<html><h2>Release binaries for Tasmota firmware 15.6.0 "
            "on ESP32</h2></html>"
        )
        self.assertEqual(release.version, "15.6.0")
        self.assertEqual(
            release.url,
            "http://ota.tasmota.com/tasmota32/release/tasmota32.bin",
        )

    def test_rejects_release_page_without_esp32_version(self) -> None:
        with self.assertRaises(TasmotaError):
            parse_latest_firmware_release("<h2>Firmware page unavailable</h2>")

    def test_offers_only_newer_version_for_matching_esp32_firmware(self) -> None:
        self.assertTrue(
            firmware_update_available(self.status, FirmwareRelease("15.6.0"))
        )
        current = DeviceStatus(
            **{**self.status.__dict__, "version": "15.6.0(release-tasmota32)"}
        )
        other_hardware = DeviceStatus(
            **{**self.status.__dict__, "hardware": "ESP8266"}
        )
        self.assertFalse(
            firmware_update_available(current, FirmwareRelease("15.6.0"))
        )
        self.assertFalse(
            firmware_update_available(other_hardware, FirmwareRelease("15.6.0"))
        )

    def test_confirms_target_or_newer_firmware_is_installed(self) -> None:
        release = FirmwareRelease("15.6.0")
        self.assertTrue(firmware_release_installed(
            DeviceStatus(
                **{
                    **self.status.__dict__,
                    "version": "15.6.0(release-tasmota32)",
                    "language_lcid": 2057,
                }
            ),
            release,
        ))
        self.assertTrue(firmware_release_installed(
            DeviceStatus(
                **{
                    **self.status.__dict__,
                    "version": "15.7.0(release-tasmota32)",
                    "language_lcid": 2057,
                }
            ),
            release,
        ))
        self.assertFalse(firmware_release_installed(self.status, release))
        self.assertFalse(
            firmware_release_installed(
                DeviceStatus(**{**self.status.__dict__, "hardware": "ESP8266"}),
                release,
            )
        )

    def test_starts_upgrade_with_approved_firmware_url(self) -> None:
        client = TasmotaClient()
        with patch.object(client, "command", return_value={}) as command:
            client.start_firmware_upgrade(
                "192.168.1.50", FirmwareRelease("15.6.0").url
            )
            self.assertEqual(
                command.call_args_list,
                [
                    call(
                        "192.168.1.50",
                        "OtaUrl http://ota.tasmota.com/tasmota32/release/tasmota32.bin",
                    ),
                    call("192.168.1.50", "Upgrade 1"),
                ],
            )
            with self.assertRaises(TasmotaError):
                client.start_firmware_upgrade(
                    "192.168.1.50", "http://example.com/fw.bin"
                )




class FirmwareFileTests(unittest.TestCase):
    def setUp(self) -> None:
        image = bytearray(128 * 1024)
        image[0] = 0xE9
        image[32:36] = b"\x32\x54\xcd\xab"
        self.image = bytes(image)
        self.release = FirmwareRelease("15.6.0")
        self.metadata = {
            "tag_name": "v15.6.0",
            "assets": [{
                "name": "tasmota32.bin",
                "browser_download_url": (
                    "https://github.com/arendst/Tasmota/releases/download/"
                    "v15.6.0/tasmota32.bin"
                ),
                "size": len(self.image),
                "digest": "sha256:" + hashlib.sha256(self.image).hexdigest(),
            }],
        }

    def test_downloads_versioned_image_and_verifies_digest(self) -> None:
        with TemporaryDirectory() as folder:
            with patch("tasmota_manager.urlopen", side_effect=[
                io.BytesIO(json.dumps(self.metadata).encode()), io.BytesIO(self.image),
            ]) as request:
                path = download_firmware_file(self.release, Path(folder))
            self.assertEqual(path.read_bytes(), self.image)
            self.assertEqual(path.name, "tasmota32-v15.6.0.bin")
            self.assertIn("/v15.6.0/tasmota32.bin", request.call_args.args[0])
            self.assertEqual(list(Path(folder).glob("*.tmp")), [])

    def test_rejects_download_with_wrong_digest_without_saving(self) -> None:
        self.metadata["assets"][0]["digest"] = "sha256:" + "0" * 64
        with TemporaryDirectory() as folder:
            with patch("tasmota_manager.urlopen", side_effect=[
                io.BytesIO(json.dumps(self.metadata).encode()), io.BytesIO(self.image),
            ]):
                with self.assertRaisesRegex(TasmotaError, "SHA-256"):
                    download_firmware_file(self.release, Path(folder))
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_downloads_exact_spanish_asset_with_digest_and_distinct_filename(self) -> None:
        release = FirmwareRelease("15.6.0", SPANISH_FIRMWARE_URL)
        self.metadata["assets"][0]["name"] = "tasmota32-ES.bin"
        self.metadata["assets"][0]["browser_download_url"] = (
            "https://github.com/arendst/Tasmota/releases/download/v15.6.0/tasmota32-ES.bin"
        )
        with TemporaryDirectory() as folder:
            with patch("tasmota_manager.urlopen", side_effect=[
                io.BytesIO(json.dumps(self.metadata).encode()), io.BytesIO(self.image),
            ]) as request:
                path = download_firmware_file(release, Path(folder))
            self.assertEqual(path.name, "tasmota32-ES-v15.6.0.bin")
            self.assertEqual(path.read_bytes(), self.image)
            self.assertTrue(request.call_args.args[0].endswith("/tasmota32-ES.bin"))
        with patch("tasmota_manager.urlopen", return_value=io.BytesIO(
            json.dumps({"assets": [{"name": "tasmota32.bin"}]}).encode()
        )):
            with self.assertRaises(TasmotaError):
                download_firmware_file(release)

    def test_rejects_other_esp32_chip_and_non_firmware(self) -> None:
        image = bytearray(self.image)
        image[12] = 5
        with self.assertRaises(TasmotaError):
            validate_esp32_firmware(bytes(image))
        with self.assertRaises(TasmotaError):
            validate_esp32_firmware(b"<html>error</html>")

    def test_allows_safeboot_recovery_only_on_matching_hardware(self) -> None:
        status = DeviceStatus(
            "192.168.1.50", "Jardín", "12.1.0(safeboot)",
            "ESP32-D0WD-V3 rev.3", "", None, None, (),
        )
        self.assertTrue(firmware_file_update_available(status, self.release))
        self.assertTrue(firmware_update_available(status, self.release))
        self.assertFalse(firmware_release_installed(status, self.release))
        other = DeviceStatus(**{**status.__dict__, "hardware": "ESP32-C3"})
        self.assertFalse(firmware_file_update_available(other, self.release))
        self.assertFalse(firmware_update_available(other, self.release))

    def test_prepares_normal_firmware_with_webui_safeboot_handshake(self) -> None:
        client = TasmotaClient()
        with patch.object(client, "_web_request", side_effect=[
            '<input name="u2">return upl(this)',
            "false", WebRequestUnavailable("restarting"), "true",
            '<input name="u2">',
        ]) as request, patch("tasmota_manager.time.sleep"):
            client.prepare_firmware_upload("192.168.1.50")
        self.assertEqual(request.call_args_list, [
            call("192.168.1.50", "/up"),
            call("192.168.1.50", "/u4?u4=fct&api="),
            call("192.168.1.50", "/u4?u4=fct&api="),
            call("192.168.1.50", "/u4?u4=fct&api="),
            call("192.168.1.50", "/up"),
        ])

    def test_does_not_prepare_or_upload_when_webui_denies_access(self) -> None:
        client = TasmotaClient()
        with patch.object(client, "_web_request", side_effect=TasmotaError("HTTP 401")) as request:
            with self.assertRaisesRegex(TasmotaError, "401"):
                client.prepare_firmware_upload("192.168.1.50")
        self.assertEqual(request.call_count, 1)

    def test_upload_uses_file_field_size_authentication_and_binary(self) -> None:
        client = TasmotaClient("admin", "secret")
        with TemporaryDirectory() as folder:
            path = Path(folder) / "firmware.bin"
            path.write_bytes(self.image)
            with patch("tasmota_manager.urlopen", return_value=io.BytesIO(
                b"<b>Upload <font color='#008000'>Successful</font></b>"
            )) as request:
                client.upload_firmware_file("192.168.1.50", path)
        sent = request.call_args.args[0]
        self.assertEqual(sent.get_method(), "POST")
        self.assertEqual(sent.full_url, f"http://192.168.1.50/u2?fsz={len(self.image)}")
        self.assertIn(b'name="u2"; filename="tasmota32.bin"', sent.data)
        self.assertIn(self.image, sent.data)
        self.assertEqual(sent.get_header("Referer"), "http://192.168.1.50/up")
        self.assertTrue(sent.get_header("Authorization").startswith("Basic "))

    def test_upload_rejects_webui_failure_even_with_http_success(self) -> None:
        with TemporaryDirectory() as folder:
            path = Path(folder) / "firmware.bin"
            path.write_bytes(self.image)
            with patch("tasmota_manager.urlopen", return_value=io.BytesIO(
                b"<b>Upload Failed</b>Not enough space"
            )):
                with self.assertRaisesRegex(TasmotaError, "Not enough space"):
                    TasmotaClient().upload_firmware_file("192.168.1.50", path)

    def test_upload_accepts_official_spanish_success_but_not_failure(self) -> None:
        with TemporaryDirectory() as folder:
            path = Path(folder) / "firmware.bin"
            path.write_bytes(self.image)
            with patch("tasmota_manager.urlopen", return_value=io.BytesIO(
                b"<b>Carga <font color='#008000'>Exitosa</font></b>"
            )):
                TasmotaClient().upload_firmware_file("192.168.1.50", path)
            with patch("tasmota_manager.urlopen", return_value=io.BytesIO(
                b"<b>Carga fallida</b>Espacio insuficiente"
            )):
                with self.assertRaises(TasmotaError):
                    TasmotaClient().upload_firmware_file("192.168.1.50", path)

    def test_connection_loss_during_upload_is_explicitly_uncertain(self) -> None:
        with TemporaryDirectory() as folder:
            path = Path(folder) / "firmware.bin"
            path.write_bytes(self.image)
            with patch("tasmota_manager.urlopen", side_effect=TimeoutError("timeout")):
                with self.assertRaises(FirmwareUploadUncertain):
                    TasmotaClient().upload_firmware_file("192.168.1.50", path)



    def test_existing_backup_rejects_error_page_and_accepts_dmp(self) -> None:
        with TemporaryDirectory() as folder:
            path = Path(folder) / "backup.dmp"
            path.write_bytes(b"\x00" * 4240)
            self.assertEqual(validate_existing_backup(path), path)
            path.write_bytes(b"<html>not a backup</html>" + b" " * 256)
            with self.assertRaises(TasmotaError):
                validate_existing_backup(path)




class DimmerAndLanguageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.status = DeviceStatus(
            "192.168.1.51", "Garaje", "15.6.0(release-tasmota32)",
            "ESP32-D0WD-V3 rev.3", "", None, None,
            (RelayStatus(1, "Luz", "OFF"), RelayStatus(2, "Intensidad", "ON")),
            dimmer=20, language_lcid=2057,
        )

    def test_parses_dimmer_and_actual_locale_from_features(self) -> None:
        status = parse_device_status(
            self.status.ip,
            {"Status": {}, "StatusFWR": {}, "StatusMEM": {"Features": ["040A", "FFFFFFFF"]}},
            {"StatusSTS": {"POWER1": "ON", "Dimmer": 37}},
        )
        self.assertEqual(status.dimmer, 37)
        self.assertEqual(status.language_lcid, 1034)
        self.assertIsNone(parse_device_status(
            self.status.ip, {"Status": {}, "StatusFWR": {}}, {"StatusSTS": {}}
        ).dimmer)

    def test_parses_eight_digit_locale_from_safeboot(self) -> None:
        status = parse_device_status(
            self.status.ip,
            {"Status": {"DeviceName": "Garaje"},
             "StatusFWR": {"Version": "12.1.0(safeboot)", "Hardware": "ESP32-D0WD-V3 rev.3"},
             "StatusMEM": {"Features": ["00000809", "008005C0"]}},
            {"StatusSTS": {}},
        )
        self.assertEqual(status.language_lcid, 2057)
        self.assertEqual(status.version, "12.1.0(safeboot)")
        for bad in ("12340809", "809", "000000809"):
            with self.subTest(locale=bad), self.assertRaises(TasmotaError):
                parse_device_status(
                    self.status.ip,
                    {"Status": {}, "StatusFWR": {}, "StatusMEM": {"Features": [bad]}},
                    {"StatusSTS": {}},
                )

    def test_rejects_invalid_dimmer_and_locale(self) -> None:
        for value in (-1, 101, True, "20", 20.5):
            with self.subTest(value=value), self.assertRaises(TasmotaError):
                parse_device_status(
                    self.status.ip, {"Status": {}, "StatusFWR": {}},
                    {"StatusSTS": {"Dimmer": value}},
                )
        with self.assertRaises(TasmotaError):
            parse_device_status(
                self.status.ip,
                {"Status": {}, "StatusFWR": {}, "StatusMEM": {"Features": ["bad"]}},
                {"StatusSTS": {}},
            )

    def test_same_version_language_change_requires_confirmed_locale(self) -> None:
        spanish = FirmwareRelease("15.6.0", SPANISH_FIRMWARE_URL)
        self.assertTrue(firmware_update_available(self.status, spanish))
        self.assertFalse(firmware_release_installed(self.status, spanish))
        installed = DeviceStatus(**{**self.status.__dict__, "language_lcid": 1034})
        self.assertTrue(firmware_release_installed(installed, spanish))
        self.assertFalse(firmware_update_available(installed, spanish))
        self.assertTrue(firmware_update_available(installed, FirmwareRelease("15.6.0")))
        unknown = DeviceStatus(**{**self.status.__dict__, "language_lcid": None})
        self.assertFalse(firmware_release_installed(unknown, spanish))
        self.assertFalse(firmware_update_available(unknown, spanish))
        self.assertFalse(firmware_update_available(
            self.status, FirmwareRelease("15.5.0", SPANISH_FIRMWARE_URL)
        ))

    def test_spanish_ota_url_is_approved(self) -> None:
        client = TasmotaClient()
        with patch.object(client, "command") as command:
            client.start_firmware_upgrade(self.status.ip, SPANISH_FIRMWARE_URL)
        command.assert_has_calls([
            call(self.status.ip, f"OtaUrl {SPANISH_FIRMWARE_URL}"),
            call(self.status.ip, "Upgrade 1"),
        ])
        with self.assertRaises(TasmotaError):
            _ = FirmwareRelease("15.6.0", "http://example.com/a.bin").filename






class ConfigurationBackupTests(unittest.TestCase):
    def test_downloads_atomic_dmp_with_auth_and_referer(self) -> None:
        response = io.BytesIO(b"\x00" * 1024)
        response.headers = Message()
        response.headers["Content-Type"] = "application/octet-stream"
        response.headers["Content-Length"] = "1024"
        client = TasmotaClient(username="admin", password="secret")
        with TemporaryDirectory() as temp_dir:
            with patch("tasmota_manager.urlopen", return_value=response) as urlopen_mock:
                backup_path = client.download_configuration_backup(
                    "192.168.1.50",
                    "Jardín principal",
                    Path(temp_dir),
                )

            request = urlopen_mock.call_args.args[0]
            self.assertEqual(request.full_url, "http://192.168.1.50/dl")
            self.assertEqual(request.get_header("Referer"), "http://192.168.1.50/")
            self.assertEqual(
                request.get_header("Authorization"),
                "Basic " + base64.b64encode(b"admin:secret").decode("ascii"),
            )
            self.assertEqual(backup_path.suffix, ".dmp")
            self.assertIn("Jard_n_principal", backup_path.name)
            self.assertEqual(backup_path.read_bytes(), b"\x00" * 1024)
            self.assertEqual(list(Path(temp_dir).glob("*.tmp")), [])

    def test_rejects_error_page_without_saving_a_backup(self) -> None:
        response = io.BytesIO(b"<html>not a configuration</html>")
        response.headers = Message()
        response.headers["Content-Type"] = "text/html"
        with TemporaryDirectory() as temp_dir:
            with patch("tasmota_manager.urlopen", return_value=response):
                with self.assertRaises(TasmotaError):
                    TasmotaClient().download_configuration_backup(
                        "192.168.1.50", "Jardín", Path(temp_dir)
                    )
            self.assertEqual(list(Path(temp_dir).iterdir()), [])

    def test_failed_backup_prevents_firmware_upgrade(self) -> None:
        client = TasmotaClient()
        with (
            patch.object(
                client,
                "download_configuration_backup",
                side_effect=TasmotaError("no backup"),
            ),
            patch.object(client, "start_firmware_upgrade") as upgrade,
        ):
            with self.assertRaisesRegex(TasmotaError, "no backup"):
                backup_before_firmware_upgrade(
                    client,
                    "192.168.1.50",
                    "Jardín",
                    FirmwareRelease("15.6.0").url,
                )
            upgrade.assert_not_called()

    def test_failed_upgrade_reports_saved_backup_path(self) -> None:
        client = TasmotaClient()
        backup_path = Path("backups/jardín.dmp")
        with (
            patch.object(
                client,
                "download_configuration_backup",
                return_value=backup_path,
            ),
            patch.object(
                client,
                "start_firmware_upgrade",
                side_effect=TasmotaError("upgrade failed"),
            ),
        ):
            with self.assertRaisesRegex(TasmotaError, "jardín.dmp"):
                backup_before_firmware_upgrade(
                    client,
                    "192.168.1.50",
                    "Jardín",
                    FirmwareRelease("15.6.0").url,
                )


if __name__ == "__main__":
    unittest.main()

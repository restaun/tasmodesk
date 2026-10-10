import unittest
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, call, patch

from tasmota_manager import DeviceStatus, FirmwareRelease, FirmwareUploadUncertain, RelayStatus, TasmotaError
from tasmota_service import TasmotaService, UpdateJob, SharedFirmware


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.preferences_path = Path(directory.name) / "preferences.json"
        with patch("tasmota_service.load_device_inventory", return_value=()):
            self.service = TasmotaService(preferences_path=self.preferences_path)
        self.service.executor.shutdown()
        self.service.executor = Mock()
        self.status = DeviceStatus(
            "192.168.1.111", "Comedor", "15.4.0(release-tasmota32)",
            "ESP32-D0WD-V3", "", 85, -50,
            (RelayStatus(1, "Luz", "ON"), RelayStatus(2, "Intensidad", "ON")), 20, 2057,
        )
        self.service.devices[self.status.ip] = self.status
        self.service.release = FirmwareRelease("15.6.0")

    def run_submitted(self) -> None:
        submission = self.service.executor.submit.call_args
        submission.args[0](*submission.args[1:])

    def test_demo_is_read_only_and_does_not_load_inventory(self) -> None:
        with patch("tasmota_service.load_device_inventory") as inventory:
            service = TasmotaService(demo=True)
        inventory.assert_not_called()
        self.assertEqual(len(service.snapshot()["devices"]), 3)
        with self.assertRaises(TasmotaError):
            service.refresh()
        with self.assertRaises(TasmotaError):
            service.start_updates(by_file=True)
        self.assertTrue(service.close())

    def test_credentials_are_not_in_snapshot_and_language_is_validated(self) -> None:
        self.service.set_access("admin", "not-for-snapshot")
        self.assertNotIn("not-for-snapshot", str(self.service.snapshot()))
        with self.assertRaises(TasmotaError):
            self.service.set_language("unknown")
        self.assertEqual(self.service.selected_release().filename, "tasmota32-ES.bin")
        self.service.set_language("English")
        self.assertEqual(self.service.selected_release().filename, "tasmota32.bin")

    def test_snapshot_identifies_found_firmware_for_selected_language(self) -> None:
        state = self.service.snapshot()
        self.assertEqual(state["release"], "15.6.0")
        self.assertEqual(state["release_filename"], "tasmota32-ES.bin")
        self.assertTrue(state["release_url"].endswith("/tasmota32-ES.bin"))
        self.service.set_language("English")
        state = self.service.snapshot()
        self.assertEqual(state["release_filename"], "tasmota32.bin")
        self.assertTrue(state["release_url"].endswith("/tasmota32.bin"))
        self.service.release = None
        state = self.service.snapshot()
        self.assertEqual(state["release"], "")
        self.assertEqual(state["release_filename"], "")
        self.assertEqual(state["release_url"], "")

    def test_changes_are_validated_and_read_back_once(self) -> None:
        client = Mock()
        client.read_status.return_value = self.status
        with patch.object(self.service, "_client", return_value=client):
            self.service.change(self.status.ip, "dimmer", 55)
        self.assertIn(self.status.ip, self.service.pending)
        with self.assertRaises(TasmotaError):
            self.service.change(self.status.ip, "dimmer", 60)
        self.run_submitted()
        client.command.assert_called_once_with(self.status.ip, "Dimmer 55")
        client.read_status.assert_called_once_with(self.status.ip)
        self.assertFalse(self.service.pending)
        for operation, value, number in (("dimmer", 101, 0), ("power", "ON", 5),
                                          ("name", "bad;name", 0), ("relay_name", "test", 9)):
            with self.assertRaises(TasmotaError):
                self.service.change(self.status.ip, operation, value, number)

    def test_change_failure_is_explicit_and_unlocks(self) -> None:
        client = Mock()
        client.command.side_effect = TasmotaError("sin conexión")
        with patch.object(self.service, "_client", return_value=client):
            self.service.change(self.status.ip, "power", "OFF", 1)
        self.run_submitted()
        self.assertFalse(self.service.pending)
        self.assertEqual(self.service.events.get_nowait(), ("error", "sin conexión"))

    def test_bulk_schedules_all_jobs_with_captured_language_and_credentials(self) -> None:
        second = DeviceStatus(**{**self.status.__dict__, "ip": "192.168.1.112"})
        self.service.devices[second.ip] = second
        self.service.devices["192.168.1.113"] = "offline"
        self.service.start_updates(by_file=True)
        self.assertEqual(self.service.executor.submit.call_count, 2)
        self.assertEqual(self.service.running_updates(), {self.status.ip, second.ip})
        self.service.set_language("English")
        for submission in self.service.executor.submit.call_args_list:
            job = submission.args[2]
            self.assertEqual(job.release.filename, "tasmota32-ES.bin")
            self.assertTrue(job.by_file)
            self.assertEqual(submission.args[3], ("admin", ""))
        self.assertFalse(self.service.close())
        with self.assertRaises(TasmotaError):
            self.service.change(self.status.ip, "name", "Nuevo")

    def test_same_version_only_targets_language_change(self) -> None:
        current = DeviceStatus(**{**self.status.__dict__, "version": "15.6.0(release-tasmota32)"})
        self.service.devices[current.ip] = current
        self.assertEqual(len(self.service.update_targets(by_file=True)), 1)
        self.service.set_language("English")
        with self.assertRaises(TasmotaError):
            self.service.update_targets(by_file=True)

    def test_upload_orders_backup_download_prepare_upload_and_verification(self) -> None:
        release = self.service.selected_release()
        job = UpdateJob(self.status.ip, release, True)
        installed = DeviceStatus(**{**self.status.__dict__, "version": "15.6.0(release-tasmota32)",
                                    "language_lcid": 1034})
        trace = Mock()
        client = Mock()
        client.download_configuration_backup.return_value = Path("copy.dmp")
        client.read_status.return_value = installed
        trace.attach_mock(client, "client")
        with patch("tasmota_service.TasmotaClient", return_value=client), patch(
            "tasmota_service.download_firmware_file", return_value=Path("image.bin")
        ) as download, patch("tasmota_service.time.sleep"):
            trace.attach_mock(download, "download")
            self.service._update(self.status, job, ("admin", ""), None)
        self.assertEqual(trace.mock_calls[:4], [
            call.client.download_configuration_backup(self.status.ip, self.status.name),
            call.download(release),
            call.client.prepare_firmware_upload(self.status.ip),
            call.client.upload_firmware_file(self.status.ip, Path("image.bin")),
        ])
        self.assertEqual(job.state, "success")
        self.assertEqual(job.progress, 100)
        self.assertEqual(job.backup, "copy.dmp")
        self.assertEqual(self.service.devices[self.status.ip].language_lcid, 1034)

    def test_failed_backup_never_downloads_or_uploads(self) -> None:
        job = UpdateJob(self.status.ip, self.service.selected_release(), True)
        client = Mock()
        client.download_configuration_backup.side_effect = TasmotaError("backup denied")
        with patch("tasmota_service.TasmotaClient", return_value=client), patch(
            "tasmota_service.download_firmware_file"
        ) as download:
            self.service._update(self.status, job, ("admin", ""), None)
        download.assert_not_called()
        client.prepare_firmware_upload.assert_not_called()
        client.upload_firmware_file.assert_not_called()
        self.assertEqual(job.state, "error")
        self.assertFalse(job.needs_backup)

    def test_uncertain_upload_is_not_repeated(self) -> None:
        job = UpdateJob(self.status.ip, self.service.selected_release(), True)
        client = Mock()
        client.download_configuration_backup.return_value = Path("copy.dmp")
        client.upload_firmware_file.side_effect = FirmwareUploadUncertain("interrupted")
        client.read_status.return_value = DeviceStatus(
            **{**self.status.__dict__, "version": "15.6.0(release-tasmota32)", "language_lcid": 1034}
        )
        with patch("tasmota_service.TasmotaClient", return_value=client), patch(
            "tasmota_service.download_firmware_file", return_value=Path("image.bin")
        ), patch("tasmota_service.time.sleep"):
            self.service._update(self.status, job, ("admin", ""), None)
        self.assertEqual(client.upload_firmware_file.call_count, 1)
        self.assertEqual(job.state, "success")

    def test_wrong_language_times_out_without_success(self) -> None:
        job = UpdateJob(self.status.ip, self.service.selected_release(), True)
        client = Mock()
        client.download_configuration_backup.return_value = Path("copy.dmp")
        client.read_status.return_value = self.status
        with patch("tasmota_service.TasmotaClient", return_value=client), patch(
            "tasmota_service.download_firmware_file", return_value=Path("image.bin")
        ), patch.object(self.service, "_wait_phase", return_value=0):
            self.service._update(self.status, job, ("admin", ""), None)
        self.assertEqual(job.state, "error")
        self.assertLess(job.progress, 100)

    def test_safeboot_ota_sends_exactly_one_second_order_and_waits(self) -> None:
        self.service.set_language("English")
        job = UpdateJob(self.status.ip, self.service.selected_release(), False)
        safeboot = DeviceStatus(**{**self.status.__dict__, "version": "12.1.0(safeboot)"})
        installed = DeviceStatus(**{**self.status.__dict__, "version": "15.6.0(release-tasmota32)"})
        client = Mock()
        client.download_configuration_backup.return_value = Path("copy.dmp")
        client.read_status.side_effect = [safeboot, safeboot, installed]
        with patch("tasmota_service.TasmotaClient", return_value=client), patch(
            "tasmota_service.time.sleep"
        ) as sleep:
            self.service._update(self.status, job, ("admin", ""), None)
        self.assertEqual(
            client.command.call_args_list,
            [call(self.status.ip, f"OtaUrl {job.release.url}"),
             call(self.status.ip, "Upgrade 1"), call(self.status.ip, "Upgrade 1")],
        )
        self.assertEqual(sleep.call_args_list, [call(20), call(60), call(30)])
        self.assertEqual(job.state, "success")

    def test_safeboot_failed_backup_requires_explicit_previous_file(self) -> None:
        job = UpdateJob(self.status.ip, self.service.selected_release(), True)
        safeboot = DeviceStatus(**{**self.status.__dict__, "version": "12.1.0(safeboot)"})
        client = Mock()
        client.download_configuration_backup.side_effect = TasmotaError("404")
        with patch("tasmota_service.TasmotaClient", return_value=client):
            self.service._update(safeboot, job, ("admin", ""), None)
        self.assertTrue(job.needs_backup)
        self.service.jobs[job.ip] = job
        self.service.devices[job.ip] = safeboot
        with patch("tasmota_service.validate_existing_backup", return_value=Path("previous.dmp")):
            self.service.use_previous_backup(job.ip, "previous.dmp")
        self.assertEqual(job.state, "running")
        self.assertFalse(job.needs_backup)
        self.assertEqual(self.service.executor.submit.call_args.args[-1], Path("previous.dmp"))

    def test_preferences_persist_without_credentials_and_reject_corrupt_overwrite(self) -> None:
        self.service.set_access("admin", "not-saved")
        self.service.set_preferences({"theme": "dark", "page": "Actualizaciones", "auto_refresh": False})
        data = json.loads(self.preferences_path.read_text(encoding="utf-8"))
        self.assertEqual(data, {"theme": "dark", "page": "Actualizaciones", "auto_refresh": False})
        self.assertNotIn("not-saved", self.preferences_path.read_text())
        with patch("tasmota_service.load_device_inventory", return_value=()):
            other = TasmotaService(preferences_path=self.preferences_path)
        self.assertEqual(other.preferences, data)
        other.close()
        previous = self.preferences_path.read_text()
        with patch("tasmota_service.os.replace", side_effect=OSError("denied")):
            with self.assertRaises(TasmotaError):
                self.service.set_preferences({"theme": "light"})
        self.assertEqual(self.preferences_path.read_text(), previous)
        self.assertEqual(list(self.preferences_path.parent.glob("*.tmp")), [])
        for invalid in ({"auto_refresh": 1}, {"theme": "x"}, {"page": "x"}, {"password": "secret"}):
            with self.assertRaises(TasmotaError):
                self.service.set_preferences(invalid)
        self.preferences_path.write_text("corrupt")
        with patch("tasmota_service.load_device_inventory", return_value=()):
            other = TasmotaService(preferences_path=self.preferences_path)
        with self.assertRaises(TasmotaError):
            other.set_preferences({"theme": "dark"})
        self.assertEqual(self.preferences_path.read_text(), "corrupt")
        other.close()

    def test_auto_refresh_threshold_and_operation_pause(self) -> None:
        self.service.last_refresh = 100
        with patch("tasmota_service.time.monotonic", return_value=159), patch.object(
            self.service, "refresh"
        ) as refresh:
            self.service.auto_refresh_tick()
            refresh.assert_not_called()
        with patch("tasmota_service.time.monotonic", return_value=160), patch.object(
            self.service, "refresh"
        ) as refresh:
            self.service.busy = "busy"
            self.service.auto_refresh_tick()
            self.service.busy = ""
            self.service.pending.add(self.status.ip)
            self.service.auto_refresh_tick()
            self.service.pending.clear()
            self.service.jobs[self.status.ip] = UpdateJob(self.status.ip, self.service.selected_release(), True)
            self.service.auto_refresh_tick()
            self.service.jobs.clear()
            self.service.preferences["auto_refresh"] = False
            self.service.auto_refresh_tick()
            refresh.assert_not_called()
            self.service.preferences["auto_refresh"] = True
            self.service.auto_refresh_tick()
            refresh.assert_called_once()

    def test_offline_keeps_name_last_read_and_update_details(self) -> None:
        self.service._record_status(self.status.ip, self.status)
        before = self.service.last_read[self.status.ip]
        job = UpdateJob(self.status.ip, self.service.selected_release(), True)
        job.backup = "copy.dmp"
        job.logs = ["Guardada la copia", "Reiniciando"]
        self.service.jobs[job.ip] = job
        self.service._record_status(job.ip, "No responde")
        state = self.service.snapshot()
        row = state["devices"][0]
        self.assertFalse(row["available"])
        self.assertEqual(row["name"], "Comedor")
        self.assertEqual(row["last_read"], before)
        self.assertTrue(row["updating"])
        self.assertEqual(state["jobs"][0]["backup"], "copy.dmp")
        self.assertEqual(state["jobs"][0]["logs"], job.logs)

    def test_automatic_refresh_reschedules_once_per_interval(self) -> None:
        self.service.last_refresh = 100
        with patch("tasmota_service.time.monotonic", return_value=160), patch.object(
            self.service, "_launch"
        ) as launch:
            self.service.auto_refresh_tick()
            self.service.auto_refresh_tick()
            launch.assert_called_once()
            self.assertEqual(self.service.last_refresh, 160)
        with patch("tasmota_service.time.monotonic", return_value=219), patch.object(
            self.service, "_launch"
        ) as launch:
            self.service.auto_refresh_tick()
            launch.assert_not_called()
        with patch("tasmota_service.time.monotonic", return_value=220), patch.object(
            self.service, "_launch"
        ) as launch:
            self.service.auto_refresh_tick()
            launch.assert_called_once()

    def test_shared_download_runs_once_for_concurrent_batch_and_shares_failures(self) -> None:
        shared = SharedFirmware()
        release = self.service.selected_release()
        entered = threading.Event()
        finish = threading.Event()
        def download(release):
            entered.set()
            self.assertTrue(finish.wait(5))
            return Path("verified.bin")
        with patch("tasmota_service.download_firmware_file", side_effect=download) as fetch:
            with ThreadPoolExecutor(max_workers=3) as workers:
                futures = [workers.submit(shared.get, release) for _ in range(3)]
                self.assertTrue(entered.wait(5))
                finish.set()
                self.assertEqual([future.result() for future in futures], [Path("verified.bin")] * 3)
            fetch.assert_called_once_with(release)
        failed = SharedFirmware()
        with patch("tasmota_service.download_firmware_file", side_effect=TasmotaError("digest")) as fetch:
            for _ in range(2):
                with self.assertRaises(TasmotaError):
                    failed.get(release)
            fetch.assert_called_once()
        second = DeviceStatus(**{**self.status.__dict__, "ip": "192.168.1.112"})
        self.service.devices[second.ip] = second
        self.service.start_updates(by_file=True)
        jobs = list(self.service.jobs.values())
        self.assertIs(jobs[0].firmware, jobs[1].firmware)

    def test_lists_only_backups_belonging_to_selected_ip(self) -> None:
        with TemporaryDirectory() as folder, patch.object(
            self.service, "backup_directory", return_value=Path(folder)
        ):
            correct = Path(folder) / "Comedor_192.168.1.111_20261010.dmp"
            correct.write_bytes(b"\0" * 1024)
            (Path(folder) / "Other_192.168.1.112_20261010.dmp").write_bytes(b"\0")
            (Path(folder) / "Comedor_192.168.1.111_error.html").write_text("error")
            copies = self.service.list_backups(self.status.ip)
            self.assertEqual(len(copies), 1)
            self.assertEqual(copies[0]["name"], correct.name)
            self.assertEqual(copies[0]["size"], 1024)
            with self.assertRaises(TasmotaError):
                self.service.list_backups("192.168.1.199")

    @staticmethod
    def firmware_image() -> bytes:
        image = bytearray(256 * 1024)
        image[0] = 0xE9
        image[32:36] = b"\x32\x54\xcd\xab"
        return bytes(image)

    def test_local_firmware_is_validated_copied_and_confirmed_outside_safeboot(self) -> None:
        with TemporaryDirectory() as folder:
            source = Path(folder) / "custom.bin"
            source.write_bytes(self.firmware_image())
            self.service.firmware_directory = Path(folder) / "cache"
            self.service.release = None
            message, digest = self.service.local_update_confirmation(str(source), self.status.ip)
            self.assertIn(digest, message)
            self.assertIn("Comedor (192.168.1.111)", message)
            self.service.start_local_updates(str(source), digest, self.status.ip)
            job = self.service.jobs[self.status.ip]
            copy = job.firmware.path
            self.assertEqual(copy.parent, self.service.firmware_directory)
            self.assertEqual(copy.read_bytes(), source.read_bytes())
            self.service.jobs.clear()
            safeboot = DeviceStatus(**{**self.status.__dict__, "version": "15.6.0(safeboot)"})
            installed = DeviceStatus(**{**self.status.__dict__, "version": "15.7.0(release-tasmota32)"})
            client = Mock()
            client.download_configuration_backup.return_value = Path("copy.dmp")
            client.read_status.side_effect = [safeboot, installed]
            with patch("tasmota_service.TasmotaClient", return_value=client), patch(
                "tasmota_service.download_firmware_file"
            ) as download, patch("tasmota_service.time.sleep"):
                self.service._update(self.status, job, ("admin", ""), None)
            download.assert_not_called()
            client.upload_firmware_file.assert_called_once_with(self.status.ip, copy)
            self.assertEqual(job.state, "success")
            self.assertIn("15.7.0(release-tasmota32) desde custom.bin", job.message)

    def test_local_firmware_rejects_invalid_or_changed_files(self) -> None:
        with TemporaryDirectory() as folder:
            self.service.firmware_directory = Path(folder) / "cache"
            text = Path(folder) / "page.bin"
            text.write_bytes(b"<html>" * 50_000)
            with self.assertRaises(TasmotaError):
                self.service.local_update_confirmation(str(text))
            wrong = Path(folder) / "image.txt"
            wrong.write_bytes(self.firmware_image())
            with self.assertRaises(TasmotaError):
                self.service.local_update_confirmation(str(wrong))
            source = Path(folder) / "custom.bin"
            source.write_bytes(self.firmware_image())
            _message, digest = self.service.local_update_confirmation(str(source))
            source.write_bytes(self.firmware_image()[:-1] + b"\1")
            with self.assertRaises(TasmotaError):
                self.service.start_local_updates(str(source), digest)
            self.assertFalse(self.service.jobs)
            self.service.demo = True
            with self.assertRaises(TasmotaError):
                self.service.start_local_updates(str(source), digest)

    def test_refresh_does_not_lock_device_controls_or_overwrite_newer_changes(self) -> None:
        stale = DeviceStatus(**{**self.status.__dict__, "name": "Antiguo"})
        with patch("tasmota_service.scan_devices", return_value={self.status.ip: stale}):
            self.service.refresh()
            refresh = self.service.executor.submit.call_args.args[0]
            self.assertFalse(self.service.snapshot()["devices_locked"])
            client = Mock()
            client.read_status.return_value = self.status
            with patch.object(self.service, "_client", return_value=client):
                self.service.change(self.status.ip, "power", "OFF", 1)
            self.run_submitted()
            refresh()
        self.assertEqual(self.service.devices[self.status.ip].name, "Comedor")
        self.assertEqual(self.service.busy, "")
        with patch("tasmota_service.discover_tasmotas"):
            self.service.discover()
        self.assertTrue(self.service.snapshot()["devices_locked"])
        with self.assertRaises(TasmotaError):
            self.service.change(self.status.ip, "power", "OFF", 1)

if __name__ == "__main__":
    unittest.main()

import importlib.util
import os
import time
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@unittest.skipUnless(importlib.util.find_spec("PySide6"), "Instala PySide6 para probar la interfaz nativa.")
class NativeWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle("Fusion")

    def setUp(self) -> None:
        from tasmota_service import TasmotaService
        from tasmodesk import TasmotaWindow
        self.directory = TemporaryDirectory()
        self.service = TasmotaService(demo=True, preferences_path=Path(self.directory.name) / "prefs.json")
        self.service.demo = False
        self.window = TasmotaWindow(self.service)
        self.window.timer.stop()
        self.window.show()
        self.app.processEvents()
        self.ip = "192.168.1.111"

    def tearDown(self) -> None:
        for job in self.service.jobs.values():
            job.state = "error"
        self.service.pending.clear()
        self.service.busy = ""
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.directory.cleanup()

    def test_three_pages_native_layout_and_responsive_grid(self) -> None:
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QLineEdit, QPushButton, QSlider
        self.assertEqual(len(self.window.cards), 3)
        self.assertEqual(len(self.window.grid_widget.findChildren(QSlider)), 3)
        powers = [b for b in self.window.grid_widget.findChildren(QPushButton) if b.objectName().startswith("power-")]
        self.assertEqual(len(powers), 6)
        self.assertEqual([b.text() for b in powers[:2]], ["Encender", "Apagar"])
        self.assertEqual(len(self.window.grid_widget.findChildren(QLineEdit)), 0)
        self.assertEqual(self.window.grid.getItemPosition(1)[1], 1)
        self.window.navigate("Configuración")
        self.assertTrue(self.window.access_frame.isVisible())
        self.assertEqual(len(self.window.grid_widget.findChildren(QLineEdit)), 12)
        self.window.navigate("Actualizaciones")
        self.assertTrue(self.window.firmware_frame.isVisible())
        self.assertIn("15.6.0", self.window.firmware_text.text())
        self.assertIn("tasmota32-ES.bin", self.window.firmware_source.text())
        self.window.resize(900, 650)
        QTest.qWait(50)
        self.assertEqual(self.window.device_scroll.horizontalScrollBar().maximum(), 0)
        self.assertEqual(self.window.grid.itemAtPosition(0, 0).widget(), self.window.cards["192.168.1.110"])

    def test_power_single_button_sends_opposite_and_blocks_duplicates(self) -> None:
        from PySide6.QtWidgets import QPushButton
        def change(ip, operation, value, number):
            self.service.pending.add(ip)
            self.service._touch()
        with patch.object(self.service, "change", side_effect=change) as send:
            control = self.window.findChild(QPushButton, f"power-{self.ip}-1")
            control.click()
            send.assert_called_once_with(self.ip, "power", "ON", 1)
            self.assertFalse(self.window.findChild(QPushButton, f"power-{self.ip}-1").isEnabled())
            control = self.window.findChild(QPushButton, f"power-{self.ip}-1")
            control.click()
            self.assertEqual(send.call_count, 1)
            previous = self.service.last_status[self.ip]
            relays = (replace(previous.relays[0], state="ON"), *previous.relays[1:])
            self.service._record_status(self.ip, replace(previous, relays=relays))
            self.service.pending.clear()
            self.service._touch()
            self.window.poll()
            control = self.window.findChild(QPushButton, f"power-{self.ip}-1")
            self.assertEqual(control.text(), "Apagar")
            control.click()
            self.assertEqual(send.call_args.args, (self.ip, "power", "OFF", 1))

    def test_dimmer_commits_only_on_release_and_keyboard_release(self) -> None:
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        from tasmodesk import DimmerSlider
        with patch.object(self.service, "change") as send:
            slider = self.window.findChild(DimmerSlider, "dimmer-" + self.ip)
            slider.setSliderDown(True)
            slider.setValue(72)
            QTest.qWait(550)
            send.assert_not_called()
            QTest.mouseRelease(slider, Qt.MouseButton.LeftButton)
            send.assert_called_once_with(self.ip, "dimmer", 72, 0)
            send.reset_mock()
            slider = self.window.findChild(DimmerSlider, "dimmer-" + self.ip)
            QTest.keyPress(slider, Qt.Key.Key_Right)
            send.assert_not_called()
            QTest.keyRelease(slider, Qt.Key.Key_Right)
            send.assert_called_once_with(self.ip, "dimmer", 36, 0)

    def test_wheel_batches_changes_and_failed_unchanged_name_is_retained(self) -> None:
        from PySide6.QtCore import QPoint, QPointF, Qt
        from PySide6.QtGui import QWheelEvent
        from PySide6.QtTest import QTest
        from tasmodesk import DimmerSlider
        with patch.object(self.service, "change") as send:
            slider = self.window.findChild(DimmerSlider, "dimmer-" + self.ip)
            for _ in range(2):
                event = QWheelEvent(
                    QPointF(5, 5), QPointF(5, 5), QPoint(), QPoint(0, 120),
                    Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                    Qt.ScrollPhase.NoScrollPhase, False,
                )
                self.app.sendEvent(slider, event)
                QTest.qWait(50)
            final_value = slider.value()
            send.assert_not_called()
            QTest.qWait(550)
            send.assert_called_once_with(self.ip, "dimmer", final_value, 0)
        self.window.navigate("Configuración")
        with patch.object(self.service, "change", side_effect=ValueError("Error de envío")):
            self.window.save_name(self.ip, 0, "Comedor")
        self.assertIn((self.ip, 0), self.window.drafts)
        self.assertFalse(self.window.drafts[(self.ip, 0)].saving)

    def test_drafts_survive_refresh_navigation_and_failed_save(self) -> None:
        from PySide6.QtWidgets import QLineEdit
        self.window.navigate("Configuración")
        self.window.edit_name((self.ip, 0), "Comedor", "Nuevo comedor")
        self.service._touch()
        self.window.poll()
        self.assertEqual(self.window.findChild(QLineEdit, f"name-{self.ip}-0").text(), "Nuevo comedor")
        self.window.navigate("Dispositivos")
        self.window.navigate("Configuración")
        self.assertEqual(self.window.findChild(QLineEdit, f"name-{self.ip}-0").text(), "Nuevo comedor")
        with patch.object(self.service, "change", side_effect=ValueError("No se pudo guardar")):
            self.window.save_name(self.ip, 0, "Nuevo comedor")
        self.assertFalse(self.window.drafts[(self.ip, 0)].saving)
        self.assertTrue(self.window.notifications)
        self.assertEqual(self.window.drafts[(self.ip, 0)].value, "Nuevo comedor")
        self.window.discard_name((self.ip, 0))
        self.assertEqual(self.window.findChild(QLineEdit, f"name-{self.ip}-0").text(), "Comedor")

    def test_saving_draft_waits_for_confirmed_read_and_retains_offline_failure(self) -> None:
        from tasmodesk import Draft
        self.window.drafts[(self.ip, 0)] = Draft("Nuevo comedor", True)
        self.service.pending.add(self.ip)
        self.window.poll()
        self.assertIn((self.ip, 0), self.window.drafts)
        previous = self.service.last_status[self.ip]
        self.service._record_status(self.ip, replace(previous, name="Nuevo comedor"))
        self.service.pending.clear()
        self.service._touch()
        self.window.poll()
        self.assertNotIn((self.ip, 0), self.window.drafts)
        self.window.drafts[(self.ip, 0)] = Draft("Nuevo comedor", True)
        self.service._record_status(self.ip, "Sin conexión")
        self.service._touch()
        self.window.poll()
        self.assertFalse(self.window.drafts[(self.ip, 0)].saving)

    def test_search_filters_and_offline_cached_name(self) -> None:
        self.window.search.setText("comedor")
        self.assertEqual(list(self.window.cards), [self.ip])
        self.window.search.setText("calefacción")
        self.assertEqual(len(self.window.cards), 3)
        self.window.search.clear()
        self.service._record_status(self.ip, "Sin conexión")
        self.service._touch()
        self.window.poll()
        self.window.filter.setCurrentIndex(2)
        self.assertEqual(list(self.window.cards), [self.ip])
        self.window.filter.setCurrentIndex(1)
        self.assertEqual(len(self.window.cards), 2)
        self.window.filter.setCurrentIndex(3)
        self.assertEqual(len(self.window.cards), 2)
        self.window.filter.setCurrentIndex(4)
        self.assertEqual(len(self.window.cards), 2)

    def test_offline_job_keeps_progress_backup_logs_and_blocks_close(self) -> None:
        from PySide6.QtWidgets import QLabel, QProgressBar, QTextEdit, QToolButton
        from tasmota_service import UpdateJob
        job = UpdateJob(self.ip, self.service.selected_release(), True)
        job.progress = 72
        job.message = "Esperando reinicio"
        job.backup = "Copia_192.168.1.111.dmp"
        job.logs = ["Copia guardada", "Firmware subido"]
        self.service.jobs[self.ip] = job
        self.service._record_status(self.ip, "Sin conexión")
        self.service._touch()
        self.window.navigate("Actualizaciones")
        progress, message = self.window.job_widgets[self.ip]
        self.assertEqual(progress.value(), 72)
        self.assertIn("Esperando reinicio", message.text())
        self.assertTrue(any(job.backup in l.text() for l in self.window.cards[self.ip].findChildren(QLabel)))
        toggle = self.window.cards[self.ip].findChild(QToolButton)
        toggle.click()
        self.window.navigate("Dispositivos")
        self.window.navigate("Actualizaciones")
        toggle = self.window.cards[self.ip].findChild(QToolButton)
        self.assertTrue(toggle.isChecked())
        self.assertEqual(self.window.cards[self.ip].findChild(QTextEdit).toPlainText(), "\n".join(job.logs))
        self.window.close()
        self.assertTrue(self.window.isVisible())
        self.assertFalse(self.window.timer.isActive())
        self.assertTrue(self.window.notifications)
        job.state = "success"
        job.progress = 100
        job.finished_at = time.monotonic()
        self.service._touch()
        self.window.poll()
        self.assertEqual(self.window.job_widgets[self.ip][0].value(), 100)
        job.finished_at = time.monotonic() - 5
        self.window.poll()
        self.assertNotIn(self.ip, self.window.job_widgets)
        self.assertFalse(self.window.cards[self.ip].findChildren(QProgressBar))

    def test_update_requires_confirmation_and_reuses_service(self) -> None:
        with patch.object(self.window, "confirm", return_value=False), patch.object(self.service, "start_updates") as update:
            self.window.start_update_ui("", True)
            update.assert_not_called()
        with patch.object(self.window, "confirm", return_value=True), patch.object(self.service, "start_updates") as update:
            self.window.start_update_ui("", True)
            update.assert_called_once_with(ip="", by_file=True)
        with patch.object(self.service, "update_confirmation", side_effect=ValueError("No disponible")), patch.object(
            self.service, "start_updates"
        ) as update:
            self.window.start_update_ui(self.ip, False)
            update.assert_not_called()
            self.assertTrue(self.window.notifications)

    def test_credentials_preferences_language_and_no_password_snapshot(self) -> None:
        self.window.username.setText("admin")
        self.window.password.setText("solo-memoria")
        self.window.invoke(lambda: self.service.set_access(self.window.username.text(), self.window.password.text()))
        self.window.navigate("Configuración")
        self.window.navigate("Dispositivos")
        self.window.navigate("Configuración")
        self.assertEqual(self.window.password.text(), "solo-memoria")
        self.assertNotIn("solo-memoria", str(self.service.snapshot()))
        self.window.auto_refresh.setChecked(False)
        self.assertFalse(self.service.preferences["auto_refresh"])
        self.window.toggle_theme()
        self.assertEqual(self.service.preferences["theme"], "dark")
        self.window.language.setCurrentText("English")
        self.assertIn("tasmota32.bin", self.window.firmware_source.text())
        self.assertNotIn("tasmota32-ES.bin", self.window.firmware_source.text())

    def test_backups_webui_fixed_folder_and_explicit_failures(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtWidgets import QTextEdit
        self.window.navigate("Configuración")
        with patch.object(self.service, "list_backups", return_value=[
            {"name": "Copy.dmp", "size": 1024, "modified": "2026-10-10"}
        ]):
            self.window.show_backups(self.ip)
        self.assertIn("Copy.dmp", self.window.cards[self.ip].findChild(QTextEdit).toPlainText())
        with patch("tasmodesk.QDesktopServices.openUrl", return_value=True) as open_url:
            self.window.open_webui(self.ip)
            open_url.assert_called_once_with(QUrl("http://" + self.ip))
            with self.assertRaises(Exception):
                self.window.open_webui("example.com")
            self.assertEqual(open_url.call_count, 1)
        with patch("pathlib.Path.mkdir") as mkdir, patch.object(self.window, "open_url") as open_url:
            self.window.open_backup_folder()
            mkdir.assert_called_once_with(parents=True, exist_ok=True)
            open_url.assert_called_once_with(QUrl.fromLocalFile(str(self.service.backup_directory())))
        with patch("tasmodesk.QDesktopServices.openUrl", return_value=False):
            self.assertFalse(self.window.invoke(lambda: self.window.open_webui(self.ip)))

    def test_demo_disables_network_controls_and_poll_uses_auto_tick(self) -> None:
        from PySide6.QtWidgets import QPushButton, QSlider
        self.service.demo = True
        self.service._touch()
        with patch.object(self.service, "auto_refresh_tick") as tick:
            self.window.poll()
            tick.assert_called_once()
        self.assertFalse(self.window.refresh_button.isEnabled())
        self.assertFalse(self.window.discover_button.isEnabled())
        self.assertTrue(all(not b.isEnabled() for b in self.window.grid_widget.findChildren(QPushButton)
                            if b.objectName().startswith("power-")))
        self.assertTrue(all(not s.isEnabled() for s in self.window.grid_widget.findChildren(QSlider)))

    def test_rendering_never_opens_temporary_windows(self) -> None:
        from PySide6.QtCore import QEvent, QObject
        from PySide6.QtWidgets import QPushButton
        from tasmodesk import PAGES
        shown: list[str] = []
        window = self.window
        class Watch(QObject):
            def eventFilter(self, watched, event):
                if (event.type() == QEvent.Type.Show and watched.isWidgetType()
                        and watched.isWindow() and watched is not window):
                    shown.append(type(watched).__name__)
                return False
        watch = Watch()
        self.app.installEventFilter(watch)
        try:
            with patch.object(self.service, "change"):
                self.window.findChild(QPushButton, f"power-{self.ip}-1").click()
            for page in PAGES * 2:
                self.window.navigate(page)
                self.app.processEvents()
            self.window.notify("Aviso")
            self.window.dismiss(self.window.notifications[0])
            self.app.processEvents()
        finally:
            self.app.removeEventFilter(watch)
        self.assertEqual(shown, [])

    def test_cards_keep_equal_height_and_layout_is_compact(self) -> None:
        from PySide6.QtWidgets import QPushButton
        status = self.service.devices[self.ip]
        self.service._record_status(self.ip, replace(status, relays=status.relays * 2))
        self.service._touch()
        self.window.poll()
        self.app.processEvents()
        heights = {card.height() for card in self.window.cards.values()}
        self.assertEqual(len(heights), 1)
        self.assertLessEqual(max(c.parentWidget().height() for c in self.window.counts), 46)
        self.window.navigate("Actualizaciones")
        self.app.processEvents()
        tops = {b.mapTo(self.window, b.rect().topLeft()).y()
                for b in self.window.firmware_frame.findChildren(QPushButton)}
        self.assertEqual(len(tops), 1)
        self.assertTrue(self.window.findChild(QPushButton, "local-" + self.ip).isEnabled())

    def test_refresh_keeps_device_controls_enabled(self) -> None:
        from PySide6.QtWidgets import QPushButton
        self.service.busy = "Consultando dispositivos"
        self.service.busy_exclusive = False
        self.service._touch()
        self.window.poll()
        self.assertFalse(self.window.refresh_button.isEnabled())
        self.assertTrue(self.window.findChild(QPushButton, f"power-{self.ip}-1").isEnabled())
        self.service.busy_exclusive = True
        self.service._touch()
        self.window.poll()
        self.assertFalse(self.window.findChild(QPushButton, f"power-{self.ip}-1").isEnabled())

    def test_local_file_selection_for_one_device_and_for_all(self) -> None:
        with patch("tasmodesk.QFileDialog.getOpenFileName", return_value=("", "")), patch.object(
            self.service, "local_update_confirmation"
        ) as confirmation:
            self.window.local_update_ui(self.ip)
            confirmation.assert_not_called()
        for ip in (self.ip, ""):
            with patch("tasmodesk.QFileDialog.getOpenFileName", return_value=("custom.bin", "")), patch.object(
                self.service, "local_update_confirmation", return_value=("Confirmar", "abc")
            ) as confirmation, patch.object(self.window, "confirm", return_value=True), patch.object(
                self.service, "start_local_updates"
            ) as start:
                self.window.local_update_ui(ip)
                confirmation.assert_called_once_with("custom.bin", ip)
                start.assert_called_once_with("custom.bin", "abc", ip)
        with patch("tasmodesk.QFileDialog.getOpenFileName", return_value=("custom.bin", "")), patch.object(
            self.service, "local_update_confirmation", return_value=("Confirmar", "abc")
        ), patch.object(self.window, "confirm", return_value=False), patch.object(
            self.service, "start_local_updates"
        ) as start:
            self.window.local_update_ui("")
            start.assert_not_called()


if __name__ == "__main__":
    unittest.main()

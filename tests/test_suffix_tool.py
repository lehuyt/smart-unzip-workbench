import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QMimeData, QPointF, QUrl, Qt
from PySide6.QtGui import QDropEvent
from PySide6.QtWidgets import QApplication, QMessageBox

from suffix_tool import (
    MODE_APPEND,
    MODE_CHANGE,
    FileDropArea,
    FileSuffixModifierDialog,
    filename_without_delete_character,
    normalize_suffix,
    rename_without_overwrite,
    renamed_filename,
)


class SuffixNameTests(unittest.TestCase):
    def test_change_and_append_suffix(self):
        self.assertEqual(renamed_filename("archive.tar.gz", MODE_CHANGE, ".7z"), "archive.tar.7z")
        self.assertEqual(renamed_filename("archive.tar.gz", MODE_APPEND, "zip"), "archive.tar.gz.zip")

    def test_extensionless_filename(self):
        self.assertEqual(renamed_filename("archive", MODE_CHANGE, "zip"), "archive.zip")
        self.assertEqual(renamed_filename("archive", MODE_APPEND, "zip"), "archive.zip")

    def test_suffix_validation(self):
        self.assertEqual(normalize_suffix(" .rar "), "rar")
        with self.assertRaises(ValueError):
            normalize_suffix(" ")
        with self.assertRaises(ValueError):
            normalize_suffix("../other")

    def test_remove_delete_markers_from_extension(self):
        self.assertEqual(filename_without_delete_character("model.r删ar删"), "model.rar")
        self.assertEqual(filename_without_delete_character("no_extension"), "no_extension")

    def test_rename_refuses_to_overwrite_existing_target(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory:
            source = Path(directory) / "source.zip"
            target = Path(directory) / "source.7z"
            source.write_text("source", encoding="utf-8")
            target.write_text("keep", encoding="utf-8")

            self.assertFalse(rename_without_overwrite(source, target))
            self.assertEqual(source.read_text(encoding="utf-8"), "source")
            self.assertEqual(target.read_text(encoding="utf-8"), "keep")


class SuffixToolUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_drop_area_accepts_local_files(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory:
            file_path = Path(directory) / "dragged.zip"
            file_path.write_text("test", encoding="utf-8")
            area = FileDropArea("drop")
            received = []
            area.files_dropped.connect(received.extend)
            mime = QMimeData()
            mime.setUrls([QUrl.fromLocalFile(str(file_path))])
            event = QDropEvent(
                QPointF(8, 8),
                Qt.DropAction.CopyAction,
                mime,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )

            area.dropEvent(event)
            self.assertTrue(event.isAccepted())
            self.assertEqual(
                [os.path.normcase(os.path.normpath(path)) for path in received],
                [os.path.normcase(os.path.normpath(str(file_path)))],
            )

    def test_drop_then_change_suffix_renames_file(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory:
            source = Path(directory) / "dragged.zip"
            source.write_text("test", encoding="utf-8")
            dialog = FileSuffixModifierDialog()
            dialog.set_dropped_files([str(source)])
            dialog.suffix_input.setText("7z")
            with patch.object(QMessageBox, "information"):
                dialog.modify_files()
            target = Path(directory) / "dragged.7z"
            self.assertTrue(target.is_file())
            self.assertFalse(source.exists())
            dialog.close()

    def test_main_toolbar_buttons_have_outline_and_tool_window_is_reused(self):
        import importlib.util

        source = Path(__file__).resolve().parents[1] / "智能解压V1.0.py"
        spec = importlib.util.spec_from_file_location("suffix_tool_main_app", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        app_class = module.DecompressApp

        with (
            patch.object(app_class, "load_config", lambda self: setattr(self, "config", {
                "winrar_path": "",
                "sevenzip_path": "",
                "password_book_path": "",
                "temporary_password_book_path": "",
            })),
            patch.object(app_class, "load_password_book", lambda self: []),
            patch.object(app_class, "load_temporary_password_book", lambda self: []),
            patch.object(app_class, "log", lambda self, *_args: None),
        ):
            window = app_class()

        self.assertEqual(window.btn_tools.text(), "工具")
        for button in window.root.findChildren(type(window.btn_tools)):
            if button.text() in ("清除选中", "清空列表", "工具"):
                self.assertEqual(button.property("variant"), "secondary")
        self.assertIn("border: 1px solid", window.qt_app.styleSheet())

        window.root.resize(920, 600)
        window.root.show()
        self.app.processEvents()
        toolbar = window.btn_tools.parentWidget()
        toolbar_buttons = toolbar.findChildren(type(window.btn_tools))
        for index, button in enumerate(toolbar_buttons):
            self.assertGreater(button.width(), 0)
            self.assertGreaterEqual(button.geometry().left(), 0)
            self.assertLessEqual(button.geometry().right(), toolbar.width())
            for other in toolbar_buttons[index + 1:]:
                self.assertFalse(button.geometry().intersects(other.geometry()))
        first = window.btn_tools
        first.click()
        tool_window = window.suffix_tool_window
        self.assertIsInstance(tool_window, FileSuffixModifierDialog)
        first.click()
        self.assertIs(window.suffix_tool_window, tool_window)
        self.assertEqual(len(window.root.findChildren(FileSuffixModifierDialog)), 1)
        tool_window.close()
        window.root.close()


if __name__ == "__main__":
    unittest.main()

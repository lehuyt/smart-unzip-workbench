import os
import re

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


MODE_CHANGE = "修改后缀"
MODE_APPEND = "添加后缀"
INVALID_SUFFIX_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def normalize_suffix(value):
    suffix = str(value or "").strip()
    if suffix.startswith("."):
        suffix = suffix[1:]
    if not suffix:
        raise ValueError("请输入要修改或添加的后缀。")
    if INVALID_SUFFIX_CHARS.search(suffix):
        raise ValueError("后缀不能包含 Windows 文件名禁用字符。")
    return suffix


def renamed_filename(file_name, mode, suffix):
    suffix = normalize_suffix(suffix)
    base_name, extension = os.path.splitext(file_name)
    if mode == MODE_CHANGE:
        return f"{base_name}.{suffix}"
    if mode == MODE_APPEND:
        return f"{base_name}{extension}.{suffix}"
    raise ValueError(f"不支持的修改模式：{mode}")


def filename_without_delete_character(file_name):
    base_name, extension = os.path.splitext(file_name)
    if not extension:
        return file_name
    new_extension = extension.replace("删", "")
    if not new_extension:
        new_extension = "."
    return base_name + new_extension


def rename_without_overwrite(source_path, target_path):
    source = os.path.abspath(source_path)
    target = os.path.abspath(target_path)
    if os.path.normcase(source) == os.path.normcase(target):
        return True
    if os.path.exists(target):
        return False
    os.rename(source, target)
    return True


class FileDropArea(QGroupBox):
    files_dropped = Signal(list)

    def __init__(self, title, parent=None):
        super().__init__(title, parent)
        self.setAcceptDrops(True)

    @staticmethod
    def _local_file_paths(event):
        if not event.mimeData().hasUrls():
            return []
        return [
            url.toLocalFile()
            for url in event.mimeData().urls()
            if url.isLocalFile()
        ]

    def dragEnterEvent(self, event):
        if any(os.path.isfile(path) for path in self._local_file_paths(event)):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if any(os.path.isfile(path) for path in self._local_file_paths(event)):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        paths = []
        seen = set()
        for path in self._local_file_paths(event):
            normalized = os.path.normcase(os.path.abspath(path))
            if os.path.isfile(path) and normalized not in seen:
                seen.add(normalized)
                paths.append(path)
        if not paths:
            event.ignore()
            return
        event.acceptProposedAction()
        self.files_dropped.emit(paths)


class FileSuffixModifierDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle("文件后缀修改工具")
        self.resize(640, 500)
        self.setMinimumSize(560, 430)
        self.setModal(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        self.drop_area = FileDropArea("拖放文件到这里")
        drop_layout = QVBoxLayout(self.drop_area)
        self.file_list = QListWidget()
        self.file_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.file_list.setMinimumHeight(120)
        drop_layout.addWidget(self.file_list)
        self.drop_area.files_dropped.connect(self.set_dropped_files)
        layout.addWidget(self.drop_area, 1)

        mode_group = QGroupBox("修改模式")
        mode_layout = QHBoxLayout(mode_group)
        mode_layout.addWidget(QLabel("选择模式："))
        self.mode_combo = QComboBox()
        self.mode_combo.addItems([MODE_CHANGE, MODE_APPEND])
        self.mode_combo.currentTextChanged.connect(self.update_hint)
        mode_layout.addWidget(self.mode_combo)
        mode_layout.addStretch(1)
        layout.addWidget(mode_group)

        suffix_group = QGroupBox("后缀设置")
        suffix_layout = QVBoxLayout(suffix_group)
        input_layout = QHBoxLayout()
        input_layout.addWidget(QLabel("输入后缀："))
        self.suffix_input = QLineEdit()
        self.suffix_input.setPlaceholderText("例如：txt、jpg（不需要加点）")
        self.suffix_input.textChanged.connect(self.update_hint)
        input_layout.addWidget(self.suffix_input, 1)
        suffix_layout.addLayout(input_layout)

        common_layout = QHBoxLayout()
        common_layout.addWidget(QLabel("常用后缀："))
        for suffix in ("zip", "rar", "7z"):
            button = QPushButton(f".{suffix}")
            button.clicked.connect(
                lambda _checked=False, selected=suffix: self.suffix_input.setText(selected)
            )
            common_layout.addWidget(button)
        common_layout.addStretch(1)
        suffix_layout.addLayout(common_layout)
        layout.addWidget(suffix_group)

        special_group = QGroupBox("特殊功能")
        special_layout = QHBoxLayout(special_group)
        self.remove_delete_button = QPushButton("去除删字")
        self.remove_delete_button.setToolTip("移除文件扩展名中的“删”，例如 .r删ar 则变为 .rar")
        self.remove_delete_button.clicked.connect(self.remove_delete_characters)
        special_layout.addWidget(self.remove_delete_button)
        special_layout.addStretch(1)
        layout.addWidget(special_group)

        button_layout = QHBoxLayout()
        self.execute_button = QPushButton("执行修改")
        self.execute_button.setProperty("variant", "primary")
        self.execute_button.clicked.connect(self.modify_files)
        self.clear_button = QPushButton("清空列表")
        self.clear_button.setProperty("variant", "secondary")
        self.clear_button.clicked.connect(self.clear_file_list)
        button_layout.addWidget(self.execute_button)
        button_layout.addWidget(self.clear_button)
        layout.addLayout(button_layout)

        self.hint_label = QLabel()
        self.hint_label.setObjectName("Muted")
        self.hint_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint_label.setWordWrap(True)
        layout.addWidget(self.hint_label)
        self.update_hint()

    def set_dropped_files(self, paths):
        self.file_list.clear()
        self.file_list.addItems(paths)
        self.update_hint()

    def clear_file_list(self):
        self.file_list.clear()
        self.update_hint()

    def update_hint(self, *_args):
        if self.file_list.count() == 0:
            text = "提示：请将需要修改后缀的文件拖放到上方区域。"
        elif not self.suffix_input.text().strip():
            text = "提示：请输入要修改或添加的后缀，也可以选择常用后缀。"
        elif self.mode_combo.currentText() == MODE_CHANGE:
            text = f"将文件扩展名改为 .{self.suffix_input.text().strip()}。"
        else:
            text = f"在文件原扩展名后添加 .{self.suffix_input.text().strip()}。"
        self.hint_label.setText(text)

    def _rename_paths(self, target_name_for):
        if self.file_list.count() == 0:
            QMessageBox.warning(self, "提示", "请先拖放文件到列表。")
            return

        succeeded = 0
        failed = 0
        for index in range(self.file_list.count()):
            source = self.file_list.item(index).text()
            directory, file_name = os.path.split(source)
            try:
                target_name = target_name_for(file_name)
                target = os.path.join(directory, target_name)
                if rename_without_overwrite(source, target):
                    succeeded += 1
                else:
                    failed += 1
            except (OSError, ValueError):
                failed += 1

        QMessageBox.information(
            self,
            "完成",
            f"操作完成！\n成功：{succeeded} 个文件\n失败：{failed} 个文件",
        )
        self.clear_file_list()

    def modify_files(self):
        if self.file_list.count() == 0:
            QMessageBox.warning(self, "提示", "请先拖放文件到列表。")
            return
        try:
            suffix = normalize_suffix(self.suffix_input.text())
        except ValueError as error:
            QMessageBox.warning(self, "后缀无效", str(error))
            return
        mode = self.mode_combo.currentText()
        self._rename_paths(lambda name: renamed_filename(name, mode, suffix))

    def remove_delete_characters(self):
        self._rename_paths(filename_without_delete_character)

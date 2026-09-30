import sys
import os
import re
import json
import html as html_lib
import subprocess
import threading
import datetime
import shutil  
import time 
import webbrowser
from PySide6.QtCore import QEvent, QObject, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QFontDatabase, QTextCharFormat, QTextCursor
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGraphicsDropShadowEffect,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from suffix_tool import FileSuffixModifierDialog


class TaskTable(QTableWidget):
    files_dropped = Signal(list)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setAcceptDrops(True)
        self.drop_viewport = self.viewport()
        self.drop_viewport.setAcceptDrops(True)
        self.drop_viewport.installEventFilter(self)

    def _accept_file_drop(self, event, emit=False):
        mime_data = event.mimeData()
        if not mime_data.hasUrls():
            return False
        paths = [url.toLocalFile() for url in mime_data.urls() if url.isLocalFile()]
        if not paths:
            event.ignore()
            self._set_drop_active(False)
            return True
        if not emit:
            self._set_drop_active(True)
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()
        if emit:
            self._set_drop_active(False)
            self.files_dropped.emit(paths)
        return True

    def _set_drop_active(self, active):
        if self.property("dropActive") == active:
            return
        self.setProperty("dropActive", active)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def eventFilter(self, watched, event):
        if watched == self.drop_viewport:
            event_type = event.type()
            if event_type in (QEvent.Type.DragEnter, QEvent.Type.DragMove):
                if self._accept_file_drop(event):
                    return True
            elif event_type == QEvent.Type.Drop:
                if self._accept_file_drop(event, emit=True):
                    return True
            elif event_type == QEvent.Type.DragLeave:
                self._set_drop_active(False)
        return super().eventFilter(watched, event)

    def dragEnterEvent(self, event):
        if self._accept_file_drop(event):
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if self._accept_file_drop(event):
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event):
        if self._accept_file_drop(event, emit=True):
            return
        super().dropEvent(event)


class SourceLinkEdit(QLineEdit):
    double_clicked = Signal()

    def mouseDoubleClickEvent(self, event):
        self.double_clicked.emit()
        super().mouseDoubleClickEvent(event)


class DecompressApp(QObject):
    APP_VERSION = "1.2"
    LATEST_RELEASE_API = "https://api.github.com/repos/lehuyt/smart-unzip-workbench/releases/latest"
    RELEASE_PAGE_PREFIX = "/lehuyt/smart-unzip-workbench/releases/"
    UPDATE_REQUEST_TIMEOUT_MS = 10000

    log_signal = Signal(str, str, str)
    task_status_signal = Signal(int, str, str)
    task_progress_signal = Signal(int, str)
    runtime_status_signal = Signal(str, str)
    overview_signal = Signal()
    password_signal = Signal(int, str)
    undo_signal = Signal(bool)
    controls_signal = Signal(bool, bool, str, str, str)
    completion_signal = Signal(str)

    LARGE_FILE_THRESHOLD = 100 * 1024 * 1024
    SINGLE_EXTENSIONLESS_FOLDER_THRESHOLD = 50 * 1024 * 1024
    LARGE_FILE_ZIP_EXTENSIONS = frozenset({
        ".txt", ".jpg", ".png", ".gif", ".webp", ".webm", ".jpeg",
    })
    COMMON_ARCHIVE_EXTENSIONS = frozenset({
        ".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".zst",
        ".cab", ".iso", ".arj", ".lzh", ".ace", ".jar", ".apk", ".xpi",
        ".z", ".lz", ".lz4", ".br", ".001", ".r00", ".r01", ".z01",
    })

    def __init__(self):
        self.qt_app = QApplication.instance() or QApplication(sys.argv)
        super().__init__()
        self.root = QMainWindow()
        self.root.setWindowTitle("智能解压工作台 · Smart Unzip")
        self.root.resize(1180, 760)
        self.root.setMinimumSize(920, 600)
        self._setup_theme()

        self.files_data = []
        self.next_task_id = 1
        self.rename_history = [] 
        self.is_running = False 
        self.stop_requested = threading.Event()
        self.current_process = None
        self.last_extraction_stopped = False
        self.current_task_index = 0
        self.resume_index = 0
        self.process_thread = None
        self.engine_type = 1
        self.suffix_tool_window = None
        # 按原有功能固定为：处理完成后自动还原被清理的文件名。
        self.auto_undo_enabled = True
        
        # 配置和默认密码本均以脚本/程序所在目录为基准，避免从其他目录启动时找不到文件。
        if getattr(sys, "frozen", False):
            self.app_dir = os.path.dirname(os.path.abspath(sys.executable))
        else:
            self.app_dir = os.path.dirname(os.path.abspath(__file__))

        self.config_file = os.path.join(self.app_dir, "config.json")
        # 执行日志与解压引擎原生调试输出分开保存。
        # “查看日志”只打开执行日志，内容与界面中的执行日志保持一致。
        self.execution_log_file = os.path.join(self.app_dir, "execution.log")
        self.debug_log_file = os.path.join(self.app_dir, "debug_decompress.log")
        self.execution_log_lock = threading.Lock()
        self.log_signal.connect(self._append_log, Qt.ConnectionType.QueuedConnection)
        self.task_status_signal.connect(self._set_task_status, Qt.ConnectionType.QueuedConnection)
        self.task_progress_signal.connect(self._set_task_progress, Qt.ConnectionType.QueuedConnection)
        self.runtime_status_signal.connect(self._set_runtime_status, Qt.ConnectionType.QueuedConnection)
        self.overview_signal.connect(self._refresh_overview, Qt.ConnectionType.QueuedConnection)
        self.password_signal.connect(self._set_task_password, Qt.ConnectionType.QueuedConnection)
        self.undo_signal.connect(self._set_undo_available, Qt.ConnectionType.QueuedConnection)
        self.controls_signal.connect(self._set_processing_controls, Qt.ConnectionType.QueuedConnection)
        self.completion_signal.connect(self._show_completion, Qt.ConnectionType.QueuedConnection)

        self.load_config()
        self.password_book = self.load_password_book()
        self.temporary_password_records = self.load_temporary_password_book()
        self.successful_passwords = [] 

        self.winrar_candidates = [r"C:\Program Files\WinRAR\WinRAR.exe", r"C:\Program Files (x86)\WinRAR\WinRAR.exe", r"D:\Program Files\WinRAR\WinRAR.exe"]
        self.sevenzip_candidates = [r"C:\Program Files\7-Zip\7z.exe", r"D:\Program Files\7-Zip\7z.exe"]

        self._create_ui()
        self.root.show()
        self.log("系统就绪。已开启【高频密码动态记忆提速】机制。", "INFO")

    def _setup_theme(self):
        """建立浅色业务工作台主题，令牌与 design.md 保持一致。"""
        self.colors = {
            "bg": "#F4F6F5",
            "surface": "#FFFFFF",
            "surface_2": "#F7F9F8",
            "panel": "#FFFFFF",
            "panel_alt": "#F7F9F8",
            "border": "#E4EAE7",
            "border_soft": "#E4EAE7",
            "text": "#17201D",
            "muted": "#6F7975",
            "white": "#ffffff",
            "primary": "#2AA88F",
            "primary_hover": "#258F7A",
            "primary_pressed": "#1D7768",
            "primary_soft": "#E0F6F0",
            "success": "#249B7F",
            "success_hover": "#17856D",
            "success_soft": "#E2F5EF",
            "warning": "#B77B12",
            "warning_hover": "#986711",
            "warning_soft": "#FFF3D5",
            "danger": "#D65353",
            "danger_hover": "#BF4545",
            "danger_soft": "#FCE8E7",
            "info": "#4386B5",
            "info_soft": "#E8F2F8",
            "button_secondary_hover": "#F1F5F3",
            "button_subtle_hover": "#F1F5F3",
            "button_ghost_hover": "#F1F5F3",
            "disabled": "#A8B0AD",
            "disabled_text": "#919A96",
            "entry_bg": "#FFFFFF",
            "log_bg": "#FFFFFF",
            "log_text": "#17201D",
            "log_time": "#6F7975",
            "log_info": "#4386B5",
            "log_success": "#249B7F",
            "log_error": "#D65353",
            "log_action": "#2AA88F",
            "table_success_bg": "#E2F5EF",
            "table_error_bg": "#FCE8E7",
            "table_processing_bg": "#E8F2F8",
        }

        available_families = {
            family.casefold(): family for family in QFontDatabase.families()
        }
        font_family = next(
            (available_families[name.casefold()] for name in ("Microsoft YaHei UI", "Noto Sans SC", "Segoe UI") if name.casefold() in available_families),
            QApplication.font().family(),
        )
        self.fonts = {
            "family": font_family,
            "mono": "Cascadia Mono",
            "body_size": 14,
            "small_size": 12,
            "heading_size": 16,
            "title_size": 22,
            "large_size": 24,
            "mono_size": 14,
        }
        family = self.fonts["family"]
        app_font = QFont(family)
        app_font.setPixelSize(self.fonts["body_size"])
        self.qt_app.setFont(app_font)
        self.qt_app.setStyle("Fusion")
        self.qt_app.setStyleSheet(f"""
            QMainWindow, QDialog {{ background: {self.colors['bg']}; color: {self.colors['text']}; }}
            QWidget {{ color: {self.colors['text']}; font-family: "{family}"; font-size: 14px; }}
            QFrame#Panel {{ background: {self.colors['panel']}; border: 1px solid {self.colors['border']}; border-radius: 8px; }}
            QFrame#Toolbar {{ background: {self.colors['panel']}; border: 1px solid {self.colors['border']}; border-radius: 8px; }}
            QLabel#PageTitle {{ font-size: 22px; font-weight: 700; }}
            QLabel#SectionTitle {{ font-size: 16px; font-weight: 600; }}
            QLabel#Muted {{ color: {self.colors['muted']}; font-size: 12px; }}
            QPushButton {{ min-height: 34px; padding: 0 12px; border: 1px solid {self.colors['border']}; border-radius: 6px; background: #FFFFFF; color: {self.colors['text']}; }}
            QPushButton:hover {{ background: {self.colors['button_secondary_hover']}; }}
            QPushButton:disabled {{ color: {self.colors['disabled_text']}; background: {self.colors['surface_2']}; }}
            QPushButton[variant="primary"] {{ background: #151918; border-color: #151918; color: #FFFFFF; font-weight: 600; }}
            QPushButton[variant="primary"]:hover {{ background: #2A3230; border-color: #2A3230; }}
            QPushButton[variant="danger"] {{ background: {self.colors['danger_soft']}; border-color: {self.colors['danger_soft']}; color: {self.colors['danger']}; }}
            QPushButton[variant="ghost"] {{ background: transparent; border-color: transparent; }}
            QLineEdit {{ min-height: 34px; padding: 0 9px; border: 1px solid {self.colors['border']}; border-radius: 6px; background: #FFFFFF; selection-background-color: {self.colors['primary_soft']}; }}
            QLineEdit:focus {{ border: 1px solid {self.colors['primary']}; }}
            QTextEdit {{ border: none; background: #FFFFFF; }}
            QTableWidget {{ border: none; background: #FFFFFF; gridline-color: {self.colors['border']}; selection-background-color: {self.colors['primary_soft']}; selection-color: {self.colors['text']}; outline: none; }}
            QTableWidget::item {{ padding: 5px 6px; border-bottom: 1px solid {self.colors['border']}; }}
            QTableWidget[dropActive="true"] {{ border: 2px dashed {self.colors['primary']}; border-radius: 6px; background: {self.colors['primary_soft']}; }}
            QHeaderView::section {{ padding: 9px 8px; border: none; border-bottom: 1px solid {self.colors['border']}; background: {self.colors['panel_alt']}; color: {self.colors['muted']}; font-size: 12px; font-weight: 600; }}
            QRadioButton {{ spacing: 6px; padding: 7px 10px; border: 1px solid {self.colors['border']}; border-radius: 6px; background: #FFFFFF; }}
            QRadioButton:checked {{ border-color: {self.colors['primary']}; background: {self.colors['primary_soft']}; color: {self.colors['text']}; }}
            QMenu {{ padding: 4px; border: 1px solid {self.colors['border']}; border-radius: 6px; background: #FFFFFF; }}
            QMenu::item {{ padding: 7px 20px; border-radius: 4px; }}
            QMenu::item:selected {{ background: {self.colors['primary_soft']}; }}
            QScrollBar:vertical {{ width: 10px; margin: 2px; background: transparent; }}
            QScrollBar::handle:vertical {{ min-height: 24px; border-radius: 4px; background: #CBD4D0; }}
            QScrollBar:horizontal {{ height: 10px; margin: 2px; background: transparent; }}
            QScrollBar::handle:horizontal {{ min-width: 24px; border-radius: 4px; background: #CBD4D0; }}
            QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
        """)

    def load_config(self):
        self.config = {
            "winrar_path": "",
            "sevenzip_path": "",
            "password_book_path": os.path.join(self.app_dir, "密码本.txt"),
            "temporary_password_book_path": os.path.join(self.app_dir, "临时密码本.txt"),
        }
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    self.config.update(json.load(f))
            except Exception: pass

    def resolve_config_path(self, path, default_filename):
        """解析密码本路径：相对路径默认相对于程序目录。"""
        path = str(path or "").strip()
        if not path:
            path = os.path.join(self.app_dir, default_filename)
        elif not os.path.isabs(path):
            path = os.path.join(self.app_dir, path)
        return os.path.normpath(path)

    def save_config(self):
        try:
            # 自动还原是固定行为，不再写入或读取设置项。
            self.config.pop("auto_undo", None)
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, ensure_ascii=False, indent=4)
            self.password_book = self.load_password_book()
            self.temporary_password_records = self.load_temporary_password_book()
        except Exception as e:
            self.log(f"保存配置失败: {e}", "ERROR")

    def load_password_book(self):
        book = []
        path = self.resolve_config_path(self.config.get("password_book_path", ""), "密码本.txt")
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    for line in f:
                        pwd = line.strip()
                        if pwd and pwd not in book:
                            book.append(pwd)
                self.log(f"成功加载密码本，共读取 {len(book)} 个密码", "INFO")
            except Exception as e:
                self.log(f"无法读取密码本: {str(e)}", "ERROR")
        return book

    def split_temporary_passwords(self, value):
        """拆分临时密码本中的一行候选密码，同时保留密码内部的空格。"""
        value = str(value or "").strip()
        if not value:
            return []

        # 去掉常见的外层引号和可信度备注，不拆分密码内部的普通空格。
        value = value.strip('"\'“”‘’')
        value = re.sub(r"\s*[（(]\s*(?:可信度|置信度|可信|概率|优先级).*?[）)]\s*$", "", value, flags=re.IGNORECASE)

        parts = re.split(r"\s*(?:\|\||\||、|,|，|;|；)\s*", value)
        result = []
        for part in parts:
            password = part.strip().strip('"\'“”‘’')
            if password and password not in result:
                result.append(password)
        return result

    def load_temporary_password_book(self):
        """读取按文件名分组的临时密码本，保留文件中的候选密码顺序。"""
        records = []
        path = self.resolve_config_path(
            self.config.get("temporary_password_book_path", ""),
            "临时密码本.txt"
        )

        if not os.path.exists(path):
            self.log(f"未找到临时密码本: {path}，将跳过临时密码搜索", "INFO")
            return records

        try:
            try:
                with open(path, 'r', encoding='utf-8-sig') as f:
                    text = f.read()
            except UnicodeDecodeError:
                with open(path, 'r', encoding='gb18030') as f:
                    text = f.read()

            # 每个 ### 分隔块是一条文件/文件夹记录；没有分隔符时也支持整篇作为一块处理。
            blocks = re.split(r"(?m)^\s*#{3,}\s*$", text)
            name_pattern = re.compile(
                r"^\s*(?:文件名|文件夹名|目录名|名称)\s*[:：]\s*(.*?)\s*$",
                re.IGNORECASE
            )
            password_pattern = re.compile(
                r"^\s*(?:候选\s*)?(?:解压密码|解压码|压缩密码|密码|password|pwd)\s*\d*\s*[:：]\s*(.*?)\s*$",
                re.IGNORECASE
            )

            for block in blocks:
                file_name = ""
                passwords = []
                source_info = {
                    "file_name": "",
                    "original_url": "",
                    "cloud_url": "",
                    "source": "",
                    "saved": "",
                }
                for line in block.splitlines():
                    name_match = name_pattern.match(line)
                    if name_match and not file_name:
                        file_name = name_match.group(1).strip()
                        source_info["file_name"] = file_name
                        continue

                    password_match = password_pattern.match(line)
                    if password_match:
                        for password in self.split_temporary_passwords(password_match.group(1)):
                            if password not in passwords:
                                passwords.append(password)

                    field_match = re.match(r"^\s*(原网页链接|网页链接|原网页|网盘链接|网盘|来源|保存)\s*[:：]\s*(.*?)\s*$", line, re.IGNORECASE)
                    if field_match:
                        field_name, field_value = field_match.group(1), field_match.group(2).strip()
                        if field_name in ("原网页链接", "网页链接", "原网页"):
                            source_info["original_url"] = field_value
                        elif field_name == "网盘链接":
                            source_info["cloud_url"] = field_value
                        elif field_name == "来源":
                            source_info["source"] = field_value
                        elif field_name == "保存":
                            source_info["saved"] = field_value

                # 即使某条记录暂时没有候选密码，也保留它的源信息；
                # 任务列表的“源信息”状态只代表是否检索到文件名记录。
                if file_name:
                    records.append({
                        "name": file_name,
                        "passwords": passwords,
                        "source_info": source_info,
                    })

            candidate_count = sum(len(record["passwords"]) for record in records)
            self.log(
                f"成功加载临时密码本，共读取 {len(records)} 条文件名记录、{candidate_count} 个候选密码",
                "INFO"
            )
        except Exception as e:
            self.log(f"无法读取临时密码本: {str(e)}", "ERROR")

        return records

    def get_lookup_name_variants(self, value):
        """生成用于匹配的文件名/文件夹名变体，兼容压缩扩展名和分卷名。"""
        if not value:
            return []

        value = os.path.basename(str(value).strip('{} '))
        variants = []

        def add(item):
            item = str(item or "").strip().strip('"\'“”‘’')
            if not item:
                return

            # 大小写、全角字符和空白差异不影响文件名匹配。
            normalized = item.casefold()
            normalized = re.sub(r"\s+", "", normalized)
            if normalized and normalized not in variants:
                variants.append(normalized)

            # 再提供一个去除常见标点的变体，兼容记录名与实际文件名之间的符号差异。
            compact = re.sub(r"[^\w\u4e00-\u9fff]+", "", normalized, flags=re.UNICODE)
            if compact and compact not in variants:
                variants.append(compact)

        add(value)
        add(value.replace("删", ""))

        normalized_name, disguise_suffix = self.normalize_disguised_archive_name(value)
        if disguise_suffix:
            add(normalized_name)

        # 提取压缩文件扩展名之前的主体，兼容“文件名.zip”“文件名.part1.rar”等写法。
        stem = re.sub(
            r"\.part\d+(?:\.(?:rar|zip|7z|zst))?$",
            "",
            value,
            flags=re.IGNORECASE
        )
        stem = re.sub(
            r"\.(?:rar|zip|7z|tar|gz|tgz|bz2|xz|zst|001)(?:\.\d{3})?$",
            "",
            stem,
            flags=re.IGNORECASE
        )
        stem = re.sub(r"\.z\d+$", "", stem, flags=re.IGNORECASE)
        add(stem)

        # 某些伪装文件名可能在扩展名后附带密码，例如“文件名.zip密码”。
        archive_match = re.search(
            r"(?i)^(.+?\.(?:rar|zip|7z|tar|gz|zst|17z|mp4|001))(?:.*)$",
            value
        )
        if archive_match:
            archive_name = archive_match.group(1)
            add(archive_name)
            add(re.sub(r"\.(?:rar|zip|7z|tar|gz|zst|17z|mp4|001)$", "", archive_name, flags=re.IGNORECASE))

        return variants

    def get_lookup_attempt_names(self, file_path, lookup_names=None):
        """按优先级返回本次临时密码本检索要尝试的原始名称。"""
        if lookup_names is None:
            file_path = os.path.abspath(str(file_path))
            file_name = os.path.basename(file_path)
            file_dir = os.path.dirname(file_path)
            lookup_names = [
                file_name,
                os.path.basename(file_dir),
                os.path.basename(os.path.dirname(file_dir)),
            ]
        elif isinstance(lookup_names, str):
            lookup_names = [lookup_names]

        names = []
        for item in lookup_names:
            item = os.path.basename(str(item or "").strip("{} "))
            if item and item not in names:
                names.append(item)
        return names

    def get_target_lookup_names(self, file_path, lookup_names=None):
        """生成当前检索名称的全部变体。"""
        names = self.get_lookup_attempt_names(file_path, lookup_names)
        variants = []
        for name in names:
            for variant in self.get_lookup_name_variants(name):
                if variant not in variants:
                    variants.append(variant)
        return variants

    def get_name_match_score(self, record_name, target_variants):
        """为临时密码本记录评分：完整匹配优先，包含匹配作为兼容兜底。"""
        record_variants = self.get_lookup_name_variants(record_name)
        best_score = 0

        for record_variant in record_variants:
            for target_variant in target_variants:
                if record_variant == target_variant:
                    best_score = max(best_score, 100)
                elif len(record_variant) >= 3 and len(target_variant) >= 3:
                    if record_variant in target_variant or target_variant in record_variant:
                        # 越接近完整名称，分数越高；避免短名称压过精确匹配。
                        similarity = min(len(record_variant), len(target_variant)) / max(len(record_variant), len(target_variant))
                        best_score = max(best_score, 60 + int(similarity * 30))

        return best_score

    def _find_temporary_matches_for_name(self, lookup_name):
        """查找单个名称对应的临时密码本记录，并按匹配分数排序。"""
        target_variants = self.get_lookup_name_variants(lookup_name)
        matches = []
        for index, record in enumerate(self.temporary_password_records):
            score = self.get_name_match_score(record["name"], target_variants)
            if score:
                matches.append((score, index, record))
        matches.sort(key=lambda item: (-item[0], item[1]))
        return matches

    def find_temporary_passwords(self, file_path, lookup_names=None):
        """按检索优先级查找临时密码本中的候选密码。"""
        if not self.temporary_password_records:
            return []

        matched_record = False
        for attempt_index, lookup_name in enumerate(
            self.get_lookup_attempt_names(file_path, lookup_names),
            start=1,
        ):
            matches = self._find_temporary_matches_for_name(lookup_name)
            if not matches:
                self.log(
                    f"临时密码本第{attempt_index}次检索未命中“{lookup_name}”",
                    "INFO",
                )
                continue

            matched_record = True
            passwords = []
            for _, _, record in matches:
                for password in record["passwords"]:
                    if password not in passwords:
                        passwords.append(password)

            if passwords:
                self.log(
                    f"临时密码本第{attempt_index}次检索命中“{lookup_name}”，"
                    f"找到 {len(passwords)} 个候选密码",
                    "INFO",
                )
                return passwords

            self.log(
                f"临时密码本第{attempt_index}次检索命中“{lookup_name}”，"
                "但记录没有候选密码，继续下一次检索",
                "INFO",
            )

        if not matched_record:
            self.log(
                f"临时密码本未匹配到: {os.path.basename(file_path)}",
                "INFO",
            )
        return []

    def find_temporary_record(self, file_path, lookup_names=None):
        """按检索优先级返回第一条匹配记录，用于源信息展示。"""
        if not self.temporary_password_records:
            return None

        for lookup_name in self.get_lookup_attempt_names(file_path, lookup_names):
            matches = self._find_temporary_matches_for_name(lookup_name)
            if matches:
                return matches[0][2]
        return None

    def get_source_info_for_path(self, file_path, lookup_names=None):
        record = self.find_temporary_record(file_path, lookup_names)
        if record:
            info = dict(record.get("source_info", {}))
            info["file_name"] = info.get("file_name") or record.get("name", "")
            return info
        return None

    def get_source_marker_name(self, source_info):
        """把源信息中的“来源”转换为安全的 HTML 文件名。"""
        source = str((source_info or {}).get("source", "") or "").strip()
        if not source:
            return ""

        # 来源可能包含 URL、路径或换行；这些字符不能直接用于 Windows 文件名。
        safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", source)
        safe_name = re.sub(r"\s+", " ", safe_name).strip().rstrip(". ")
        if not safe_name or safe_name in (".", ".."):
            return ""

        reserved_names = {
            "CON", "PRN", "AUX", "NUL",
            *(f"COM{index}" for index in range(1, 10)),
            *(f"LPT{index}" for index in range(1, 10)),
        }
        if safe_name.split(".", 1)[0].upper() in reserved_names:
            safe_name += "_"

        safe_name = safe_name[:200].rstrip(". ")
        safe_name = re.sub(r"\.html$", "", safe_name, flags=re.IGNORECASE).rstrip(". ")
        return f"{safe_name}.html" if safe_name else ""

    def create_source_marker_file(self, output_dir, source_info):
        """在指定的一级目录生成来源 HTML，并自动导航到原网页。"""
        marker_name = self.get_source_marker_name(source_info)
        if not marker_name:
            return ""
        if not os.path.isdir(output_dir):
            return marker_name

        marker_path = os.path.join(output_dir, marker_name)
        original_url = str((source_info or {}).get("original_url", "") or "").strip()
        is_web_url = bool(re.match(r"^https?://", original_url, re.IGNORECASE))
        escaped_url = html_lib.escape(original_url, quote=True)
        escaped_title = html_lib.escape(os.path.splitext(marker_name)[0], quote=True)

        if is_web_url:
            redirect_tag = (
                f'<meta http-equiv="refresh" content="0;url={escaped_url}">'
            )
            link_html = (
                f'<a href="{escaped_url}">如果没有自动跳转，请点击打开原网页</a>'
            )
        else:
            redirect_tag = ""
            link_html = (
                "临时密码本中没有找到有效的原网页链接。"
                if not original_url
                else f"原网页链接格式无效：{html_lib.escape(original_url)}"
            )

        html_content = f"""<!doctype html>
<html lang="zh-CN">
<head>
    <meta charset="utf-8">
    {redirect_tag}
    <title>{escaped_title}</title>
</head>
<body>
    <p>{link_html}</p>
</body>
</html>
"""

        try:
            if os.path.isdir(marker_path):
                self.log(f"来源 HTML 文件名与目录冲突，跳过生成: {marker_name}", "ERROR")
            elif not os.path.exists(marker_path):
                with open(marker_path, "x", encoding="utf-8") as html_file:
                    html_file.write(html_content)
                self.log(f"已生成来源 HTML: {marker_name}", "ACTION")
            else:
                self.log(f"来源 HTML 已存在，跳过生成: {marker_name}", "INFO")
            return marker_name
        except FileExistsError:
            return marker_name
        except Exception as e:
            self.log(f"生成来源 HTML 失败: {e}", "ERROR")
            return marker_name

    def format_source_cell(self, file_path):
        """返回任务列表中的源信息状态：命中显示绿勾，未命中显示红叉。"""
        return "已找到" if self.find_temporary_record(file_path) else "未找到"

    def open_settings(self):
        dialog = QDialog(self.root)
        dialog.setWindowTitle("工作台设置")
        dialog.setMinimumWidth(720)
        dialog.setModal(True)

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(16)
        title = QLabel("工作台设置")
        title.setObjectName("PageTitle")
        subtitle = QLabel("配置解压引擎与密码本路径，保存后立即重新加载。")
        subtitle.setObjectName("Muted")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        card = QFrame()
        card.setObjectName("Panel")
        form = QFormLayout(card)
        form.setContentsMargins(18, 18, 18, 18)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(14)

        settings_fields = {
            "winrar_path": QLineEdit(self.config.get("winrar_path", "")),
            "sevenzip_path": QLineEdit(self.config.get("sevenzip_path", "")),
            "password_book_path": QLineEdit(self.config.get("password_book_path", "")),
            "temporary_password_book_path": QLineEdit(self.config.get("temporary_password_book_path", "")),
        }
        settings_rows = (
            ("winrar_path", "WinRAR 路径", "用于 WinRAR 引擎和伪装格式修复。"),
            ("sevenzip_path", "7-Zip 路径", "用于常规压缩包和无密码快速解压。"),
            ("password_book_path", "普通密码本", "临时密码搜索失败后的兜底列表。"),
            ("temporary_password_book_path", "临时密码本", "按文件名匹配的优先候选密码。"),
        )
        for key, label_text, hint in settings_rows:
            row_widget = QWidget()
            row_layout = QHBoxLayout(row_widget)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(8)
            field = settings_fields[key]
            field.setClearButtonEnabled(True)
            row_layout.addWidget(field, 1)
            browse_button = self._make_button(
                row_widget,
                "浏览",
                lambda _checked=False, target=field: self._browse_path(target),
                kind="secondary",
            )
            row_layout.addWidget(browse_button)
            label_widget = QWidget()
            label_layout = QVBoxLayout(label_widget)
            label_layout.setContentsMargins(0, 0, 0, 0)
            label_layout.setSpacing(3)
            label = QLabel(label_text)
            label.setStyleSheet("font-size: 14px; font-weight: 600;")
            hint_label = QLabel(hint)
            hint_label.setObjectName("Muted")
            label_layout.addWidget(label)
            label_layout.addWidget(hint_label)
            form.addRow(label_widget, row_widget)
        layout.addWidget(card)
        self._apply_card_shadow(card)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        footer.addWidget(self._make_button(dialog, "查看日志", self.open_debug_log_ui, "secondary"))
        update_button = self._make_button(dialog, "检查更新", None, "secondary")
        update_status = QLabel("")
        update_status.setObjectName("Muted")
        update_status.setMinimumWidth(150)
        footer.addWidget(update_button)
        footer.addWidget(update_status, 1)
        footer.addStretch(1)
        cancel = self._make_button(dialog, "取消", dialog.reject, "ghost")
        save_button = self._make_button(dialog, "保存设置", None, "primary")
        footer.addWidget(cancel)
        footer.addWidget(save_button)
        layout.addLayout(footer)

        def save():
            for key, field in settings_fields.items():
                self.config[key] = field.text().strip()
            self.save_config()
            self.refresh_source_info_columns()
            dialog.accept()
            self.log("配置已更新并保存", "SUCCESS")

        save_button.clicked.connect(save)
        update_manager = QNetworkAccessManager(dialog)
        update_button.clicked.connect(
            lambda _checked=False: self.check_for_updates(
                dialog, update_manager, update_button, update_status
            )
        )
        dialog.resize(780, 520)
        dialog.exec()

    @staticmethod
    def parse_release_version(value):
        """Parse a numeric release tag such as v1.2.0 into a comparable tuple."""
        match = re.fullmatch(r"[vV]?(\d+(?:\.\d+)*)", str(value or "").strip())
        if not match:
            return None
        parts = [int(part) for part in match.group(1).split(".")]
        while len(parts) > 1 and parts[-1] == 0:
            parts.pop()
        return tuple(parts)

    @classmethod
    def is_newer_release(cls, current_version, release_tag):
        current = cls.parse_release_version(current_version)
        latest = cls.parse_release_version(release_tag)
        if current is None or latest is None:
            return None
        return latest > current

    @classmethod
    def is_valid_release_url(cls, value):
        url = QUrl(str(value or ""))
        return (
            url.scheme().lower() == "https"
            and url.host().lower() == "github.com"
            and url.path().startswith(cls.RELEASE_PAGE_PREFIX)
        )

    def check_for_updates(self, dialog, manager, button, status_label):
        button.setEnabled(False)
        status_label.setStyleSheet(f"color: {self.colors['muted']};")
        status_label.setText("正在检查...")

        request = QNetworkRequest(QUrl(self.LATEST_RELEASE_API))
        request.setRawHeader(b"Accept", b"application/vnd.github+json")
        request.setRawHeader(b"User-Agent", b"SmartUnzipWorkbench")
        request.setTransferTimeout(self.UPDATE_REQUEST_TIMEOUT_MS)
        reply = manager.get(request)

        def finish_check():
            status_code = reply.attribute(
                QNetworkRequest.Attribute.HttpStatusCodeAttribute
            )
            response_bytes = bytes(reply.readAll())
            network_error = reply.error()
            reply.deleteLater()
            button.setEnabled(True)

            if network_error != QNetworkReply.NetworkError.NoError:
                if status_code in (403, 429):
                    message = "GitHub 请求受限，请稍后重试。"
                elif status_code == 404:
                    message = "GitHub 上暂时没有正式版本。"
                else:
                    message = "检查失败，请检查网络后重试。"
                status_label.setStyleSheet(f"color: {self.colors['danger']};")
                status_label.setText(message)
                return

            try:
                release = json.loads(response_bytes.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                status_label.setStyleSheet(f"color: {self.colors['danger']};")
                status_label.setText("GitHub 返回了无法识别的数据。")
                return
            if not isinstance(release, dict):
                status_label.setStyleSheet(f"color: {self.colors['danger']};")
                status_label.setText("GitHub 返回了无法识别的数据。")
                return

            latest_tag = str(release.get("tag_name", "") or "").strip()
            has_update = self.is_newer_release(self.APP_VERSION, latest_tag)
            release_url_text = str(release.get("html_url", "") or "")
            release_url = QUrl(release_url_text)
            if (
                has_update is None
                or not self.is_valid_release_url(release_url_text)
            ):
                status_label.setStyleSheet(f"color: {self.colors['danger']};")
                status_label.setText("GitHub Release 信息无效。")
                return

            if has_update:
                status_label.setStyleSheet(f"color: {self.colors['success']};")
                if self.open_release_page(release_url):
                    status_label.setText(f"发现新版本 {latest_tag}，已打开发布页。")
                else:
                    status_label.setStyleSheet(f"color: {self.colors['danger']};")
                    status_label.setText("发现新版本，但无法打开浏览器。")
                return

            status_label.setStyleSheet(f"color: {self.colors['success']};")
            status_label.setText(f"当前已是最新版本（v{self.APP_VERSION}）。")

        reply.finished.connect(finish_check)

    @staticmethod
    def open_release_page(url):
        return QDesktopServices.openUrl(url)

    def _browse_path(self, target):
        dialog = QFileDialog(target.window(), "选择文件")
        dialog.setFileMode(QFileDialog.FileMode.ExistingFile)
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptOpen)
        dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        if dialog.exec() and dialog.selectedFiles():
            target.setText(dialog.selectedFiles()[0])

    def _make_button(self, parent, text, command, kind="secondary", width=None):
        button = QPushButton(text, parent)
        button.setProperty("variant", kind)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        if width:
            button.setMinimumWidth(max(72, width * 8))
        if command is not None:
            button.clicked.connect(command)
        return button

    def open_suffix_tool(self):
        if self.suffix_tool_window is None:
            self.suffix_tool_window = FileSuffixModifierDialog(self.root)
        self.suffix_tool_window.show()
        self.suffix_tool_window.raise_()
        self.suffix_tool_window.activateWindow()

    def update_overview(self):
        """刷新当前布局中的任务数量徽标。"""
        self.overview_signal.emit()

    def _create_ui(self):
        central = QWidget(self.root)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(20, 18, 20, 18)
        outer.setSpacing(12)

        toolbar = QFrame()
        toolbar.setObjectName("Toolbar")
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(12, 10, 12, 10)
        toolbar_layout.setSpacing(8)

        engine_group_frame = QFrame()
        engine_layout = QHBoxLayout(engine_group_frame)
        engine_layout.setContentsMargins(0, 0, 0, 0)
        engine_layout.setSpacing(6)
        self.engine_group = QButtonGroup(self.root)
        for label, value in (("WinRAR", 1), ("7-Zip", 2)):
            option = QRadioButton(label)
            option.setCursor(Qt.CursorShape.PointingHandCursor)
            self.engine_group.addButton(option, value)
            engine_layout.addWidget(option)
        self.engine_group.button(1).setChecked(True)
        self.engine_group.idToggled.connect(self._on_engine_changed)
        toolbar_layout.addWidget(engine_group_frame)
        toolbar_layout.addSpacing(6)
        toolbar_layout.addWidget(self._make_button(toolbar, "清除选中", self.clear_selected, "secondary"))
        toolbar_layout.addWidget(self._make_button(toolbar, "清空列表", self.clear_list, "secondary"))
        self.btn_tools = self._make_button(toolbar, "工具", self.open_suffix_tool, "secondary")
        toolbar_layout.addWidget(self.btn_tools)
        toolbar_layout.addStretch(1)

        self.btn_start = self._make_button(toolbar, "开始处理", self.start_thread, "primary", 12)
        self.btn_stop = self._make_button(toolbar, "停止处理", self.stop_processing, "danger", 12)
        self.btn_stop.setEnabled(False)
        self.btn_settings = self._make_button(toolbar, "设置", self.open_settings, "secondary", 9)
        toolbar_layout.addWidget(self.btn_start)
        toolbar_layout.addWidget(self.btn_stop)
        toolbar_layout.addWidget(self.btn_settings)
        outer.addWidget(toolbar)
        self._apply_card_shadow(toolbar)

        queue_card = QFrame()
        queue_card.setObjectName("Panel")
        queue_layout = QVBoxLayout(queue_card)
        queue_layout.setContentsMargins(16, 12, 16, 14)
        queue_layout.setSpacing(8)
        queue_header = QHBoxLayout()
        queue_header.setSpacing(8)
        queue_title = QLabel("任务队列")
        queue_title.setObjectName("SectionTitle")
        queue_header.addWidget(queue_title)
        queue_header.addStretch(1)
        self.status_dot = QLabel("●")
        self.status_dot.setStyleSheet(f"color: {self.colors['success']}; font-size: 12px;")
        self.status_label = QLabel("等待操作...")
        self.status_label.setObjectName("Muted")
        queue_header.addWidget(self.status_dot)
        queue_header.addWidget(self.status_label)
        self.queue_badge = QLabel("0 个任务")
        self.queue_badge.setStyleSheet(
            f"padding: 4px 9px; border-radius: 6px; background: {self.colors['primary_soft']}; color: {self.colors['success']}; font-size: 12px;"
        )
        queue_header.addWidget(self.queue_badge)
        queue_layout.addLayout(queue_header)

        columns = ("待处理主卷", "重命名目标", "提取的密码 · 双击修改", "源信息", "解压状态", "进度")
        self.tree = TaskTable(0, len(columns), queue_card)
        self.tree.setHorizontalHeaderLabels(columns)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked)
        self.tree.setAlternatingRowColors(False)
        self.tree.setWordWrap(False)
        self.tree.setShowGrid(False)
        self.tree.setSortingEnabled(False)
        self.tree.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)
        self.tree.setAcceptDrops(True)
        self.tree.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.tree.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.tree.verticalHeader().setVisible(False)
        self.tree.verticalHeader().setDefaultSectionSize(42)
        self.tree.horizontalHeader().setMinimumSectionSize(78)
        for column, width in enumerate((300, 260, 230, 88, 112, 76)):
            self.tree.setColumnWidth(column, width)
        for column in range(3):
            self.tree.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeMode.Stretch
            )
        for column in range(3, len(columns)):
            self.tree.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeMode.Fixed
            )
        self.tree.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.tree.itemSelectionChanged.connect(self.on_task_selected)
        self.tree.itemChanged.connect(self._on_table_item_changed)
        self.tree.cellDoubleClicked.connect(self.on_double_click)
        self.tree.files_dropped.connect(self.handle_drop)
        queue_layout.addWidget(self.tree, 1)
        outer.addWidget(queue_card, 1)
        self._apply_card_shadow(queue_card)

        bottom = QHBoxLayout()
        bottom.setSpacing(12)
        log_card = QFrame()
        log_card.setObjectName("Panel")
        log_layout = QVBoxLayout(log_card)
        log_layout.setContentsMargins(14, 12, 14, 12)
        log_layout.setSpacing(8)
        log_header = QHBoxLayout()
        log_title = QLabel("执行日志")
        log_title.setObjectName("SectionTitle")
        log_header.addWidget(log_title)
        log_header.addStretch(1)
        self.log_area = QTextEdit()
        self.log_area.setReadOnly(True)
        self.log_area.setMinimumHeight(132)
        log_font = QFont(self.fonts["family"])
        log_font.setPixelSize(self.fonts["body_size"])
        self.log_area.setFont(log_font)
        log_layout.addLayout(log_header)
        log_layout.addWidget(self.log_area, 1)
        bottom.addWidget(log_card, 2)

        source_card = QFrame()
        source_card.setObjectName("Panel")
        source_layout = QVBoxLayout(source_card)
        source_layout.setContentsMargins(14, 12, 14, 12)
        source_layout.setSpacing(8)
        source_title = QLabel("源信息")
        source_title.setObjectName("SectionTitle")
        source_layout.addWidget(source_title)
        source_form = QFormLayout()
        source_form.setContentsMargins(0, 0, 0, 0)
        source_form.setHorizontalSpacing(10)
        source_form.setVerticalSpacing(5)
        source_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.source_info_vars = {}
        source_fields = (
            ("file_name", "文件名"),
            ("original_url", "原网页"),
            ("cloud_url", "网盘"),
            ("source", "来源"),
            ("saved", "保存"),
        )
        for key, label_text in source_fields:
            field = SourceLinkEdit("—")
            field.setReadOnly(True)
            field.setClearButtonEnabled(False)
            field.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            field.customContextMenuRequested.connect(
                lambda point, target=field: self.show_source_context_menu(target, point)
            )
            if key in ("original_url", "cloud_url"):
                field.setStyleSheet(f"color: {self.colors['primary']};")
                field.double_clicked.connect(lambda field_key=key: self.open_source_link(field_key))
            self.source_info_vars[key] = field
            label = QLabel(label_text)
            label.setObjectName("Muted")
            source_form.addRow(label, field)
        source_layout.addLayout(source_form, 1)
        bottom.addWidget(source_card, 3)
        outer.addLayout(bottom, 0)
        self.root.setCentralWidget(central)
        self._apply_card_shadow(log_card)
        self._apply_card_shadow(source_card)

    def _apply_card_shadow(self, widget):
        shadow = QGraphicsDropShadowEffect(widget)
        shadow.setBlurRadius(12)
        shadow.setOffset(0, 2)
        shadow.setColor(QColor(23, 32, 29, 18))
        widget.setGraphicsEffect(shadow)

    def _task_id_at_row(self, row):
        item = self.tree.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _find_task_row(self, task_id):
        for row in range(self.tree.rowCount()):
            if self._task_id_at_row(row) == task_id:
                return row
        return -1

    def _add_task_row(self, task_id, values):
        row = self.tree.rowCount()
        self.tree.insertRow(row)
        for column, value in enumerate(values):
            item = QTableWidgetItem(str(value))
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            if column in (3, 4, 5):
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if column == 0:
                item.setData(Qt.ItemDataRole.UserRole, task_id)
                item.setToolTip(str(value))
            elif column == 1:
                item.setToolTip(str(value))
            elif column == 2:
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            self.tree.setItem(row, column, item)
        self._style_source_cell(row)

    def _style_source_cell(self, row):
        item = self.tree.item(row, 3)
        if not item:
            return
        found = "已找到" in item.text()
        item.setForeground(QColor(self.colors["success"] if found else self.colors["danger"]))
        item.setBackground(QColor(self.colors["success_soft"] if found else self.colors["danger_soft"]))

    def _on_table_item_changed(self, item):
        if item.column() != 2:
            return
        task_id = self._task_id_at_row(item.row())
        for task in self.files_data:
            if task.get("tree_id") == task_id:
                task["password"] = item.text()
                break

    def _on_engine_changed(self, engine_id, checked):
        if checked:
            self.engine_type = engine_id

    def _refresh_overview(self):
        if hasattr(self, "queue_badge"):
            self.queue_badge.setText(f"{len(self.files_data)} 个任务")

    def _set_task_status(self, task_id, status_text, tag):
        row = self._find_task_row(task_id)
        if row < 0:
            return
        status_item = self.tree.item(row, 4)
        if status_item:
            status_item.setText(status_text)
        row_colors = {
            "success": (self.colors["table_success_bg"], self.colors["success"]),
            "error": (self.colors["table_error_bg"], self.colors["danger"]),
            "processing": (self.colors["table_processing_bg"], self.colors["info"]),
        }
        background, foreground = row_colors.get(tag, ("#FFFFFF", self.colors["text"]))
        for column in range(self.tree.columnCount()):
            item = self.tree.item(row, column)
            if item:
                item.setBackground(QColor(background))
                item.setForeground(QColor(foreground))
        self._style_source_cell(row)

    def _set_task_progress(self, task_id, progress_text):
        row = self._find_task_row(task_id)
        if row >= 0 and self.tree.item(row, 5):
            self.tree.item(row, 5).setText(progress_text)

    def _set_runtime_status(self, status_text, color):
        self.status_label.setText(status_text)
        dot_color = color or self.colors["success"]
        self.status_dot.setStyleSheet(f"color: {dot_color}; font-size: 12px;")

    def _set_task_password(self, task_id, password):
        row = self._find_task_row(task_id)
        if row >= 0 and self.tree.item(row, 2):
            self.tree.item(row, 2).setText(password)

    def _set_undo_available(self, available):
        self.undo_available = available

    def _set_processing_controls(self, start_enabled, stop_enabled, start_text, status_text, color):
        self.btn_start.setEnabled(start_enabled)
        self.btn_start.setText(start_text)
        self.btn_stop.setEnabled(stop_enabled)
        self._set_runtime_status(status_text, color)

    def _show_completion(self, text):
        QMessageBox.information(self.root, "处理完成", text)

    def refresh_source_info_columns(self):
        for task in self.files_data:
            file_path = task.get("original_path", "")
            lookup_names = task.get("lookup_names")
            record = self.find_temporary_record(file_path, lookup_names) if file_path else None
            task["source_found"] = record is not None
            task["source_info"] = (
                self.get_source_info_for_path(file_path, lookup_names) if record else None
            )
            row = self._find_task_row(task.get("tree_id"))
            if row >= 0:
                item = self.tree.item(row, 3)
                if item:
                    item.setText("已找到" if record else "未找到")
                self._style_source_cell(row)
        self.on_task_selected()

    def _clear_source_info(self):
        for field in getattr(self, "source_info_vars", {}).values():
            field.setText("—")

    def on_task_selected(self):
        selected = self.tree.selectionModel().selectedRows()
        if not selected:
            self._clear_source_info()
            return
        task_id = self._task_id_at_row(selected[0].row())
        task = next((item for item in self.files_data if item.get("tree_id") == task_id), None)
        if not task:
            self._clear_source_info()
            return
        info = task.get("source_info") or {}
        for key in ("file_name", "original_url", "cloud_url", "source", "saved"):
            field = self.source_info_vars[key]
            value = str(info.get(key, "") or "").strip()
            field.setText(value or "—")
            field.setToolTip(value)

    def copy_source_text(self, widget):
        selected = widget.selectedText()
        value = selected or widget.text()
        if value and value != "—":
            QApplication.clipboard().setText(value)

    def show_source_context_menu(self, widget, point):
        menu = QMenu(widget)
        menu.addAction("复制", lambda: self.copy_source_text(widget))
        menu.addAction("全选", widget.selectAll)
        menu.exec(widget.mapToGlobal(point))

    def open_source_link(self, field):
        if field not in ("original_url", "cloud_url"):
            return
        value = self.source_info_vars.get(field).text().strip() if field in self.source_info_vars else ""
        if not value or value == "—":
            return
        if not re.match(r"^https?://", value, re.IGNORECASE):
            self.log("源信息中的链接格式无效", "ERROR")
            return
        try:
            webbrowser.open_new_tab(value)
            self.log("已打开源信息链接", "ACTION")
        except Exception as e:
            self.log(f"打开源信息链接失败: {e}", "ERROR")

    def clear_selected(self):
        if self.is_running:
            self.log("处理进行中，暂不能清除选中任务", "INFO")
            return
        rows = sorted({index.row() for index in self.tree.selectionModel().selectedRows()}, reverse=True)
        if not rows:
            self.log("未选择任务，无法清除", "INFO")
            return
        selected_ids = {self._task_id_at_row(row) for row in rows}
        removed_before_resume = sum(
            1 for index, task in enumerate(self.files_data)
            if index < self.resume_index and task.get("tree_id") in selected_ids
        )
        self.files_data = [task for task in self.files_data if task.get("tree_id") not in selected_ids]
        for row in rows:
            self.tree.removeRow(row)
        self.resume_index = max(0, self.resume_index - removed_before_resume)
        self.current_task_index = min(self.current_task_index, max(0, len(self.files_data) - 1))
        self.rename_history = []
        self._set_undo_available(False)
        self._clear_source_info()
        self.log(f"已从列表清除 {len(selected_ids)} 个任务", "ACTION")
        self.update_overview()

    def stop_processing(self):
        """请求停止当前任务，并保留当前任务索引供下次继续。"""
        if not self.is_running:
            return

        self.stop_requested.set()
        self.log("收到停止处理请求，正在停止当前任务…", "ACTION")
        self.update_runtime_status("正在停止当前任务…", self.colors["warning"])

        process = self.current_process
        if process is not None:
            try:
                if process.poll() is None:
                    process.terminate()
            except Exception as e:
                self.log(f"停止解压进程失败: {e}", "ERROR")

    def open_debug_log_ui(self):
        if os.path.exists(self.execution_log_file):
            if os.name == 'nt': os.startfile(self.execution_log_file)
            else: subprocess.call(['open', self.execution_log_file])
        else:
            QMessageBox.information(self.root, "提示", "目前还没有生成日志文件。")

    def update_tree_status(self, item_id, status_text, tag=""):
        self.task_status_signal.emit(int(item_id), status_text, tag)
        
    def update_tree_progress(self, item_id, progress_text):
        self.task_progress_signal.emit(int(item_id), progress_text)

    def update_runtime_status(self, status_text, color=None):
        self.runtime_status_signal.emit(status_text, str(color or ""))

    def log(self, message, level="INFO"):
        now = datetime.datetime.now()
        display_time = now.strftime("%H:%M:%S")
        file_time = now.strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{file_time}] [{level}] {message}"

        # 后台线程和主线程都可能写日志，文件写入必须串行化。
        try:
            with self.execution_log_lock:
                with open(self.execution_log_file, "a", encoding="utf-8") as log_file:
                    log_file.write(line + "\n")
        except Exception:
            pass

        self.log_signal.emit(display_time, level, str(message))

    def _append_log(self, display_time, level, message):
        cursor = self.log_area.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        time_format = QTextCharFormat()
        time_format.setForeground(QColor(self.colors["log_time"]))
        cursor.insertText(f"[{display_time}] ", time_format)
        level_colors = {
            "INFO": self.colors["log_info"],
            "SUCCESS": self.colors["log_success"],
            "ERROR": self.colors["log_error"],
            "WARNING": self.colors["warning"],
            "ACTION": self.colors["log_action"],
        }
        message_format = QTextCharFormat()
        message_format.setForeground(QColor(level_colors.get(level, self.colors["log_text"])))
        message_format.setFontWeight(QFont.Weight.Normal)
        cursor.insertText(f"[{level}] {message}\n", message_format)
        self.log_area.setTextCursor(cursor)
        self.log_area.ensureCursorVisible()

    def handle_drop(self, paths):
        for path in paths:
            if path:
                self.process_input_path(os.path.normpath(path))

    def infer_missing_archive_suffix(self, filename):
        """为没有扩展名的主卷推断需要补上的压缩格式后缀。

        没有任何格式关键词时按用户约定使用 .zip；如果文件名中出现
        zip、rar、7z 或 zst，则优先使用文件名里最先出现的关键词。
        """
        filename = os.path.basename(str(filename or "")).strip("{} ")
        _, extension = os.path.splitext(filename)
        if extension and extension != ".":
            return ""

        format_tokens = (
            ("zip", ".zip"),
            ("rar", ".rar"),
            ("7z", ".7z"),
            ("zst", ".zst"),
        )
        lowered = filename.casefold()
        matches = [
            (lowered.find(token), -len(token), suffix)
            for token, suffix in format_tokens
            if lowered.find(token) >= 0
        ]
        if matches:
            matches.sort(key=lambda item: (item[0], item[1]))
            return matches[0][2]
        return ".zip"

    def ensure_archive_suffix(self, filename):
        """返回带有可用压缩格式后缀的文件名；已有后缀时保持原样。"""
        filename = os.path.basename(str(filename or "")).strip("{} ")
        suffix = self.infer_missing_archive_suffix(filename)
        if not suffix:
            return filename
        return filename.rstrip(". ") + suffix

    def looks_like_supported_archive(self, filename):
        """判断文件名是否是脚本支持的压缩包，兼容无后缀格式关键词。"""
        filename = os.path.basename(str(filename or "")).strip("{} ")
        normalized_name, disguise_suffix = self.normalize_disguised_archive_name(filename)
        if disguise_suffix:
            filename = normalized_name
        lowered = filename.casefold()
        supported_extensions = (
            ".rar", ".zip", ".7z", ".tar", ".gz", ".tgz",
            ".bz2", ".xz", ".zst", ".001", ".mp4", ".17z",
        )
        if lowered.endswith(supported_extensions):
            return True
        _, extension = os.path.splitext(filename)
        return (not extension or extension == ".") and any(
            token in lowered for token in ("zip", "rar", "7z", "zst")
        )

    def is_zst_file(self, file_path):
        return os.path.basename(str(file_path or "")).casefold().endswith(".zst")

    def extract_password_from_str(self, text):
        pattern = r'.*?(?:密码|pw|解压密码|解压码)[:：]?(.*)'
        match = re.match(pattern, text, re.IGNORECASE)
        if match: return match.group(1).strip('{} ')
        return ""

    def process_input_path(self, path):
        path = path.strip('{} ')
        if os.path.isdir(path):
            input_root = os.path.abspath(path)
            self.log(f"扫描目录: {input_root}")
            folder_name = os.path.basename(input_root)
            inherited_pw = self.extract_password_from_str(folder_name)

            # 文件夹内只有一个较大的无后缀文件时，也按待处理压缩包加入任务。
            # 统计整个扫描范围，保持与下面的递归目录扫描规则一致。
            folder_files = []
            for scan_root, _, scan_names in os.walk(input_root):
                for scan_name in scan_names:
                    folder_files.append(os.path.join(scan_root, scan_name))

            single_extensionless_file = None
            if len(folder_files) == 1:
                candidate_path = folder_files[0]
                candidate_name = os.path.basename(candidate_path)
                _, candidate_extension = os.path.splitext(candidate_name)
                if (
                    (not candidate_extension or candidate_extension == ".")
                    and self.is_file_over_threshold(
                        candidate_path,
                        self.SINGLE_EXTENSIONLESS_FOLDER_THRESHOLD,
                    )
                ):
                    single_extensionless_file = os.path.abspath(candidate_path)
                    self.log(
                        f"检测到文件夹内唯一的无后缀大文件，将加入任务: {candidate_name}",
                        "INFO",
                    )

            for root, dirs, files in os.walk(input_root):
                for file in files:
                    file_path = os.path.join(root, file)
                    if (
                        self.looks_like_supported_archive(file)
                        or '删' in file
                        or self.should_force_zip_for_large_file(file_path, file)
                        or (
                            single_extensionless_file
                            and os.path.abspath(file_path) == single_extensionless_file
                        )
                    ):
                        self.analyze_and_add_file(
                            file_path,
                            inherited_pw,
                            "目录扫描",
                            source_root=input_root,
                        )
        else:
            self.analyze_and_add_file(path, "", "手动添加", source_root="")

    def normalize_disguised_archive_name(self, filename):
        """去掉压缩分卷末尾的伪装后缀，并返回(规范名, 伪装后缀)。

        例如“文件.7z.001.TXT”和“文件.7z.jpg”会分别规范为
        “文件.7z.001”和“文件.7z”。
        只有文件名中已经出现已知压缩格式，且其后还有非标准尾缀时才会触发，
        普通的 TXT、JPG 等文件不会因此被当成压缩包。
        """
        filename = os.path.basename(str(filename or "")).strip("{} ")
        if not filename:
            return filename, ""

        pattern = (
            r"^(?P<archive>"
            r".+?\.(?:zip|rar|7z|tar|gz|tgz|bz2|xz|zst)\.\d{3}"
            r"|.+?\.part\d+\.(?:zip|rar|7z|zst)"
            r"|.+?\.(?:zip|rar|7z|tar|gz|tgz|bz2|xz|zst)"
            r")"
            r"(?P<disguise>\.[^\\/:*?\"<>|]+)$"
        )
        match = re.match(pattern, filename, re.IGNORECASE)
        if not match:
            return filename, ""

        disguise_suffix = match.group("disguise")
        disguise_lower = disguise_suffix.casefold()
        # 已经是标准压缩扩展名、标准分卷后缀时不剥离，避免误处理
        # .tar.gz、.7z.001、.zip.z01 等合法的多级扩展名。
        if (
            disguise_lower in self.COMMON_ARCHIVE_EXTENSIONS
            or re.fullmatch(r"\.(?:\d{3}|z\d{2}|r\d{2})", disguise_lower)
            or re.fullmatch(r"\.part\d+", disguise_lower)
        ):
            return filename, ""
        return match.group("archive"), disguise_suffix

    def is_secondary_part(self, filename):
        filename, _ = self.normalize_disguised_archive_name(filename)
        filename_lower = filename.lower()
        if re.search(r'\.part(\d+)\.(?:rar|zip|7z|zst)', filename_lower) and int(re.search(r'\.part(\d+)\.(?:rar|zip|7z|zst)', filename_lower).group(1)) > 1: return True
        if re.search(r'\.(?:7z|zip|rar|tar|zst)\.(\d{3})', filename_lower) and int(re.search(r'\.(?:7z|zip|rar|tar|zst)\.(\d{3})', filename_lower).group(1)) > 1: return True
        if re.search(r'\.z\d+$', filename_lower): return True
        return False

    def get_split_family_name(self, filename):
        """返回分卷文件的归属组名称，用于同步修复伪装分卷后缀。"""
        filename, _ = self.normalize_disguised_archive_name(filename)
        filename = os.path.basename(filename)

        numeric_match = re.match(
            r"^(.*\.(?:zip|rar|7z|tar|gz|tgz|bz2|xz|zst))\.\d{3}$",
            filename,
            re.IGNORECASE,
        )
        if numeric_match:
            return numeric_match.group(1).casefold()

        part_match = re.match(
            r"^(.*)\.part\d+\.(zip|rar|7z|zst)$",
            filename,
            re.IGNORECASE,
        )
        if part_match:
            return f"{part_match.group(1)}.{part_match.group(2)}".casefold()
        return ""

    def is_file_over_threshold(self, filepath, threshold):
        try:
            return os.path.getsize(filepath) > threshold
        except (OSError, TypeError):
            return False

    def is_large_file(self, filepath):
        return self.is_file_over_threshold(filepath, self.LARGE_FILE_THRESHOLD)

    def should_force_zip_for_large_file(self, filepath, filename):
        """判断大文件是否需要把伪装或无意义后缀改为 .zip。"""
        if not self.is_large_file(filepath):
            return False

        extension = os.path.splitext(filename)[1].casefold()
        if not extension or extension == ".":
            return False

        # 保留原有 .mp4 -> .zip、.17z -> .7z 兼容逻辑，避免覆盖既有格式推断。
        if extension in (".mp4", ".17z"):
            return False

        return (
            extension in self.LARGE_FILE_ZIP_EXTENSIONS
            or extension not in self.COMMON_ARCHIVE_EXTENSIONS
        )

    def replace_file_extension(self, filename, new_extension):
        stem, extension = os.path.splitext(filename)
        if extension and extension != ".":
            return stem + new_extension
        return filename.rstrip(". ") + new_extension

    def analyze_and_add_file(self, filepath, force_pw="", source="", source_root=""):
        filename = os.path.basename(filepath).strip('{} ')
        analysis_filename, disguised_suffix = self.normalize_disguised_archive_name(filename)
        if self.is_secondary_part(analysis_filename): return
        
        directory = os.path.dirname(filepath)
        exts = ['.rar', '.zip', '.7z', '.tar', '.gz', '.tgz', '.bz2', '.xz', '.zst', '.mp4', '.17z', '.001']
        real_ext, split_index = "", -1
        
        for ext in exts:
            idx = analysis_filename.lower().rfind(ext)
            if idx > split_index:
                split_index, real_ext = idx, analysis_filename[idx:idx+len(ext)]
        
        password = force_pw
        clean_name = analysis_filename
        
        if split_index != -1:
            extra_part = analysis_filename[split_index + len(real_ext):]
            name_body = analysis_filename[:split_index]
            current_ext = ".7z" if real_ext.lower() == ".17z" else (".zip" if real_ext.lower() == ".mp4" else real_ext)
            clean_name = name_body + current_ext
            if not password and extra_part:
                password = self.extract_password_from_str(extra_part)
                if not password: 
                    password = extra_part.strip('{} ')

        # 伪装文件名可能是“文件.7z.jpg密码123”。只读取明确的“密码”字段，
        # 不把普通伪装后缀（例如“.jpg”）误当成解压密码。
        if not password and disguised_suffix:
            password = self.extract_password_from_str(disguised_suffix)

        # 拖入的主卷可能完全没有扩展名。重命名时补上可识别的格式后缀：
        # 文件名含 zip/rar/7z/zst 时使用对应格式，否则默认使用 .zip。
        if split_index == -1:
            clean_name = self.ensure_archive_suffix(clean_name)

        # 大于 100MB 的图片、文本、动图或未知后缀可能是被伪装的压缩包。
        # 统一改成“原文件名主体.zip”，实际处理完成后仍按原有流程自动还原。
        if self.should_force_zip_for_large_file(filepath, analysis_filename):
            clean_name = self.replace_file_extension(analysis_filename, ".zip")

        if "删" in clean_name: clean_name = clean_name.replace("删", "")

        is_split_file = ".001" in clean_name or ".part" in clean_name.lower()
        if not is_split_file and clean_name.lower().endswith('.zip'):
            base_name = os.path.splitext(analysis_filename)[0]
            try:
                if any(f.lower().endswith('.z01') and base_name.lower() in f.lower() for f in os.listdir(directory)):
                    is_split_file = True
            except: pass

        if source_root:
            source_root_name = os.path.basename(os.path.abspath(source_root))
            lookup_names = [source_root_name, filename]
        else:
            file_path = os.path.abspath(filepath)
            file_dir = os.path.dirname(file_path)
            lookup_names = [
                filename,
                os.path.basename(file_dir),
                os.path.basename(os.path.dirname(file_dir)),
            ]
        lookup_names = self.get_lookup_attempt_names(filepath, lookup_names)

        source_record = self.find_temporary_record(filepath, lookup_names)
        source_found = source_record is not None
        source_info = (
            self.get_source_info_for_path(filepath, lookup_names)
            if source_found else None
        )
        source_cell = "已找到" if source_found else "未找到"
        item_id = self.next_task_id
        self.next_task_id += 1
        self._add_task_row(
            item_id,
            (filename, clean_name, password, source_cell, "等待中", "0%"),
        )
        self.files_data.append({
            'original_path': filepath, 'directory': directory, 
            'clean_name': clean_name, 'password': password, 
            'tree_id': item_id, 'is_split': is_split_file,
            'source_found': source_found,
            'source_info': source_info,
            'source_type': source,
            'disguised_suffix': disguised_suffix,
            'lookup_names': lookup_names,
            # 拖入文件夹时，来源 HTML 统一写入最外层拖入目录；
            # 直接拖入文件时为空，运行阶段改用该文件的解压输出目录。
            'input_root': os.path.abspath(source_root) if source_root else "",
        })
        self.update_overview()

    def on_double_click(self, row, column):
        if column == 2:
            item = self.tree.item(row, column)
            if item and item.flags() & Qt.ItemFlag.ItemIsEditable:
                self.tree.editItem(item)

    def undo_rename(self):
        if not self.rename_history: return 0
        success = 0
        for curr, old in reversed(self.rename_history):
            try:
                if os.path.exists(curr):
                    os.rename(curr, old)
                    success += 1
            except: pass
        self.log(f"执行手动还原：成功恢复 {success} 个文件名", "ACTION")
        self.rename_history = []
        self.undo_signal.emit(False)
        return success

    def write_debug_log(self, command, return_code, output):
        try:
            with open(self.debug_log_file, "a", encoding="utf-8") as f:
                now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                f.write(f"\n[{now}] ====== 解压执行 ======\n")
                f.write(f"命令行: {' '.join(command)}\n")
                f.write(f"返回码: {return_code}\n")
                f.write(f"原生输出:\n{output}\n")
                f.write("-" * 60 + "\n")
        except: pass

    def clear_output_dir_for_retry(self, output_dir):
        """清理上一次错误密码尝试留下的输出，避免残留文件造成误判。"""
        if self.stop_requested.is_set():
            return
        if os.path.exists(output_dir):
            try:
                shutil.rmtree(output_dir, ignore_errors=True)
                time.sleep(0.1)
            except Exception as e:
                self.log(f"清理重试目录失败: {e}", "ERROR")

    def remember_successful_password(self, password):
        """把成功密码放入本次运行的高频记忆队列。"""
        if password in self.successful_passwords:
            self.successful_passwords.remove(password)
        self.successful_passwords.insert(0, password)
        self.update_overview()

    def perform_extraction(self, exe_path, engine_type, file_path, output_dir, password, is_fallback=False, tree_id=None):
        if self.stop_requested.is_set():
            self.last_extraction_stopped = True
            return False, "用户已停止处理"

        self.last_extraction_stopped = False
        if not is_fallback:
            self.log(f"正在解压: {os.path.basename(file_path)}", "INFO")

        # WinRAR 对 .zst 的兼容性取决于安装版本；7-Zip 对 Zstandard 支持更稳定。
        # 用户仍可保持原来的引擎选择，遇到 .zst 时自动使用可用的 7-Zip 执行。
        effective_exe_path = exe_path
        effective_engine_type = engine_type
        if self.is_zst_file(file_path) and engine_type == 1:
            sevenzip_path = self.config.get("sevenzip_path") or next(
                (p for p in self.sevenzip_candidates if os.path.exists(p)),
                None,
            )
            if sevenzip_path and os.path.exists(sevenzip_path):
                effective_exe_path = sevenzip_path
                effective_engine_type = 2
                if not is_fallback:
                    self.log("检测到 .zst，自动使用 7-Zip 进行解压", "ACTION")

        if effective_engine_type == 1:
            cmd = [effective_exe_path, "x", "-y", f"-p{password}" if password else "-p-", file_path, output_dir + "\\"]
        elif effective_engine_type == 2:
            cmd = [effective_exe_path, "x", "-y", "-aoa", "-bsp1", "-bse1", f"-o{output_dir}"]
            cmd.append(f"-p{password}" if password else "-p")
            cmd.append(file_path)
        else:
            return False, "未知解压引擎"
            
        startupinfo = None
        if os.name == 'nt':
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        
        full_output_log = [] 
        
        try:
            process = subprocess.Popen(cmd, shell=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, startupinfo=startupinfo)
            self.current_process = process
            
            while True:
                if self.stop_requested.is_set():
                    self.last_extraction_stopped = True
                    try:
                        if process.poll() is None:
                            process.terminate()
                    except Exception:
                        pass
                    break

                chunk = process.stdout.read1(2048) 
                if not chunk and process.poll() is not None:
                    break
                
                if chunk:
                    try:
                        chunk_str = chunk.decode('gbk', errors='ignore')
                        full_output_log.append(chunk_str)
                        if tree_id and not is_fallback:
                            matches = re.findall(r'(\d{1,3})%', chunk_str)
                            if matches: self.update_tree_progress(tree_id, matches[-1] + "%")
                    except: pass

            try:
                process.communicate(timeout=2 if self.last_extraction_stopped else None)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except Exception:
                    pass
                process.communicate()
            raw_output = "".join(full_output_log)
            self.write_debug_log(cmd, process.returncode, raw_output)

            if self.last_extraction_stopped:
                return False, "用户已停止处理"
            
            if process.returncode in (0, 1):
                extracted_ok = False
                if os.path.exists(output_dir):
                    for root_dir, dirs, files in os.walk(output_dir):
                        if files: 
                            extracted_ok = True
                            break
                
                if not extracted_ok:
                    if not is_fallback: self.log(f"解压失败(目录无文件): {os.path.basename(file_path)}", "ERROR")
                    return False, raw_output
                else:
                    if not is_fallback: self.log(f"解压成功: {os.path.basename(file_path)}", "SUCCESS")
                    return True, raw_output
            else:
                if not is_fallback: self.log(f"解压失败(Code:{process.returncode}): {os.path.basename(file_path)}", "ERROR")
                return False, raw_output
        except Exception as e:
            if self.stop_requested.is_set():
                self.last_extraction_stopped = True
                return False, "用户已停止处理"
            if not is_fallback: self.log(f"调用引擎异常: {str(e)}", "ERROR")
            self.write_debug_log(cmd, "EXCEPTION", str(e))
            return False, str(e)
        finally:
            if 'process' in locals() and self.current_process is process:
                self.current_process = None

    def extract_with_fallback(
        self,
        exe_path,
        engine_type,
        file_path,
        output_dir,
        known_password,
        tree_id=None,
        lookup_names=None,
    ):
        if self.stop_requested.is_set():
            self.last_extraction_stopped = True
            return False, "用户已停止处理"

        # 1. 第一顺位：尝试提取到的默认密码
        success, raw_output = self.perform_extraction(exe_path, engine_type, file_path, output_dir, known_password, tree_id=tree_id)
        if self.last_extraction_stopped or self.stop_requested.is_set():
            return False, "用户已停止处理"
        if success:
            return True, known_password
        
        # 智能诊断伪装格式
        if engine_type == 2 and ("Is not archive" in raw_output or "Cannot open the file as" in raw_output):
            self.log(f"诊断拦截：探测到文件【{os.path.basename(file_path)}】系伪装后缀，7-Zip 引擎无法识别", "ERROR")
            winrar_path = self.config.get("winrar_path") or next((p for p in self.winrar_candidates if os.path.exists(p)), None)
            if winrar_path and os.path.exists(winrar_path):
                self.log("启动智能修复机制，切换至 WinRAR 引擎进行补充扫描", "ACTION")
                if tree_id: self.update_tree_progress(tree_id, "切换WinRAR...")
                return self.extract_with_fallback(
                    winrar_path,
                    1,
                    file_path,
                    output_dir,
                    known_password,
                    tree_id=tree_id,
                    lookup_names=lookup_names,
                )
            else:
                self.log("修复失败：需要 WinRAR 的补充扫描机制，但未找到 WinRAR", "ERROR")
                return False, known_password

        # 【核心流程】：临时密码本 -> 历史成功密码 -> 普通密码本
        temporary_passwords = self.find_temporary_passwords(file_path, lookup_names)
        if temporary_passwords or self.password_book or self.successful_passwords:
            self.log(f"常规解压失败(可能需密码)，开始按优先级尝试候选密码...", "INFO")
            if tree_id: self.update_tree_progress(tree_id, "破解中...")
            
            tried_passwords = {known_password} # 防重合集，避免多次尝试相同的密码

            # 2. 第一优先级：根据待解压文件名/文件夹名匹配临时密码本。
            if temporary_passwords:
                self.log(f"临时密码本优先尝试 {len(temporary_passwords)} 个候选密码...", "INFO")
                for pwd in temporary_passwords:
                    if self.stop_requested.is_set():
                        self.last_extraction_stopped = True
                        return False, "用户已停止处理"
                    if pwd in tried_passwords: continue
                    tried_passwords.add(pwd)

                    self.clear_output_dir_for_retry(output_dir)
                    success_pw, raw_output_pw = self.perform_extraction(
                        exe_path, engine_type, file_path, output_dir, pwd,
                        is_fallback=True, tree_id=tree_id
                    )
                    if self.last_extraction_stopped or self.stop_requested.is_set():
                        return False, "用户已停止处理"
                    if success_pw:
                        self.log(f"临时密码本匹配成功！密码为: {pwd}", "SUCCESS")
                        self.remember_successful_password(pwd)
                        return True, pwd

            # 3. 第二优先级：优先遍历“曾经成功过”的高频记忆密码。
            if self.successful_passwords:
                self.log(f"优先尝试 {len(self.successful_passwords)} 个历史成功密码...", "INFO")
                for pwd in self.successful_passwords:
                    if self.stop_requested.is_set():
                        self.last_extraction_stopped = True
                        return False, "用户已停止处理"
                    if pwd in tried_passwords: continue
                    tried_passwords.add(pwd)
                    
                    self.clear_output_dir_for_retry(output_dir)

                    success_pw, raw_output_pw = self.perform_extraction(exe_path, engine_type, file_path, output_dir, pwd, is_fallback=True, tree_id=tree_id)
                    if self.last_extraction_stopped or self.stop_requested.is_set():
                        return False, "用户已停止处理"
                    if success_pw:
                        self.log(f"记忆提速生效！历史高频密码破解成功: {pwd}", "SUCCESS")
                        # 将它挪到队伍最前面，越近成功的优先级越高
                        self.remember_successful_password(pwd)
                        return True, pwd

            # 4. 第三优先级：临时密码本和历史密码都失败后，遍历普通密码本。
            if self.password_book:
                for pwd in self.password_book:
                    if self.stop_requested.is_set():
                        self.last_extraction_stopped = True
                        return False, "用户已停止处理"
                    if pwd in tried_passwords: continue
                    tried_passwords.add(pwd)
                    
                    self.clear_output_dir_for_retry(output_dir)

                    success_pw, raw_output_pw = self.perform_extraction(exe_path, engine_type, file_path, output_dir, pwd, is_fallback=True, tree_id=tree_id)
                    if self.last_extraction_stopped or self.stop_requested.is_set():
                        return False, "用户已停止处理"
                    if success_pw:
                        self.log(f"密码本破解成功！匹配密码为: {pwd}", "SUCCESS")
                        # 重点：初次破解成功，立即加入高频记忆队列的最前面！
                        self.remember_successful_password(pwd)
                        return True, pwd
                    
            self.log(f"穷举结束，所有密码全部尝试失败。", "ERROR")
            if tree_id: self.update_tree_progress(tree_id, "破解失败")
            
        return False, known_password

    def check_and_process_nested(
        self,
        current_folder,
        exe_path,
        engine_type,
        password,
        tree_id=None,
        ignored_names=None,
    ):
        layer = 0
        has_processed = False
        ignored_names = {
            os.path.basename(str(name))
            for name in (ignored_names or set())
            if str(name).strip()
        }
        initial_folder = os.path.normcase(os.path.abspath(current_folder))
        self.log(f"进入穿透检测: {os.path.basename(current_folder)}", "INFO")
        
        while True:
            if self.stop_requested.is_set():
                self.last_extraction_stopped = True
                break
            current_folder_is_initial = (
                os.path.normcase(os.path.abspath(current_folder)) == initial_folder
            )
            try:
                raw_files = os.listdir(current_folder)
            except: break
            
            for f in raw_files:
                if self.stop_requested.is_set():
                    self.last_extraction_stopped = True
                    break
                if current_folder_is_initial and f in ignored_names:
                    continue
                if "删" in f:
                    old_p, new_p = os.path.join(current_folder, f), os.path.join(current_folder, f.replace("删", ""))
                    if not os.path.exists(new_p):
                        try:
                            os.rename(old_p, new_p)
                            self.rename_history.append((new_p, old_p))
                            self.log(f"内层清理: {f} -> {os.path.basename(new_p)}", "ACTION")
                        except: pass

            all_files = []
            for root, dirs, files in os.walk(current_folder):
                if self.stop_requested.is_set():
                    self.last_extraction_stopped = True
                    break
                root_is_initial = os.path.normcase(os.path.abspath(root)) == initial_folder
                for f in files:
                    if root_is_initial and f in ignored_names:
                        continue
                    all_files.append(os.path.join(root, f))
            
            if not all_files: break
            
            target_file, should_extract = None, False
            if len(all_files) == 1:
                target_file = all_files[0]
                _, ext = os.path.splitext(target_file)
                if not ext:
                    new_n = target_file + self.infer_missing_archive_suffix(os.path.basename(target_file))
                    os.rename(target_file, new_n)
                    target_file = new_n
                    should_extract = True
                elif ext.lower() in ['.rar', '.zip', '.7z', '.tar', '.gz', '.tgz', '.bz2', '.xz', '.zst']:
                    should_extract = True
            else:
                all_f_names = [os.path.basename(x).lower() for x in all_files]
                for f in all_files:
                    f_low = f.lower()
                    if re.search(r'\.part0*1\.rar$', f_low) or re.search(r'\.(?:7z|zip|rar)\.001$', f_low) or f_low.endswith('.001'):
                        target_file = f; should_extract = True; break
                    elif f_low.endswith('.zip'):
                        base = os.path.splitext(os.path.basename(f))[0].lower()
                        if any(x.endswith('.z01') and base in x for x in all_f_names):
                            target_file = f; should_extract = True; break
                    elif f_low.endswith('.zst'):
                        target_file = f; should_extract = True; break
            
            if should_extract and target_file:
                if self.stop_requested.is_set():
                    self.last_extraction_stopped = True
                    break
                layer += 1
                self.log(f"发现嵌套分卷 (第{layer}层): {os.path.basename(target_file)}", "INFO")
                if tree_id: self.update_tree_progress(tree_id, f"嵌套{layer}层...")
                
                new_out = re.sub(
                    r'\.(?:7z|zip|rar|tar|gz|tgz|bz2|xz|zst)$',
                    '',
                    os.path.splitext(target_file)[0],
                    flags=re.IGNORECASE,
                )
                new_out = re.sub(r'\.part\d+$', '', new_out, flags=re.IGNORECASE)
                
                success, correct_pwd = self.extract_with_fallback(exe_path, engine_type, target_file, new_out, password, tree_id=tree_id)
                if success:
                    has_processed = True
                    current_folder = new_out
                    password = correct_pwd 
                else: 
                    break
            else: break
        return has_processed, layer

    def start_thread(self):
        if self.is_running or not self.files_data: return
        if self.resume_index >= len(self.files_data):
            self.resume_index = 0

        self.stop_requested.clear()
        self.last_extraction_stopped = False
        self.process_engine_type = self.engine_type
        self.process_auto_undo = self.auto_undo_enabled
        self.is_running = True
        self._set_processing_controls(False, True, "处理中…", "正在准备任务…", self.colors["info"])
        self.process_thread = threading.Thread(target=self.run_process, daemon=True)
        self.process_thread.start()

    def run_process(self):
        engine_type = getattr(self, "process_engine_type", self.engine_type)
        exe_path = None
        
        if engine_type == 1:
            exe_path = self.config.get("winrar_path") or next((p for p in self.winrar_candidates if os.path.exists(p)), None)
        elif engine_type == 2:
            exe_path = self.config.get("sevenzip_path") or next((p for p in self.sevenzip_candidates if os.path.exists(p)), None)
        
        if not exe_path or not os.path.exists(exe_path):
            self.log("错误：未找到所选解压软件的有效路径，请到设置中手动指定", "ERROR")
            self.reset_ui()
            return

        self.rename_history = []
        success_total, layers_total = 0, 0
        stopped = False
        
        start_index = max(0, min(self.resume_index, len(self.files_data)))
        for i in range(start_index, len(self.files_data)):
            if self.stop_requested.is_set():
                stopped = True
                self.resume_index = i
                break

            f = self.files_data[i]
            self.current_task_index = i
            tree_id = f['tree_id']
            self.update_tree_status(tree_id, "处理中...", "processing")
            self.update_tree_progress(tree_id, "解压中...")
            self.update_runtime_status(
                f"任务 {i+1}/{len(self.files_data)} · 正在处理",
                self.colors["info"],
            )
            
            orig, target = f['original_path'], os.path.join(f['directory'], f['clean_name'])
            
            if orig != target:
                if self.stop_requested.is_set():
                    stopped = True
                    self.resume_index = i
                    self.update_tree_status(tree_id, "已停止", "")
                    self.update_tree_progress(tree_id, "已停止")
                    break
                try:
                    if not os.path.exists(target):
                        os.rename(orig, target)
                        self.rename_history.append((target, orig))
                        self.log(f"重命名主卷: {os.path.basename(orig)} -> {f['clean_name']}", "ACTION")
                    orig = target
                except Exception as e:
                    self.log(f"重命名失败: {str(e)}", "ERROR")
                    self.update_tree_status(tree_id, "重命名失败", "error")
                    self.update_tree_progress(tree_id, "错误")
                    continue

            if f.get('is_split', False):
                base_no_ext = os.path.splitext(os.path.basename(orig))[0]
                main_split_family = self.get_split_family_name(os.path.basename(orig))
                for sibling in os.listdir(f['directory']):
                    if self.stop_requested.is_set():
                        stopped = True
                        self.resume_index = i
                        break
                    if "删" in sibling and (base_no_ext in sibling or self.is_secondary_part(sibling)):
                        s_old = os.path.join(f['directory'], sibling)
                        s_new = os.path.join(f['directory'], sibling.replace("删", ""))
                        if not os.path.exists(s_new):
                            try:
                                os.rename(s_old, s_new)
                                self.rename_history.append((s_new, s_old))
                                self.log(f"同步重命名分卷: {sibling}", "ACTION")
                            except: pass
                    else:
                        normalized_sibling, disguise_suffix = self.normalize_disguised_archive_name(sibling)
                        sibling_family = self.get_split_family_name(normalized_sibling)
                        if (
                            disguise_suffix
                            and main_split_family
                            and sibling_family == main_split_family
                        ):
                            s_old = os.path.join(f['directory'], sibling)
                            s_new = os.path.join(f['directory'], normalized_sibling)
                            if not os.path.isfile(s_old) or os.path.exists(s_new):
                                continue
                            try:
                                os.rename(s_old, s_new)
                                self.rename_history.append((s_new, s_old))
                                self.log(
                                    f"同步去除伪装分卷后缀: {sibling} -> {normalized_sibling}",
                                    "ACTION",
                                )
                            except Exception as e:
                                self.log(f"同步重命名伪装分卷失败: {sibling}，{e}", "ERROR")

                if stopped:
                    self.update_tree_status(tree_id, "已停止", "")
                    self.update_tree_progress(tree_id, "已停止")
                    break

            out_dir = re.sub(r'\.(?:mp4|17z|part\d+)$', '', os.path.splitext(orig)[0], flags=re.IGNORECASE)
            
            success, correct_pwd = self.extract_with_fallback(
                exe_path,
                engine_type,
                orig,
                out_dir,
                f['password'],
                tree_id=tree_id,
                lookup_names=f.get('lookup_names'),
            )

            if self.stop_requested.is_set() or self.last_extraction_stopped:
                stopped = True
                self.resume_index = i
                self.update_tree_status(tree_id, "已停止", "")
                self.update_tree_progress(tree_id, "已停止")
                break
            
            if success:
                success_total += 1
                if correct_pwd != f['password']:
                    f['password'] = correct_pwd
                    self.password_signal.emit(tree_id, correct_pwd)
                
                self.update_tree_status(tree_id, "解压成功", "success")
                source_info = f.get("source_info") if f.get("source_found") else None
                source_html_name = self.get_source_marker_name(source_info)
                source_html_dir = f.get("input_root") or out_dir

                if os.path.exists(out_dir):
                    processed, l_count = self.check_and_process_nested(
                        out_dir,
                        exe_path,
                        engine_type,
                        correct_pwd,
                        tree_id=tree_id,
                        ignored_names={source_html_name} if source_html_name else None,
                    )
                    if processed: layers_total += l_count

                if self.stop_requested.is_set() or self.last_extraction_stopped:
                    stopped = True
                    self.resume_index = i
                    self.update_tree_status(tree_id, "已停止", "")
                    self.update_tree_progress(tree_id, "已停止")
                    break

                if source_html_name:
                    self.create_source_marker_file(source_html_dir, source_info)
                
                self.update_tree_progress(tree_id, "100%")
            else:
                self.update_tree_status(tree_id, "解压失败", "error")
                self.update_tree_progress(tree_id, "中止")

            self.resume_index = i + 1

        if stopped or self.stop_requested.is_set():
            self.log(f"处理已停止，将从任务 {self.resume_index + 1} 继续", "ACTION")
            self.reset_ui(stopped=True)
            return

        if self.rename_history:
            self.undo_signal.emit(True)
        if getattr(self, "process_auto_undo", self.auto_undo_enabled): self.undo_rename()
        
        self.log(f"批量处理完成。成功: {success_total}, 穿透层数: {layers_total}", "SUCCESS")
        self.reset_ui()
        self.completion_signal.emit(f"成功: {success_total}\n自动穿透: {layers_total}层")

    def reset_ui(self, stopped=False):
        self.is_running = False
        self.current_process = None
        status = "已停止，可继续" if stopped else "任务已结束"
        color = self.colors["warning"] if stopped else self.colors["success"]
        self.controls_signal.emit(True, False, "开始处理", status, color)
        self.update_overview()

    def clear_list(self):
        if self.is_running: return
        self.tree.setRowCount(0)
        self.files_data, self.rename_history = [], []
        self.resume_index = 0
        self.current_task_index = 0
        self._clear_source_info()
        self._set_undo_available(False)
        self.log("列表已清空", "INFO")
        self.update_overview()

if __name__ == "__main__":
    app = DecompressApp()
    sys.exit(app.qt_app.exec())

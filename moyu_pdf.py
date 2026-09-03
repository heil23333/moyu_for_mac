#!/usr/bin/env python3
"""
摸鱼 PDF 阅读器 - MoYu PDF Viewer
一款为摸鱼场景设计的轻量级 PDF 阅读器。

功能特性：
- 透明度调节 (1% ~ 100%)
- 无边框窗口
- 窗口置顶
- 页面裁剪（去除白边）
- 无限滚动 + 懒加载
- 拖拽打开 PDF
- 右键菜单控制
- 浏览历史 + 设置持久化
"""

import sys
import os
import json
import time
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

import pymupdf  # PyMuPDF 新版 API（fitz 已弃用）
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QFileDialog, QMenu, QSizePolicy, QScrollArea, QInputDialog
)
from PySide6.QtCore import (
    Qt, QTimer, QPoint, QRect, QSize, QThread, Signal, QRectF
)
from PySide6.QtGui import (
    QPixmap, QImage, QRegion, QFont, QPainter, QColor, QPen,
    QCursor, QKeySequence, QShortcut, QDragEnterEvent, QDropEvent,
    QPainterPath
)


# ─────────────────────────────────────────────
# 配置
# ─────────────────────────────────────────────

@dataclass
class ViewerConfig:
    window_width: int = 500
    window_height: int = 700
    default_opacity: float = 0.7
    render_dpi: int = 150
    # 懒加载：视口上下各预渲染的页数
    buffer_pages: int = 2

    bg_color: str = "#FAFAFA"
    text_color: str = "#2C3E50"
    accent_color: str = "#3498DB"
    secondary_color: str = "#ECF0F1"
    border_color: str = "#BDC3C7"
    hover_color: str = "#E8F4FD"


# ─────────────────────────────────────────────
# PDF 单页渲染线程
# ─────────────────────────────────────────────

class PdfPageRenderWorker(QThread):
    """按需渲染指定页面；子线程全程使用线程安全的 QImage（QPixmap 只能在主线程操作）"""
    page_rendered = Signal(int, QImage, QImage, QRect)  # page_index, raw_image, display_image, crop_rect
    render_error = Signal(int, str)

    def __init__(self, pdf_path: str, page_index: int, dpi: int = 150,
                 crop_enabled: bool = True, target_width: int = 480):
        super().__init__()
        self.pdf_path = pdf_path
        self.page_index = page_index
        self.dpi = dpi
        self.crop_enabled = crop_enabled
        self.target_width = target_width

    def run(self):
        try:
            doc = pymupdf.open(self.pdf_path)
            page = doc[self.page_index]
            zoom = self.dpi / 72.0
            mat = pymupdf.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            fmt = QImage.Format.Format_RGB888
            raw_image = QImage(pix.samples, pix.width, pix.height, pix.stride, fmt)
            # 深拷贝：pix 内存属于 pymupdf，doc.close() 后失效（QImage 默认是浅拷贝）
            raw_image = raw_image.copy()
            doc.close()

            crop_rect = QRect(0, 0, raw_image.width(), raw_image.height())
            cropped = raw_image
            if self.crop_enabled:
                crop_rect = PageCropper.detect_content_rect_image(raw_image)
                cropped = raw_image.copy(crop_rect)

            # 在子线程用 QImage 缩放（线程安全）
            display = cropped.scaled(
                self.target_width,
                max(1, int(cropped.height() * self.target_width / max(cropped.width(), 1))),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )

            self.page_rendered.emit(self.page_index, raw_image, display, crop_rect)
        except Exception as e:
            self.render_error.emit(self.page_index, str(e))


class PdfMetaWorker(QThread):
    """后台读取 PDF 元数据（页数 + 每页尺寸），不阻塞 UI"""
    meta_loaded = Signal(int, list)  # total_pages, page_heights
    meta_error = Signal(str)

    def __init__(self, pdf_path: str):
        super().__init__()
        self.pdf_path = pdf_path
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            doc = pymupdf.open(self.pdf_path)
            total = len(doc)
            page_heights = []
            # 每页尺寸读取也可能耗时，逐页但可取消
            for i in range(total):
                if self._cancel:
                    doc.close()
                    return
                page = doc[i]
                rect = page.rect
                aspect = rect.height / rect.width
                page_heights.append(int(480 * aspect))
            doc.close()
            self.meta_loaded.emit(total, page_heights)
        except Exception as e:
            self.meta_error.emit(str(e))


# ─────────────────────────────────────────────
# 页面裁剪工具
# ─────────────────────────────────────────────

class PageCropper:
    @staticmethod
    def detect_content_rect(pixmap: QPixmap, threshold: int = 245) -> QRect:
        """QImage 专用检测（线程安全版本）"""
        if pixmap.isNull():
            return QRect(0, 0, pixmap.width(), pixmap.height())
        return PageCropper.detect_content_rect_image(pixmap.toImage(), threshold)

    @staticmethod
    def detect_content_rect_image(image: QImage, threshold: int = 245) -> QRect:
        """基于 QImage 的裁剪检测（可在子线程调用，不依赖 QPixmap）。

        注意：不用 pixelColor()——它在子线程每次都分配 QColor 临时对象，
        开销大且有跨线程风险。这里直接用 constBits() 原始字节扫描
        （RGB888，每像素 3 字节），速度快一个量级且零 Qt 对象分配。
        """
        w, h = image.width(), image.height()
        if w <= 0 or h <= 0:
            return QRect(0, 0, w, h)

        if image.format() != QImage.Format.Format_RGB888:
            image = image.convertToFormat(QImage.Format.Format_RGB888)
            w, h = image.width(), image.height()

        bpl = image.bytesPerLine()
        mv = memoryview(image.constBits())
        top, bottom, left, right = 0, h, 0, w

        # 上边
        for y in range(h):
            row = y * bpl
            found = False
            for x in range(0, w, 3):
                o = row + x * 3
                if max(mv[o], mv[o + 1], mv[o + 2]) < threshold:
                    top = y; found = True; break
            if found:
                break

        # 下边
        for y in range(h - 1, -1, -1):
            row = y * bpl
            found = False
            for x in range(0, w, 3):
                o = row + x * 3
                if max(mv[o], mv[o + 1], mv[o + 2]) < threshold:
                    bottom = y; found = True; break
            if found:
                break

        # 左边
        for x in range(w):
            col = x * 3
            found = False
            for y in range(0, h, 3):
                o = y * bpl + col
                if max(mv[o], mv[o + 1], mv[o + 2]) < threshold:
                    left = x; found = True; break
            if found:
                break

        # 右边
        for x in range(w - 1, -1, -1):
            col = x * 3
            found = False
            for y in range(0, h, 3):
                o = y * bpl + col
                if max(mv[o], mv[o + 1], mv[o + 2]) < threshold:
                    right = x; found = True; break
            if found:
                break

        margin = 15
        left = max(0, left - margin)
        top = max(0, top - margin)
        right = min(w, right + margin)
        bottom = min(h, bottom + margin)

        if right <= left or bottom <= top:
            return QRect(0, 0, w, h)
        return QRect(left, top, right - left, bottom - top)


# ─────────────────────────────────────────────
# 页面占位 Widget（懒加载骨架）
# ─────────────────────────────────────────────

PLACEHOLDER_HEIGHT = 600  # 初始占位高度，渲染后替换为实际高度

class PagePlaceholder(QWidget):
    """页面占位符 — 渲染前显示，渲染后替换为实际内容"""

    def __init__(self, page_index: int, total_pages: int, parent=None):
        super().__init__(parent)
        self.page_index = page_index
        self.total_pages = total_pages
        self.raw_pixmap: Optional[QPixmap] = None
        self.cropped_pixmap: Optional[QPixmap] = None
        self.display_pixmap: Optional[QPixmap] = None  # 预缩放后的显示图（渲染线程算好）
        self.is_rendered = False

        self.setFixedHeight(PLACEHOLDER_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(PLACEHOLDER_HEIGHT)

        # 页码标签
        self._page_label = QLabel(self)
        self._page_label.setAlignment(Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight)
        self._page_label.setStyleSheet("""
            QLabel {
                background-color: rgba(52, 152, 219, 0.85);
                color: white;
                padding: 2px 8px;
                border-radius: 8px;
                font-size: 11px;
                font-weight: bold;
            }
        """)
        self._page_label.setFixedHeight(22)
        self._page_label.setText(f"P{page_index + 1}/{total_pages}")
        self._page_label.adjustSize()
        self._page_label.hide()

    def paintEvent(self, event):
        """未渲染时绘制加载骨架；已渲染时直接画预缩放结果（缩放本身在渲染线程完成）"""
        painter = QPainter(self)
        if self.is_rendered and self.display_pixmap:
            # 直接用渲染线程算好的缩放图（避免主线程逐帧缩放大图）
            painter.drawPixmap(self.rect(), self.display_pixmap)
        else:
            painter.fillRect(self.rect(), QColor("#E8E8E8"))
            painter.setPen(QColor("#B0B0B0"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, f"📄 第 {self.page_index + 1} 页")
        painter.end()

    def set_rendered(self, pixmap: QPixmap, crop_enabled: bool = True,
                     display_pixmap: Optional[QPixmap] = None,
                     crop_rect: Optional[QRect] = None):
        """渲染完成后更新显示；display_pixmap/crop_rect 由渲染线程预计算，不再主线程重复扫描"""
        try:
            self.raw_pixmap = pixmap
            if crop_enabled:
                if crop_rect is not None:
                    # 子线程已算好裁剪区域（避免主线程重复检测）
                    self.cropped_pixmap = pixmap.copy(crop_rect)
                else:
                    rect = PageCropper.detect_content_rect(pixmap)
                    self.cropped_pixmap = pixmap.copy(rect)
            else:
                self.cropped_pixmap = pixmap
            self.is_rendered = True
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"[set_rendered 异常] 页 {self.page_index}: {e}")
            return

        if display_pixmap is not None:
            # 渲染线程已经算好（含裁剪+缩放）→ 直接用
            self.display_pixmap = display_pixmap
            self.setFixedHeight(display_pixmap.height())
        else:
            # 兜底（如裁剪开关切换时）：主线程临时算一次
            container_width = self.width()
            if container_width < 10:
                container_width = self.parent().width() - 16 if self.parent() else 480
            scaled = self.cropped_pixmap.scaledToWidth(
                container_width, Qt.TransformationMode.SmoothTransformation
            )
            self.display_pixmap = scaled
            self.setFixedHeight(scaled.height())
        self.update()

    def clear_pixmap(self):
        """释放内存"""
        self.raw_pixmap = None
        self.cropped_pixmap = None
        self.display_pixmap = None
        self.is_rendered = False
        self.setFixedHeight(PLACEHOLDER_HEIGHT)
        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.is_rendered and self.cropped_pixmap:
            container_width = self.width()
            if container_width > 10:
                scaled = self.cropped_pixmap.scaledToWidth(
                    container_width, Qt.TransformationMode.SmoothTransformation
                )
                self.display_pixmap = scaled
                self.setFixedHeight(scaled.height())
            self.update()
        self._page_label.move(
            self.width() - self._page_label.width() - 5,
            self.height() - self._page_label.height() - 5
        )

    def enterEvent(self, event):
        self._page_label.show()

    def leaveEvent(self, event):
        self._page_label.hide()


# ─────────────────────────────────────────────
# 设置持久化
# ─────────────────────────────────────────────

SETTINGS_FILE = os.path.join(os.path.expanduser("~"), ".moyu_pdf_settings.json")

def load_settings() -> dict:
    try:
        with open(SETTINGS_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_settings(settings: dict):
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(settings, f, indent=2)
    except Exception:
        pass


# ─────────────────────────────────────────────
# 主窗口
# ─────────────────────────────────────────────

class MoYuPdfViewer(QMainWindow):
    """摸鱼 PDF 阅读器主窗口"""

    def __init__(self, config: ViewerConfig = None):
        super().__init__()
        self.config = config or ViewerConfig()

        # 加载设置
        saved = load_settings()
        self._opacity = saved.get("opacity", self.config.default_opacity)
        self._crop_enabled = saved.get("crop_enabled", True)
        self._is_top = saved.get("is_top", True)
        self._scroll_sensitivity = saved.get("scroll_sensitivity", 3)  # 1~10 灵敏度
        self._recent_files: list[dict] = saved.get("recent_files", [])
        last_pos = saved.get("window_pos", None)
        last_size = saved.get("window_size", None)

        self.setWindowTitle("摸鱼 PDF 阅读器")

        flags = Qt.WindowType.FramelessWindowHint
        if self._is_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowOpacity(self._opacity)

        # 状态
        self.pdf_path: Optional[str] = None
        self.total_pages: int = 0
        self.worker: Optional[PdfPageRenderWorker] = None
        self._drag_pos: Optional[QPoint] = None
        self._is_hidden_mode = False
        self._pre_hide_pos: Optional[QPoint] = None
        self._pre_hide_size: Optional[QSize] = None

        # 懒加载：占位符列表 + 活跃 worker 缓存
        self._placeholders: list[PagePlaceholder] = []
        self._active_workers: dict[int, PdfPageRenderWorker] = {}
        self._render_queue: list[int] = []            # 等待渲染的页号（按优先级）
        self._retired_workers: list[PdfPageRenderWorker] = []  # 已过期但仍在跑的线程（防 GC 销毁崩溃）
        self._retired_meta: list[PdfMetaWorker] = []          # 已过期但仍在跑的元数据线程（防 GC 销毁崩溃）
        self._MAX_CONCURRENT: int = 3                 # 同时最多 3 个渲染线程
        self._last_visible_range: tuple[int, int] = (-1, -1)
        self._meta_worker: Optional[PdfMetaWorker] = None

        # 滚动防抖：快速滚动时合并高频事件，避免主线程被遍历/排序风暴打爆
        self._scroll_timer: QTimer = QTimer(self)
        self._scroll_timer.setSingleShot(True)
        self._scroll_timer.setInterval(80)  # 80ms 内只处理最后一次滚动
        self._scroll_timer.timeout.connect(self._process_scroll)
        self._scroll_pending = False

        self.setAcceptDrops(True)
        self.setMouseTracking(True)

        self._setup_ui()
        self._setup_shortcuts()

        # 恢复窗口
        if last_size:
            self.resize(last_size[0], last_size[1])
        else:
            self._center_window()

        if last_pos:
            self.move(last_pos[0], last_pos[1])
        else:
            self._center_window()

        self._apply_rounded_mask()

        # 恢复上次文件
        if self._recent_files:
            last_file = self._recent_files[0].get("path", "")
            if last_file and os.path.isfile(last_file):
                QTimer.singleShot(300, lambda: self._load_pdf(last_file))

    def _setup_ui(self):
        self.container = QWidget()
        self.container.setStyleSheet(f"""
            QWidget#container {{
                background-color: {self.config.bg_color};
                border: 1px solid {self.config.border_color};
                border-radius: 12px;
            }}
        """)
        self.container.setObjectName("container")

        main_layout = QVBoxLayout(self.container)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # 滚动区域
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll_area.setStyleSheet(f"""
            QScrollArea {{
                background-color: transparent;
                border: none;
            }}
            QScrollBar:vertical {{
                width: 6px;
                background: transparent;
            }}
            QScrollBar::handle:vertical {{
                background: {self.config.border_color};
                border-radius: 3px;
                min-height: 30px;
            }}
            QScrollBar::handle:vertical:hover {{
                background: {self.config.accent_color};
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0px;
            }}
        """)

        # 内容区域
        self.content_area = QWidget()
        self.content_layout = QVBoxLayout(self.content_area)
        self.content_layout.setContentsMargins(8, 8, 8, 8)
        self.content_layout.setSpacing(2)
        self.scroll_area.setWidget(self.content_area)

        # 进度条
        self.progress_label = QLabel("")
        self.progress_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.progress_label.setStyleSheet(f"color: {self.config.accent_color}; font-size: 12px; padding: 4px;")
        self.progress_label.hide()

        main_layout.addWidget(self.scroll_area)
        main_layout.addWidget(self.progress_label)

        self.setCentralWidget(self.container)

        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_window_menu)

        # 监听滚动
        self.scroll_area.verticalScrollBar().valueChanged.connect(self._on_scroll)
        self.scroll_area.verticalScrollBar().rangeChanged.connect(self._on_range_changed)

    def _on_range_changed(self, min_val, max_val):
        """滚动条范围变化时重新检查"""
        QTimer.singleShot(50, self._on_scroll)

    def _apply_rounded_mask(self):
        r = self.rect()
        radius = 12
        path = QPainterPath()
        path.addRoundedRect(float(r.x()), float(r.y()), float(r.width()), float(r.height()), radius, radius)
        mask = QRegion(path.toFillPolygon().toPolygon())
        self.setMask(mask)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not self._is_hidden_mode:
            self._apply_rounded_mask()

    def _setup_shortcuts(self):
        shortcuts = {
            "Ctrl+O": self._open_file,
            "Ctrl+C": self._toggle_crop,
            "Ctrl+T": self._toggle_top,
            "Ctrl+Q": self.close,
            "Escape": self.close,
            "Ctrl+=": lambda: self._adjust_opacity(5),
            "Ctrl+-": lambda: self._adjust_opacity(-5),
            "Ctrl+0": lambda: self._set_opacity(100),
            "Ctrl+Down": lambda: self._adjust_opacity(-5),
            "Ctrl+Up": lambda: self._adjust_opacity(5),
        }
        for key, handler in shortcuts.items():
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.activated.connect(handler)

    def _center_window(self):
        screen = QApplication.primaryScreen()
        if screen:
            geo = screen.availableGeometry()
            x = (geo.width() - self.config.window_width) // 2 + geo.x()
            y = (geo.height() - self.config.window_height) // 3 + geo.y()
            self.setGeometry(x, y, self.config.window_width, self.config.window_height)

    def _adjust_opacity(self, delta: int):
        current = int(self.windowOpacity() * 100)
        new_val = max(1, min(100, current + delta))
        self._set_opacity(new_val)

    def _set_opacity(self, value: int):
        self.setWindowOpacity(value / 100.0)
        self._opacity = value / 100.0

    def _input_custom_opacity(self):
        current = int(self.windowOpacity() * 100)
        val, ok = QInputDialog.getInt(
            self, "自定义透明度", "请输入透明度 (1~100%):", current, 1, 100, 1
        )
        if ok:
            self._set_opacity(val)

    def _set_scroll_sensitivity(self, value: float):
        self._scroll_sensitivity = max(0.1, min(10.0, float(value)))

    def _input_custom_scroll_sensitivity(self):
        current = self._scroll_sensitivity
        val, ok = QInputDialog.getDouble(
            self, "自定义滚动灵敏度", "请输入灵敏度 (0.1~10，数字越大滚得越快，支持小数):",
            current, 0.1, 10.0, 1
        )
        if ok:
            self._set_scroll_sensitivity(val)

    def _save_state(self):
        settings = {
            "opacity": self._opacity,
            "crop_enabled": self._crop_enabled,
            "is_top": self._is_top,
            "scroll_sensitivity": self._scroll_sensitivity,
            "recent_files": self._recent_files,
            "window_pos": [self.x(), self.y()],
            "window_size": [self.width(), self.height()],
        }
        if self.pdf_path:
            for r in self._recent_files:
                if r.get("path") == self.pdf_path:
                    r["position"] = self.scroll_area.verticalScrollBar().value()
                    break
        save_settings(settings)

    def closeEvent(self, event):
        self._save_state()
        # 优雅退出：先安全释放活跃 worker（转入退休列表），再统一等待所有线程结束
        self._retire_all()
        for worker in list(self._retired_workers):
            if worker and worker.isRunning():
                worker.wait(2000)  # 最多等 2 秒
        self._retired_workers.clear()
        # 退休的元数据线程也一起等待
        for worker in list(self._retired_meta):
            if worker and worker.isRunning():
                worker.wait(2000)
        self._retired_meta.clear()
        if self._meta_worker and self._meta_worker.isRunning():
            self._meta_worker.cancel()
            self._meta_worker.wait(2000)
        super().closeEvent(event)

    def _record_recent(self, path: str):
        old_pos = 0
        for r in self._recent_files:
            if r.get("path") == path:
                old_pos = r.get("position", 0) or 0
                break
        entry = {
            "path": path,
            "name": Path(path).stem,
            "time": time.strftime("%Y-%m-%d %H:%M"),
            "position": old_pos,
        }
        self._recent_files = [e for e in self._recent_files if e.get("path") != path]
        self._recent_files.insert(0, entry)
        self._recent_files = self._recent_files[:20]

    def _open_recent(self, path: str):
        if os.path.isfile(path):
            self._load_pdf(path)

    def _clear_history(self):
        self._recent_files = []

    def _open_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "打开 PDF 文件", "", "PDF 文件 (*.pdf)"
        )
        if path:
            self._load_pdf(path)

    def _goto_page(self):
        """跳转到指定页（输入 1~N，滚动到该页顶部）"""
        if not self._placeholders:
            return
        total = len(self._placeholders)
        value, ok = QInputDialog.getInt(
            self, "跳转到指定页",
            f"输入页码（1 ~ {total}）:",
            1, 1, total, 1
        )
        if not ok:
            return
        ph = self._placeholders[value - 1]
        # 目标滚动值 = 当前滚动值 + 页顶相对视口的偏移（mapTo 已包含所有
        # 布局平移；这个公式与 setWidgetResizable/边距无关，永远精确）
        bar = self.scroll_area.verticalScrollBar()
        target = bar.value() + ph.mapTo(self.scroll_area.viewport(), QPoint(0, 0)).y()
        target = max(0, min(target, bar.maximum()))
        bar.setValue(target)
        # 立即触发渲染（不等防抖窗口）
        self._on_scroll()

    def _load_pdf(self, path: str):
        """加载 PDF（异步）：后台读元数据 → 建占位符 → 按需懒加载"""
        # 旧 meta worker 若仍在运行：取消 + 转入退休列表，绝不裸覆盖引用
        # （裸覆盖会让 GC 销毁运行中的 QThread → SIGABRT 崩溃）
        if self._meta_worker is not None:
            old = self._meta_worker
            if old.isRunning():
                old.cancel()
                if old not in self._retired_meta:
                    self._retired_meta.append(old)
                    old.finished.connect(
                        lambda ww=old: self._retire_meta_done(ww)
                    )
            self._meta_worker = None

        # 安全释放渲染线程：同样原则，绝不裸 clear()
        self._retire_all()

        self.pdf_path = path
        self._record_recent(path)

        self.progress_label.show()
        self.progress_label.setText("⏳ 正在读取 PDF 信息…")

        self._meta_worker = PdfMetaWorker(path)
        self._meta_worker.meta_loaded.connect(
            lambda total, heights, p=path: self._on_meta_loaded(p, total, heights)
        )
        self._meta_worker.meta_error.connect(self._on_meta_error)
        self._meta_worker.start()

    def _on_meta_loaded(self, path: str, total: int, page_heights: list):
        """元数据加载完成：创建占位符骨架"""
        if path != self.pdf_path:
            return

        self.total_pages = total

        # 重置可见范围缓存，否则切换文件后首次滚动会被误判为“无变化”而跳过渲染
        self._last_visible_range = (-1, -1)

        self._clear_pages()
        self._placeholders.clear()

        filename = Path(path).stem
        self.setWindowTitle(f"摸鱼 PDF - {filename}")

        for i in range(total):
            ph = PagePlaceholder(i, total)
            ph.setFixedHeight(min(page_heights[i], 800))
            self._placeholders.append(ph)
            self.content_layout.addWidget(ph)

        self.progress_label.show()
        self.progress_label.setText(f"共 {total} 页")

        QTimer.singleShot(100, self._on_scroll)
        QTimer.singleShot(3000, self.progress_label.hide)

        saved_pos = 0
        for r in self._recent_files:
            if r.get("path") == path:
                saved_pos = r.get("position", 0) or 0
                break
        if saved_pos > 0:
            QTimer.singleShot(
                150,
                lambda: self.scroll_area.verticalScrollBar().setValue(
                    min(saved_pos, self.scroll_area.verticalScrollBar().maximum())
                )
            )

    def _on_meta_error(self, error: str):
        self.progress_label.show()
        self.progress_label.setText(f"加载失败: {error}")

    def _clear_pages(self):
        while self.content_layout.count() > 0:
            item = self.content_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._placeholders.clear()

    # ── 懒加载核心 ──

    def _on_scroll(self):
        """滚动事件入口：防抖合并高频滚动，真正处理延后到 _process_scroll"""
        if not self.pdf_path or not self._placeholders:
            return
        self._scroll_pending = True
        self._scroll_timer.start()  # 80ms 防抖窗口

    def _process_scroll(self):
        """滚动防抖后的真正处理：检查可见范围，按需渲染/释放页面，并实时记录位置"""
        if not self.pdf_path or not self._placeholders:
            self._scroll_pending = False
            return
        self._scroll_pending = False

        viewport_height = self.scroll_area.viewport().height()
        if viewport_height <= 0:
            return

        scroll_value = self.scroll_area.verticalScrollBar().value()

        # 实时更新历史记录中的浏览位置（防止崩溃/强制退出丢失）
        for r in self._recent_files:
            if r.get("path") == self.pdf_path:
                r["position"] = scroll_value
                break

        # 直接用每个 placeholder 的 geometry 计算可见范围（最准确）
        visible_start = None
        visible_end = None

        for i, ph in enumerate(self._placeholders):
            ph_y = ph.y()
            ph_h = ph.height()
            ph_top = ph_y
            ph_bottom = ph_y + ph_h

            if ph_bottom >= scroll_value and ph_top <= scroll_value + viewport_height:
                if visible_start is None:
                    visible_start = i
                visible_end = i

        if visible_start is None:
            visible_start = 0
            visible_end = 0

        buffer = self.config.buffer_pages
        range_start = max(0, visible_start - buffer)
        range_end = min(self.total_pages - 1, visible_end + buffer)

        if (range_start, range_end) == self._last_visible_range:
            return
        self._last_visible_range = (range_start, range_end)

        # 释放范围外的 worker 记录（但**不终止线程**：让它们自然跑完，
        # 回调里通过 pdf_path + page_index 校验过期，避免空引用崩溃）
        stale_workers = [i for i in self._active_workers if i < range_start or i > range_end]
        for i in stale_workers:
            w = self._active_workers.pop(i, None)
            # 线程可能还在跑：统一走退休流程（保持引用等 finished 信号，防 GC 销毁运行中线程崩溃）
            self._retire_worker(w)

        # 入队：可见范围内的未渲染页面（去重 + 按距离优先级）
        for i in range(range_start, range_end + 1):
            ph = self._placeholders[i]
            if not ph.is_rendered and i not in self._active_workers and i not in self._render_queue:
                self._render_queue.append(i)

        # 距离越近越优先（简单排序：按到视口中心的距离）
        self._render_queue.sort(key=lambda idx: abs(idx - visible_start))

        # 派发渲染队列（并发上限 3）
        self._pump_render_queue()

        # 释放距离视口远的已渲染页（仅当渲染队列压力不大时，避免快速滚动反复释放/重渲染）
        if len(self._active_workers) < self._MAX_CONCURRENT:
            for i, ph in enumerate(self._placeholders):
                if (i < range_start - 5 or i > range_end + 5) and ph.is_rendered:
                    ph.clear_pixmap()

    def _retire_worker(self, worker: Optional[PdfPageRenderWorker]):
        """安全释放单个 worker：若仍在运行则转入退休列表，等 finished 信号后再回收。

        关键：绝对不能直接丢引用让 GC 销毁运行中的 QThread（→ Qt qFatal SIGABRT）。
        """
        if worker is None:
            return
        # 竞态兜底：连接 finished 之前线程可能已结束，此时无需进退休列表
        if worker.isFinished():
            return
        if worker.isRunning():
            if worker not in self._retired_workers:
                self._retired_workers.append(worker)
                worker.finished.connect(
                    lambda ww=worker: self._retire_done(ww)
                )
        # 不运行则不保留（允许立即回收）

    def _retire_all(self):
        """安全释放所有活跃 worker（加载新 PDF/关闭窗口时调用）"""
        for worker in list(self._active_workers.values()):
            self._retire_worker(worker)
        self._active_workers.clear()

    def _retire_done(self, worker):
        """退休线程结束：从退休列表移除引用（允许 GC 清理）"""
        try:
            if worker in self._retired_workers:
                self._retired_workers.remove(worker)
        except ValueError:
            pass

    def _retire_meta_done(self, worker):
        """退休的元数据线程结束：从列表移除引用（允许 GC 清理）"""
        try:
            if worker in self._retired_meta:
                self._retired_meta.remove(worker)
        except ValueError:
            pass

    def _pump_render_queue(self):
        """渲染队列调度：已满则排队等待，有空位就派发（并发上限）"""
        while self._render_queue and len(self._active_workers) < self._MAX_CONCURRENT:
            page_index = self._render_queue.pop(0)
            ph = self._placeholders[page_index] if page_index < len(self._placeholders) else None
            if ph is None or ph.is_rendered:
                continue
            if page_index in self._active_workers:
                continue
            self._start_render(page_index)

    def _start_render(self, page_index: int):
        """启动单页渲染（裁剪+缩放都在子线程完成）"""
        target_width = self.content_area.width() - 16 if self.content_area else 480
        if target_width < 10:
            target_width = 480
        worker = PdfPageRenderWorker(
            self.pdf_path, page_index,
            dpi=self.config.render_dpi,
            crop_enabled=self._crop_enabled,
            target_width=target_width,
        )
        worker.path = self.pdf_path  # 供回调校验来源
        worker.page_rendered.connect(
            lambda idx, raw, disp, crop_rect, p=worker.pdf_path:
                self._on_page_rendered(p, idx, raw, disp, crop_rect)
        )
        worker.render_error.connect(
            lambda idx, err, p=worker.pdf_path: self._on_render_error(p, idx, err)
        )
        self._active_workers[page_index] = worker
        worker.start()

    def _on_page_rendered(self, path: str, page_index: int, raw_image: QImage,
                         display_image: QImage, crop_rect: QRect):
        """渲染完成回调（主线程执行）：把线程安全的 QImage 转成 QPixmap 再显示"""
        if path != self.pdf_path:
            self._retire_worker(self._active_workers.pop(page_index, None))
            return
        if page_index >= len(self._placeholders):
            self._retire_worker(self._active_workers.pop(page_index, None))
            return

        ph = self._placeholders[page_index]

        # 竞态保护（修正版）：worker 若已被滚动清理（移出活跃列表），
        # 其渲染结果属于过期滚动 → 丢弃，等 _process_scroll 重新入队。
        # 注意：不能用「页面是骨架状态」判断——初次渲染时页面本来就是骨架，
        # 那样会把第一次渲染结果也误丢弃（页面永不显示）。
        if page_index not in self._active_workers:
            self._pump_render_queue()
            return

        # ── QImage → QPixmap 转换必须在主线程（macOS 上子线程创建 QPixmap 会崩溃）──
        raw_pixmap = QPixmap.fromImage(raw_image)
        display_pixmap = QPixmap.fromImage(display_image)
        ph.set_rendered(raw_pixmap, self._crop_enabled, display_pixmap, crop_rect)
        # worker 可能仍在收尾：统一走退休流程，防 GC 销毁运行中线程
        self._retire_worker(self._active_workers.pop(page_index, None))

        self._pump_render_queue()   # 有空位 → 继续派发排队页

        rendered_count = sum(1 for ph in self._placeholders if ph.is_rendered)
        if rendered_count < self.total_pages:
            self.progress_label.show()
            self.progress_label.setText(f"已加载 {rendered_count}/{self.total_pages}")
        else:
            self.progress_label.setText(f"共 {self.total_pages} 页")
            QTimer.singleShot(2000, self.progress_label.hide)

    def _on_render_error(self, path: str, page_index: int, error: str):
        if path != self.pdf_path:
            return
        # worker 可能仍在收尾：统一走退休流程，防 GC 销毁运行中线程
        self._retire_worker(self._active_workers.pop(page_index, None))

    # ── 功能操作 ──

    def _toggle_crop(self):
        self._crop_enabled = not self._crop_enabled
        for i, ph in enumerate(self._placeholders):
            if ph.is_rendered and ph.raw_pixmap:
                ph.set_rendered(ph.raw_pixmap, self._crop_enabled)

    def _toggle_top(self):
        self._is_top = not self._is_top
        flags = self.windowFlags()
        if self._is_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.show()

    def _make_menu_style(self) -> str:
        return f"""
            QMenu {{
                background-color: {self.config.bg_color};
                border: 1px solid {self.config.border_color};
                border-radius: 10px;
                padding: 6px;
                font-size: 13px;
            }}
            QMenu::item {{
                padding: 7px 24px;
                border-radius: 6px;
                color: {self.config.text_color};
            }}
            QMenu::item:selected {{
                background-color: {self.config.hover_color};
            }}
            QMenu::separator {{
                height: 1px;
                background: {self.config.secondary_color};
                margin: 5px 10px;
            }}
            QMenu::submenu-open {{
                background-color: {self.config.hover_color};
            }}
        """

    def _show_window_menu(self, pos: QPoint):
        """右键菜单：收纳所有功能（摸鱼隐蔽）"""
        menu = QMenu(self)
        menu.setStyleSheet(self._make_menu_style())

        # 打开 / 跳转 / 最近
        menu.addAction("📂 打开 PDF", self._open_file)
        menu.addAction("📄 跳转到指定页…", self._goto_page)
        recent_menu = menu.addMenu("📜 最近打开")
        if self._recent_files:
            for r in self._recent_files[:10]:
                name = r.get("name", "?")
                path = r.get("path", "")
                time_str = r.get("time", "")
                page_pos = r.get("position", 0) or 0
                page_label = f"  📄 第 {page_pos // 700 + 1} 页" if page_pos > 0 else ""
                recent_menu.addAction(
                    f"{name}  ({time_str}){page_label}",
                    lambda p=path: self._open_recent(p)
                )
            recent_menu.addSeparator()
            recent_menu.addAction("🗑️ 清空历史", self._clear_history)
        else:
            recent_menu.addAction("(空)")

        menu.addSeparator()

        # 裁剪 / 置顶
        crop_state = "开" if self._crop_enabled else "关"
        menu.addAction(f"✂️ 裁剪: {crop_state}", self._toggle_crop)
        top_state = "开" if self._is_top else "关"
        menu.addAction(f"📌 置顶: {top_state}", self._toggle_top)

        # 透明度
        opacity_menu = menu.addMenu("👁️ 透明度")
        for v in (30, 40, 50, 60, 70, 80, 90, 100):
            check = "  ✓" if int(self._opacity * 100) == v else ""
            opacity_menu.addAction(f"{v}%{check}", lambda v=v: self._set_opacity(v))
        opacity_menu.addAction("✏️ 自定义…", self._input_custom_opacity)

        # 滚动灵敏度
        sens_menu = menu.addMenu("🖱️ 滚动灵敏度")
        for val, label in (
            (0.1, "极慢"), (0.5, "很慢"), (1, "慢"),
            (3, "标准(默认)"), (5, "较快"), (8, "最快")
        ):
            check = "  ✓" if abs(self._scroll_sensitivity - val) < 0.05 else ""
            sens_menu.addAction(
                f"{label}{check}",
                lambda v=val: self._set_scroll_sensitivity(v)
            )
        sens_menu.addAction("✏️ 自定义…", self._input_custom_scroll_sensitivity)

        # 快捷键说明
        help_menu = menu.addMenu("⌨️ 快捷键")
        for key, desc in (
            ("Ctrl+O", "打开 PDF"),
            ("Ctrl+C", "切换裁剪"),
            ("Ctrl+T", "切换置顶"),
            ("Ctrl+Q / Esc", "退出"),
            ("Ctrl++ / Ctrl+-", "透明度 +5 / -5"),
            ("Ctrl+0", "透明度恢复 100%"),
            ("Ctrl+↑ / Ctrl↓", "透明度 +5 / -5"),
            ("Ctrl+滚轮", "透明度调节"),
            ("双击窗口", "隐藏为图标"),
            ("双击图标", "恢复窗口"),
        ):
            help_menu.addAction(f"{key}  —  {desc}")

        menu.addSeparator()

        # 保存 / 隐藏 / 退出
        menu.addAction("💾 保存当前可见页", self._save_page_as_image)
        menu.addAction("─ 最小化", self._hide_to_menubar)
        menu.addAction("✕ 退出", self.close)

        # customContextMenuRequested 传出的是窗口局部坐标，必须转成全局
        # 坐标再弹出，否则菜单位置错位
        menu.exec(self.mapToGlobal(pos))

    def _save_page_as_image(self):
        """将当前第一页（视口内最靠上的页）保存为 PNG"""
        if not self._placeholders:
            return
        for ph in self._placeholders:
            if ph.is_rendered and ph.cropped_pixmap:
                default = f"{Path(self.pdf_path).stem}_page{ph.page_index + 1}.png"
                path, _ = QFileDialog.getSaveFileName(self, "保存页面", default, "PNG 图片 (*.png)")
                if path:
                    ok = ph.cropped_pixmap.save(path, "PNG")
                    if not ok:
                        from PySide6.QtWidgets import QMessageBox
                        QMessageBox.warning(self, "保存失败", f"无法保存到: {path}")
                break

    # ── 拖拽 ──

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.toLocalFile().lower().endswith(".pdf"):
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event: QDropEvent):
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path.lower().endswith(".pdf"):
                self._load_pdf(path)
                event.acceptProposedAction()
                return

    # ── 鼠标交互（移动 + 隐藏/恢复）──

    def mousePressEvent(self, event):
        # 隐藏模式下：单击不恢复（仅用于拖动图标），双击才恢复
        if self._is_hidden_mode:
            if event.button() == Qt.MouseButton.LeftButton:
                self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                self._hide_click_pos = event.globalPosition().toPoint()
                event.accept()
                return
            event.ignore()
            return

        # 正常模式：左键任意位置拖拽移动窗口
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_pos is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if self._is_hidden_mode:
            # 双击图标 → 恢复窗口
            self._restore_from_menubar()
            event.accept()
            return
        # 正常模式：双击 → 隐藏为图标
        self._hide_to_menubar()
        event.accept()

    # ── 隐藏/恢复 ──

    def _hide_to_menubar(self):
        """缩小为一个可拖动的小图标（双击恢复），出现在原窗口位置"""
        screen = QApplication.primaryScreen()
        if not screen:
            return

        self._pre_hide_pos = self.pos()
        self._pre_hide_size = self.size()

        # 隐藏内容（避免小图标被内容遮挡）
        self.container.hide()

        icon_size = 36
        old_center = self.frameGeometry().center()
        x = old_center.x() - icon_size // 2
        y = old_center.y() - icon_size // 2

        self.setFixedSize(icon_size, icon_size)
        self.move(x, y)
        self.setWindowOpacity(0.9)
        self._is_hidden_mode = True

        # 圆角方形 mask（贴合文档图标风格）
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, icon_size, icon_size), 8.0, 8.0)
        self.setMask(QRegion(path.toFillPolygon().toPolygon()))
        self.update()

    def _restore_from_menubar(self):
        """从隐藏状态恢复：还原大小、位置、透明度"""
        if self._pre_hide_pos is not None:
            self.move(self._pre_hide_pos)
        if self._pre_hide_size is not None:
            # 必须先解除 setFixedSize 的尺寸约束（否则 resize 被限制在 36x36 无效）
            self.setMinimumSize(0, 0)
            self.setMaximumSize(16777215, 16777215)
            self.resize(self._pre_hide_size)
        self.setWindowOpacity(self._opacity)
        self._is_hidden_mode = False

        self.container.show()
        self.clearMask()
        self._apply_rounded_mask()
        self.update()

    def paintEvent(self, event):
        """窗口级绘制：隐藏模式下绘制小图标"""
        if self._is_hidden_mode:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            rect = self.rect()

            # 深灰圆底 + 白色圆角纸片（文档图标）
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#3A3A3A"))
            painter.drawRoundedRect(rect, 8, 8)

            paper = rect.adjusted(7, 5, -7, -5)
            painter.setBrush(QColor("#FFFFFF"))
            painter.drawRoundedRect(paper, 3, 3)

            painter.setPen(QPen(QColor("#A0A0A0"), 2))
            line_margin = 10
            line_y = paper.y() + 9
            for _ in range(3):
                painter.drawLine(paper.x() + line_margin, line_y,
                                  paper.right() - line_margin, line_y)
                line_y += 6
            painter.end()
        else:
            super().paintEvent(event)

    def wheelEvent(self, event):
        """滚轮：Ctrl=调透明度，普通=按灵敏度滚动（支持小数灵敏度）"""
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            step = 5 if delta > 0 else -5
            self._adjust_opacity(step)
            event.accept()
        else:
            delta = event.angleDelta().y()
            scrollbar = self.scroll_area.verticalScrollBar()
            base_step = 120
            scale = self._scroll_sensitivity / 3.0
            precise = delta / base_step * scrollbar.singleStep() * scale
            scroll_step = int(round(precise))
            if scroll_step == 0 and precise != 0:
                scroll_step = 1 if precise > 0 else -1
            if scroll_step:
                scrollbar.setValue(scrollbar.value() - scroll_step)
            event.accept()


# ─────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────

def main():
    app = QApplication(sys.argv)

    font = QFont()
    font.setFamily("PingFang SC")
    font.setPointSize(13)
    app.setFont(font)

    config = ViewerConfig()
    viewer = MoYuPdfViewer(config)
    viewer.show()

    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        QTimer.singleShot(300, lambda: viewer._load_pdf(sys.argv[1]))

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
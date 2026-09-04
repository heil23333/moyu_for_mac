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
    QLabel, QFileDialog, QMenu, QSizePolicy, QScrollArea, QInputDialog,
    QSpinBox, QFrame
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


PLACEHOLDER_HEIGHT = 600  # 初始占位高度，渲染后替换为实际高度


# ─────────────────────────────────────────────
# PDF 单页渲染线程
# ─────────────────────────────────────────────

class PdfPageRenderWorker(QThread):
    """按需渲染指定页面；子线程全程使用线程安全的 QImage（QPixmap 只能在主线程操作）"""
    page_rendered = Signal(int, QImage, QImage, QRect)  # page_index, raw_image, display_image, crop_rect
    render_error = Signal(int, str)

    def __init__(self, pdf_path: str, page_index: int, dpi: int = 150,
                 crop_enabled: bool = True, target_width: int = 480,
                 crop_margins: Optional[list[int]] = None):
        super().__init__()
        self.pdf_path = pdf_path
        self.page_index = page_index
        self.dpi = dpi
        self.crop_enabled = crop_enabled
        self.target_width = target_width
        # crop_margins = [top, left, right, bottom]：由当前可见页检测一次得到，
        # 直接按四边距算裁剪矩形，不再逐页调 detect_content_rect_image。
        self.crop_margins = crop_margins or [0, 0, 0, 0]

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

            w, h = raw_image.width(), raw_image.height()
            crop_rect = QRect(0, 0, w, h)
            cropped = raw_image
            if self.crop_enabled and any(self.crop_margins):
                # 按四边距裁剪；边距由主窗口检测一次，存 settings 持久化
                top, left, right, bottom = self.crop_margins
                crop_rect = QRect(left, top, max(0, w - left - right), max(0, h - top - bottom))
                if crop_rect.width() > 0 and crop_rect.height() > 0:
                    cropped = raw_image.copy(crop_rect)
            # 若 crop_enabled 但 crop_margins 全零（刚开启还没检测）：不裁剪，等检测完重渲染

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
            # PLACEHOLDER_HEIGHT 作为初始占位高度，按宽高比推算
            for i in range(total):
                if self._cancel:
                    doc.close()
                    return
                page = doc[i]
                rect = page.rect
                aspect = rect.height / rect.width
                page_heights.append(int(PLACEHOLDER_HEIGHT * aspect))
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
        """主线程版本：接收 QPixmap，内部转 QImage 检测（子线程请用 detect_content_rect_image）"""
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
            for x in range(0, w):
                o = row + x * 3
                if max(mv[o], mv[o + 1], mv[o + 2]) < threshold:
                    top = y; found = True; break
            if found:
                break

        # 下边
        for y in range(h - 1, -1, -1):
            row = y * bpl
            found = False
            for x in range(0, w):
                o = row + x * 3
                if max(mv[o], mv[o + 1], mv[o + 2]) < threshold:
                    bottom = y; found = True; break
            if found:
                break

        # 左边
        for x in range(w):
            col = x * 3
            found = False
            for y in range(0, h):
                o = y * bpl + col
                if max(mv[o], mv[o + 1], mv[o + 2]) < threshold:
                    left = x; found = True; break
            if found:
                break

        # 右边
        for x in range(w - 1, -1, -1):
            col = x * 3
            found = False
            for y in range(0, h):
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
# 手动裁剪交互层
# ─────────────────────────────────────────────

class CropOverlay(QWidget):
    """手动裁剪交互层：半透明遮罩 + 拖拽框选矩形。

    覆盖在滚动视口上，左键拖拽画选区，松开（选区 ≥10px）自动确认；
    Esc / 右键取消；Enter 确认当前选区。
    confirmed 信号带视口坐标的选区 QRect；cancelled 信号表示取消。
    """

    confirmed = Signal(QRect)   # 视口坐标的选区
    cancelled = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self._drag_start: Optional[QPoint] = None
        self._sel: Optional[QRect] = None
        # 预创建字体（避免每次 paintEvent 重复创建）
        self._label_font = QFont()
        self._label_font.setPointSize(11)
        self._label_font.setBold(True)

    def begin(self):
        """显示并抢占焦点（键盘 Esc/Enter 生效）"""
        self._drag_start = None
        self._sel = None
        self.show()
        self.raise_()
        self.setFocus()
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        # 整层半透明遮罩（底下的 PDF 隐约可见）
        p.fillRect(self.rect(), QColor(16, 24, 40, 110))
        if self._sel is not None and not self._sel.isEmpty():
            sel = self._sel.normalized().intersected(self.rect())
            if not sel.isEmpty():
                # 选区内恢复透明（差集画法）
                p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
                p.fillRect(sel, QColor(0, 0, 0, 255))
                p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
                # 边框 + 尺寸标注
                p.setPen(QPen(QColor("#3498DB"), 2))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRect(sel)
                p.setFont(self._label_font)
                p.setPen(QColor("#FFFFFF"))
                label = f"{sel.width()} × {sel.height()}"
                lx = sel.x()
                ly = sel.y() - 6
                if ly < 6:
                    ly = sel.y() + 6
                p.drawText(QPoint(lx, ly), label)
        p.end()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_start = e.position().toPoint()
            self._sel = None
            self.update()
            e.accept()
        elif e.button() == Qt.MouseButton.RightButton:
            self.cancelled.emit()
            e.accept()
        else:
            super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._drag_start is not None:
            self._sel = QRect(self._drag_start, e.position().toPoint())
            self.update()
            e.accept()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self._drag_start is not None:
            self._sel = QRect(self._drag_start, e.position().toPoint()).normalized()
            self._drag_start = None
            if self._sel.width() >= 10 and self._sel.height() >= 10:
                self.confirmed.emit(self._sel)   # 松开即确认
            else:
                self._sel = None                 # 太小视为误触，清除
                self.update()
            e.accept()
        else:
            super().mouseReleaseEvent(e)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()
            e.accept()
        elif e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self._sel is not None and not self._sel.isEmpty():
                self.confirmed.emit(self._sel.normalized())
            e.accept()
        else:
            super().keyPressEvent(e)


# ─────────────────────────────────────────────
# 边距调整面板
# ─────────────────────────────────────────────

class CropMarginPanel(QWidget):
    """显示并编辑当前裁剪四边距的小面板（悬浮在窗口底部）。

    四个 SpinBox 对应 上/下/左/右，数值变化时通过 callback 实时应用。
    """

    def __init__(self, parent=None, on_apply=None):
        super().__init__(parent)
        self._on_apply = on_apply  # callback(margins: list[int])
        self.setStyleSheet("""
            QFrame {
                background: rgba(30, 30, 30, 220);
                border-radius: 8px;
                padding: 4px 8px;
            }
            QLabel { color: #FFFFFF; font-size: 11px; }
            QSpinBox {
                background: #3A3A3A; color: #FFFFFF; border: 1px solid #555;
                border-radius: 4px; padding: 2px 4px; font-size: 12px; width: 44px;
            }
            QSpinBox:focus { border-color: #3498DB; }
        """)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(6)

        self._spins: dict[str, QSpinBox] = {}
        for label_text in ["上", "下", "左", "右"]:
            lbl = QLabel(label_text)
            spin = QSpinBox()
            spin.setRange(0, 999)
            spin.setSingleStep(1)
            spin.valueChanged.connect(self._on_value_changed)
            self._spins[label_text] = spin
            layout.addWidget(lbl)
            layout.addWidget(spin)
        self.hide()

    def set_margins(self, margins: list[int]):
        """更新面板数值（外部调用，不触发回调）"""
        top, left, right, bottom = margins if len(margins) >= 4 else [0, 0, 0, 0]
        # 阻塞信号避免循环触发
        for s in self._spins.values():
            s.blockSignals(True)
        self._spins["上"].setValue(top)
        self._spins["下"].setValue(bottom)
        self._spins["左"].setValue(left)
        self._spins["右"].setValue(right)
        for s in self._spins.values():
            s.blockSignals(False)

    def _on_value_changed(self, _):
        if self._on_apply:
            margins = [
                self._spins["上"].value(),
                self._spins["左"].value(),
                self._spins["右"].value(),
                self._spins["下"].value(),
            ]
            self._on_apply(margins)


# ─────────────────────────────────────────────
# 页面占位 Widget（懒加载骨架）
# ─────────────────────────────────────────────

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
        self.crop_rect: Optional[QRect] = None   # 最近一次有效裁剪区域（手动裁剪恢复用）
        self.manual_cropped = False              # 当前是否被手动裁剪（显示的是局部区域）

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
                self.crop_rect = crop_rect or rect  # 记录裁剪区域（自动/手动都记）
            else:
                self.cropped_pixmap = pixmap
                self.crop_rect = None  # 无裁剪
            self.is_rendered = True
            # 只有标准渲染（crop_enabled=True，即自动裁白边）才重置 manual_cropped；
            # crop_enabled=False（手动裁剪路径）时不动——由 _apply_manual_crop 显式管理
            if crop_enabled:
                self.manual_cropped = False
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
        # 裁剪模式："off" / "auto" / "manual"（三态切换）
        self._crop_mode: str = saved.get("crop_mode", "auto")  # 恢复上次保存的模式
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
        self._hide_click_pos: Optional[QPoint] = None
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
        # 手动裁剪状态
        self._manual_crop_active = False              # 当前是否在手动裁剪交互中
        self._manual_crop_page: Optional[int] = None # 被手动裁剪的页码（用于恢复）
        self._manual_crop_orig = None                 # (display_pixmap, crop_rect) 该页裁剪前的原始显示
        self._crop_overlay: Optional[CropOverlay] = None
        # 自动裁剪边距 [top, left, right, bottom]（由当前可见页检测得出，存入 settings 持久化）
        self._crop_margins: list[int] = [0, 0, 0, 0]
        self._last_visible_range: tuple[int, int] = (-1, -1)
        self._meta_worker: Optional[PdfMetaWorker] = None

        # 滚动防抖：快速滚动时合并高频事件，避免主线程被遍历/排序风暴打爆
        self._scroll_timer: QTimer = QTimer(self)
        self._scroll_timer.setSingleShot(True)
        self._scroll_timer.setInterval(80)  # 80ms 内只处理最后一次滚动
        self._scroll_timer.timeout.connect(self._process_scroll)
        self._scroll_pending = False

        # 边距面板防抖：快速调节 SpinBox 时避免疯狂重渲染
        self._margin_timer: QTimer = QTimer(self)
        self._margin_timer.setSingleShot(True)
        self._margin_timer.setInterval(200)
        self._margin_timer.timeout.connect(self._apply_pending_margins)

        self.setAcceptDrops(True)
        self.setMouseTracking(True)

        self._setup_ui()
        self._setup_shortcuts()

        # 恢复窗口尺寸和位置
        if last_size:
            self.resize(last_size[0], last_size[1])
        if last_pos:
            self.move(last_pos[0], last_pos[1])
        if not last_size and not last_pos:
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
        self.content_area.setStyleSheet(f"background-color: {self.config.bg_color};")
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

        # 边距调整面板（悬浮在窗口底部，裁剪模式非 off 时显示）
        self._crop_margin_panel = CropMarginPanel(self, on_apply=self._on_margin_panel_apply)
        self._position_crop_panel()

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
        if self._crop_margin_panel.isVisible():
            self._position_crop_panel()

    def _setup_shortcuts(self):
        shortcuts = {
            "Ctrl+O": self._open_file,
            "Ctrl+C": self._cycle_crop_mode,
            "Ctrl+T": self._toggle_top,
            "Ctrl+Q": self.close,
            "Escape": self._hide_to_menubar,  # Esc 最小化（小图标），不是退出
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
        # 始终将当前文件的裁剪边距写入 recent_files（即使全零也要写，否则关闭裁剪后下次还会恢复旧值）
        if self.pdf_path:
            for r in self._recent_files:
                if r.get("path") == self.pdf_path:
                    r["crop_margins"] = self._crop_margins
                    break
        settings = {
            "opacity": self._opacity,
            "crop_mode": self._crop_mode,  # "off" / "auto" / "manual"
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
        old_entry = {}
        for r in self._recent_files:
            if r.get("path") == path:
                old_entry = r
                break
        entry = {
            "path": path,
            "name": Path(path).stem,
            "time": time.strftime("%Y-%m-%d %H:%M"),
            "position": old_entry.get("position", 0) or 0,
        }
        # 保留旧条目的 crop_margins（不丢失）
        if "crop_margins" in old_entry:
            entry["crop_margins"] = old_entry["crop_margins"]
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

        # 重置手动裁剪状态（切换文件后旧裁剪无效）
        self._manual_crop_active = False
        self._manual_crop_page = None
        self._manual_crop_orig = None
        if self._crop_overlay:
            self._crop_overlay.hide()
        # 恢复该文件的裁剪边距（从历史记录；无则全零，由 _detect_auto_margins 时填入）
        self._crop_margins = [0, 0, 0, 0]
        for r in self._recent_files:
            if r.get("path") == path:
                self._crop_margins = r.get("crop_margins", [0, 0, 0, 0])
                break

        self.pdf_path = path
        self._record_recent(path)

        # 重置滚动条到顶部（切换文件后不继承旧文件的滚动位置）
        self.scroll_area.verticalScrollBar().setValue(0)

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
            crop_enabled=self._crop_mode != "off",
            target_width=target_width,
            crop_margins=self._crop_margins,  # 左右边距，由 _detect_auto_margins 检测一次，存 settings 持久化
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
        ph.set_rendered(raw_pixmap, self._crop_mode != "off", display_pixmap, crop_rect)

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

    def _set_crop_mode(self, mode: str):
        """切换裁剪模式：off / auto / manual；重复点击当前模式触发重新操作"""
        # 先清理当前模式状态（manual 需要恢复被裁页）
        if self._crop_mode == "manual" and mode != "manual":
            self._cancel_manual_crop()
        # 设置新模式
        self._crop_mode = mode
        if mode == "auto":
            self._detect_auto_margins()
            # 检测失败（无已渲染页）时保持 auto 模式不变，等页面渲染完后可再次触发
        elif mode == "manual":
            self._start_manual_crop()
        elif mode == "off":
            self._clear_crop_and_rerender()
        # 面板显示/隐藏
        if mode != "off":
            self._crop_margin_panel.set_margins(self._crop_margins)
            self._crop_margin_panel.show()
            self._position_crop_panel()
        else:
            self._crop_margin_panel.hide()

    def _cycle_crop_mode(self):
        """快捷键 Ctrl+C 循环：关 → 自动 → 手动 → 关"""
        order = {"off": "auto", "auto": "manual", "manual": "off"}
        self._set_crop_mode(order.get(self._crop_mode, "off"))

    def _detect_auto_margins(self):
        """自动检测当前可见页的左右白边，得到固定边距，所有页统一应用"""
        if not self.pdf_path or not self._placeholders:
            return
        # 找当前可见第一个已渲染页
        ref_ph = None
        vp = self.scroll_area.viewport()
        for ph in self._placeholders:
            pos = ph.mapTo(vp, QPoint(0, 0))
            if 0 <= pos.y() < vp.height() and ph.is_rendered and ph.raw_pixmap is not None:
                ref_ph = ph
                break
        if ref_ph is None:
            # 页面还在渲染中，等 500ms 后重试一次
            QTimer.singleShot(500, self._detect_auto_margins)
            return
        raw = ref_ph.raw_pixmap
        rect = PageCropper.detect_content_rect(raw)
        # 四边距：top, left, right, bottom
        top    = rect.y()
        left   = rect.x()
        right  = raw.width()  - rect.x() - rect.width()
        bottom = raw.height() - rect.y() - rect.height()
        self._crop_margins = [top, left, right, bottom]
        # 存入历史记录持久化
        for r in self._recent_files:
            if r.get("path") == self.pdf_path:
                r["crop_margins"] = self._crop_margins
                break
        # 用检测区域刷新当前页
        ref_ph.set_rendered(raw, True, None, rect)
        # 其余页：清除 pixmap 重新渲染
        for ph in self._placeholders:
            if ph is not ref_ph and ph.is_rendered:
                ph.clear_pixmap()
        self._last_visible_range = (-1, -1)
        QTimer.singleShot(50, self._on_scroll)

    def _clear_crop_and_rerender(self):
        """关闭裁剪：清零边距，清除所有页 pixmap，重新渲染"""
        self._crop_margins = [0, 0, 0, 0]
        for ph in self._placeholders:
            if ph.is_rendered:
                ph.clear_pixmap()
        self._last_visible_range = (-1, -1)
        QTimer.singleShot(50, self._on_scroll)

    def _position_crop_panel(self):
        """把边距面板定位到窗口底部居中"""
        pw = self._crop_margin_panel.width() or 200
        ph = self._crop_margin_panel.height() or 28
        x = (self.width() - pw) // 2
        y = self.height() - ph - 6
        self._crop_margin_panel.move(x, y)

    def _on_margin_panel_apply(self, margins: list[int]):
        """面板 SpinBox 数值变化时：存边距 + 启动防抖（200ms 后才真正重渲染）"""
        self._crop_margins = margins
        # 存入历史记录持久化
        if self.pdf_path:
            for r in self._recent_files:
                if r.get("path") == self.pdf_path:
                    r["crop_margins"] = self._crop_margins
                    break
        self._margin_timer.start()  # 200ms 防抖窗口

    def _apply_pending_margins(self):
        """防抖后真正重渲染：清除所有页 pixmap，用最新边距重新渲染"""
        for ph in self._placeholders:
            if ph.is_rendered:
                ph.clear_pixmap()
        self._last_visible_range = (-1, -1)
        QTimer.singleShot(50, self._on_scroll)

    def _toggle_top(self):
        self._is_top = not self._is_top
        flags = self.windowFlags()
        if self._is_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.show()

    # ── 手动裁剪 ──

    def _start_manual_crop(self):
        """进入/退出手动裁剪模式：若已在裁剪则取消；否则在视口上叠加 CropOverlay 进行框选"""
        if not self.pdf_path or not self._placeholders:
            return
        # 若已激活手动裁剪（正在裁或已裁过）→ 取消并恢复
        if self._manual_crop_active or self._manual_crop_page is not None:
            self._cancel_manual_crop()
            return
        vp = self.scroll_area.viewport()
        if self._crop_overlay is None:
            self._crop_overlay = CropOverlay(vp)
            self._crop_overlay.confirmed.connect(self._apply_manual_crop)
            self._crop_overlay.cancelled.connect(self._cancel_manual_crop)
        # 覆盖整个视口，十字光标
        self._crop_overlay.setGeometry(vp.rect())
        self._crop_overlay.begin()
        self._manual_crop_active = True
        # 隐藏滚动条防误操作，记光标
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def _apply_manual_crop(self, sel: QRect):
        """应用手动裁剪：把视口选区映射到视口内中心页并裁剪该页"""
        vp = self.scroll_area.viewport()
        # 找选区中心所在的占位符页（最准确）
        center = sel.center()
        target_ph = None
        for ph in self._placeholders:
            pos = ph.mapTo(vp, QPoint(0, 0))
            if pos.y() <= center.y() <= pos.y() + ph.height():
                target_ph = ph
                break
        # 若找不到（罕见）就取视口最上方的页
        if target_ph is None:
            for ph in self._placeholders:
                pos = ph.mapTo(vp, QPoint(0, 0))
                if pos.y() + ph.height() >= 0 and pos.y() < vp.height():
                    target_ph = ph
                    break
        if target_ph is None or not target_ph.is_rendered or target_ph.display_pixmap is None:
            self._finish_manual_crop()
            return
        # 首次对该页手动裁剪：保存原始状态（仅保存一次，多次裁剪只保留首次的原始）
        if self._manual_crop_page != target_ph.page_index:
            self._manual_crop_page = target_ph.page_index
            self._manual_crop_orig = (target_ph.display_pixmap, target_ph.crop_rect)
        # 换算视口选区到该页的局部坐标（display_pixmap 尺寸与 placeholder 一致）
        ph_pos = target_ph.mapTo(vp, QPoint(0, 0))
        local = QRect(sel.x() - ph_pos.x(), sel.y() - ph_pos.y(), sel.width(), sel.height())
        local = local.intersected(QRect(0, 0, target_ph.display_pixmap.width(), target_ph.display_pixmap.height()))
        if local.width() < 5 or local.height() < 5:
            self._finish_manual_crop()
            return
        cropped = target_ph.display_pixmap.copy(local)
        if cropped.isNull():
            self._finish_manual_crop()
            return
        # 应用到该页：crop_enabled=False 使用提供的 display_pixmap
        target_ph.manual_cropped = True
        target_ph.set_rendered(target_ph.raw_pixmap, False, cropped, None)
        self._finish_manual_crop()

    def _finish_manual_crop(self):
        """结束交互（overlay 隐藏，滚动条恢复）但保持手动裁剪状态（菜单显示开）"""
        if self._crop_overlay:
            self._crop_overlay.hide()
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.unsetCursor()

    def _cancel_manual_crop(self):
        """取消手动裁剪并恢复被裁页为原始显示"""
        self._finish_manual_crop()
        if self._manual_crop_page is not None and self._manual_crop_page < len(self._placeholders):
            ph = self._placeholders[self._manual_crop_page]
            if ph.manual_cropped and self._manual_crop_orig is not None:
                orig_display, orig_crop = self._manual_crop_orig
                ph.manual_cropped = False
                # 恢复原始显示：crop_enabled=True + 原始 crop_rect（自动或全页）
                ph.set_rendered(ph.raw_pixmap, True, orig_display, orig_crop)
        self._manual_crop_active = False
        self._manual_crop_page = None
        self._manual_crop_orig = None

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

        # 裁剪白边（三个独立选项）
        crop_menu = menu.addMenu("✂️ 裁剪白边")
        for mode, label in [("off", "关闭"), ("auto", "自动检测"), ("manual", "手动裁切")]:
            check = "  ✓" if self._crop_mode == mode else ""
            crop_menu.addAction(
                f"{label}{check}",
                lambda m=mode: self._set_crop_mode(m)
            )
        # 置顶
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
            ("Ctrl+Q", "退出"),
            ("Esc", "最小化"),
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
        self._pre_hide_crop_mode = self._crop_mode  # 保存裁剪模式（恢复时还原）

        # 隐藏所有子 widget（不用 setCentralWidget(None)，避免 Qt 销毁 container）
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
        """从隐藏状态恢复：还原大小、位置、透明度、裁剪模式"""
        if self._pre_hide_pos is not None:
            self.move(self._pre_hide_pos)
        if self._pre_hide_size is not None:
            # 必须先解除 setFixedSize 的尺寸约束（否则 resize 被限制在 36x36 无效）
            self.setMinimumSize(0, 0)
            self.setMaximumSize(16777215, 16777215)
            self.resize(self._pre_hide_size)
        self.setWindowOpacity(self._opacity)
        self._is_hidden_mode = False

        # 恢复裁剪模式（最小化前的状态）
        saved_crop = getattr(self, '_pre_hide_crop_mode', None)
        if saved_crop and saved_crop != self._crop_mode:
            self._crop_mode = saved_crop
            # 恢复面板显示
            if self._crop_mode != "off":
                self._crop_margin_panel.set_margins(self._crop_margins)
                self._crop_margin_panel.show()
                self._position_crop_panel()
            else:
                self._crop_margin_panel.hide()

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

            # 先填充背景（覆盖任何子 widget 残留绘制）
            painter.fillRect(rect, QColor("#2C2C2C"))

            # 深灰圆底 + 白色圆角纸片（文档图标）
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#3A3A3A"))
            painter.drawRoundedRect(rect, 8, 8)

            paper = rect.adjusted(7, 5, -7, -5)
            painter.setBrush(QColor("#FFFFFF"))
            painter.drawRoundedRect(paper, 3, 3)

            painter.setPen(QPen(QColor("#A0A0A0"), 1.5))
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
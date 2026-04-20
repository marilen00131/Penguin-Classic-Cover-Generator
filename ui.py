import io
import json
import os
import shutil
import uuid

from calibre.gui2.actions import InterfaceAction
from calibre.gui2 import error_dialog, info_dialog

from qt.core import (
    Qt, QDialog, QLabel, QPushButton, QWidget, QVBoxLayout, QHBoxLayout,
    QFormLayout, QGroupBox, QSpinBox, QDoubleSpinBox, QLineEdit, QFileDialog,
    QDialogButtonBox, QMessageBox, QPixmap, QScrollArea, QSizePolicy,
    QComboBox, QCheckBox, QPainter, QPen, QBrush, QColor, QProgressDialog,
    QApplication
)

from PIL import Image, ImageDraw, ImageFont
from PIL.ImageQt import ImageQt


PLUGIN_NAME = "Cover Generator"


def load_font(font_path, size):
    try:
        if font_path and os.path.exists(font_path):
            return ImageFont.truetype(font_path, size)
    except Exception:
        pass
    return ImageFont.load_default()


def fit_image_keep_ratio(img, target_w, target_h):
    if target_w <= 0 or target_h <= 0:
        return None
    im = img.copy()
    im.thumbnail((target_w, target_h), Image.LANCZOS)
    return im


def paste_rgba(base, overlay, x, y):
    if overlay is None:
        return
    if overlay.mode != "RGBA":
        overlay = overlay.convert("RGBA")
    base.alpha_composite(overlay, (int(x), int(y)))


def text_bbox(draw, text, font):
    if not text:
        return (0, 0, 0, 0)
    return draw.textbbox((0, 0), text, font=font)


def wrap_text_to_width(draw, text, font, max_width):
    text = (text or "").strip()
    if not text:
        return []

    bbox = text_bbox(draw, text, font)
    if (bbox[2] - bbox[0]) <= max_width:
        return [text]

    words = text.split()
    if not words:
        return [text]

    wrapped = []
    current = words[0]

    for word in words[1:]:
        trial = current + " " + word
        bbox = text_bbox(draw, trial, font)
        if (bbox[2] - bbox[0]) <= max_width:
            current = trial
        else:
            wrapped.append(current)
            current = word
    wrapped.append(current)

    final_lines = []
    for line in wrapped:
        bbox = text_bbox(draw, line, font)
        if (bbox[2] - bbox[0]) <= max_width:
            final_lines.append(line)
            continue

        chunk = ""
        for ch in line:
            trial = chunk + ch
            bbox = text_bbox(draw, trial, font)
            if chunk and (bbox[2] - bbox[0]) > max_width:
                final_lines.append(chunk)
                chunk = ch
            else:
                chunk = trial
        if chunk:
            final_lines.append(chunk)

    return final_lines


def measure_text_block(draw, text_specs, base_line_spacing, title_line_spacing, max_width=None):
    items = []
    total_height = 0
    max_width_seen = 0

    non_empty_specs = [(text, font, role) for text, font, role in text_specs if (text or '').strip()]
    for spec_index, (text, font, role) in enumerate(non_empty_specs):
        wrapped_lines = wrap_text_to_width(draw, text, font, max_width) if max_width else [text]
        for line_index, wrapped in enumerate(wrapped_lines):
            bbox = text_bbox(draw, wrapped, font)
            w = bbox[2] - bbox[0]
            h = bbox[3] - bbox[1]

            spacing_after = 0
            is_last_line_in_spec = line_index == len(wrapped_lines) - 1
            is_last_spec = spec_index == len(non_empty_specs) - 1
            if not is_last_line_in_spec:
                spacing_after = title_line_spacing if role == 'title' else base_line_spacing
            elif not is_last_spec:
                spacing_after = base_line_spacing

            items.append((wrapped, font, w, h, spacing_after, role))
            total_height += h + spacing_after
            max_width_seen = max(max_width_seen, w)

    return items, total_height, max_width_seen


def fit_text_block_proportionally(draw, text_specs, line_spacing, title_line_spacing, max_width, max_height, auto_shrink=True):
    if not text_specs:
        return [], 0, 0, [], 0, 0

    def build_fonts(scale):
        built_specs = []
        scaled_sizes = []
        for text, font_path, base_size, role in text_specs:
            scaled_size = max(5, int(round(base_size * scale)))
            scaled_sizes.append(scaled_size)
            built_specs.append((text, load_font(font_path, scaled_size), role))
        scaled_spacing = max(0, int(round(line_spacing * scale)))
        scaled_title_spacing = max(0, int(round(title_line_spacing * scale)))
        return built_specs, scaled_sizes, scaled_spacing, scaled_title_spacing

    built_specs, scaled_sizes, scaled_spacing, scaled_title_spacing = build_fonts(1.0)
    items, total_h, max_w = measure_text_block(draw, built_specs, scaled_spacing, scaled_title_spacing, max_width=max_width)
    if (not auto_shrink) or (total_h <= max_height and max_w <= max_width):
        return items, total_h, max_w, scaled_sizes, scaled_spacing, scaled_title_spacing

    lo, hi = 0.1, 1.0
    best = None
    for _ in range(16):
        mid = (lo + hi) / 2.0
        built_specs, scaled_sizes, scaled_spacing, scaled_title_spacing = build_fonts(mid)
        items, total_h, max_w = measure_text_block(draw, built_specs, scaled_spacing, scaled_title_spacing, max_width=max_width)
        if total_h <= max_height and max_w <= max_width:
            best = (items, total_h, max_w, scaled_sizes, scaled_spacing, scaled_title_spacing)
            lo = mid
        else:
            hi = mid

    if best is None:
        built_specs, scaled_sizes, scaled_spacing, scaled_title_spacing = build_fonts(lo)
        items, total_h, max_w = measure_text_block(draw, built_specs, scaled_spacing, scaled_title_spacing, max_width=max_width)
        return items, total_h, max_w, scaled_sizes, scaled_spacing, scaled_title_spacing

    return best


class PreviewLabel(QLabel):
    HANDLE_SIZE = 12

    def __init__(self, dialog):
        super().__init__()
        self.dialog = dialog
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(1, 1)
        self.setStyleSheet("background:#222; border:1px solid #444;")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.dragging = None
        self.resize_mode = None
        self.last_pos = None
        self.setMouseTracking(True)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if getattr(self.dialog, '_refreshing_preview', False):
            return
        self.dialog.refresh_preview()

    def paintEvent(self, event):
        super().paintEvent(event)
        rect = self.dialog.get_selected_preview_rect()
        if not rect:
            return

        x1, y1, x2, y2 = rect
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(QColor(255, 255, 255), 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(int(x1), int(y1), int(x2 - x1), int(y2 - y1))

        painter.setPen(QPen(QColor(255, 255, 255), 1))
        painter.setBrush(QBrush(QColor(255, 255, 255)))
        hs = self.HANDLE_SIZE
        for hx, hy in self.dialog.get_resize_handles(rect).values():
            painter.drawRect(int(hx - hs / 2), int(hy - hs / 2), hs, hs)

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        px = event.position().x()
        py = event.position().y()
        self.resize_mode = self.dialog.hit_test_resize_handle(px, py)
        if self.resize_mode:
            self.last_pos = event.position()
            return
        layer = self.dialog.hit_test_preview(px, py)
        self.dragging = layer
        self.last_pos = event.position()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.resize_mode and self.last_pos is not None:
            dx = event.position().x() - self.last_pos.x()
            dy = event.position().y() - self.last_pos.y()
            self.dialog.resize_preview_layer_from_corner(self.resize_mode, dx, dy)
            self.last_pos = event.position()
            return

        if self.dragging and self.last_pos is not None:
            dx = event.position().x() - self.last_pos.x()
            dy = event.position().y() - self.last_pos.y()
            self.dialog.drag_preview_layer(self.dragging, dx, dy)
            self.last_pos = event.position()
            return

        hover_mode = self.dialog.hit_test_resize_handle(event.position().x(), event.position().y())
        if hover_mode in ('nw', 'se'):
            self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        elif hover_mode in ('ne', 'sw'):
            self.setCursor(Qt.CursorShape.SizeBDiagCursor)
        elif self.dialog.hit_test_preview(event.position().x(), event.position().y()):
            self.setCursor(Qt.CursorShape.SizeAllCursor)
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self.dragging = None
        self.resize_mode = None
        self.last_pos = None
        self.setCursor(Qt.CursorShape.ArrowCursor)
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event):
        layer = self.dialog.selected_layer
        if layer:
            self.dialog.resize_preview_layer(layer, event.angleDelta().y())
            return
        super().wheelEvent(event)


class CoverGeneratorDialog(QDialog):
    def __init__(self, gui, book_data, parent=None):
        super().__init__(parent or gui)
        self.gui = gui
        self.book_data = book_data or {"title": "Title", "author": "Author", "series": ""}
        self.setWindowTitle("Cover Generator")
        self.resize(1280, 820)

        self.config_dir = os.path.join(os.path.expanduser("~"), ".calibre_cover_generator")
        self.templates_path = os.path.join(self.config_dir, "templates.json")
        self.fonts_dir = os.path.join(self.config_dir, "stored_fonts")

        os.makedirs(self.config_dir, exist_ok=True)
        os.makedirs(self.fonts_dir, exist_ok=True)

        self.preview_label = PreviewLabel(self)
        self.preview_scroll = None
        self.preview_scale = 1.0
        self.preview_offset_x = 0
        self.preview_offset_y = 0
        self.preview_top_rect = None
        self.preview_center_rect = None
        self.selected_layer = None

        self._build_ui()
        self._connect_signals()
        self.refresh_font_dropdowns()
        self.refresh_template_dropdown()
        self.refresh_preview()

    def _build_ui(self):
        root = QHBoxLayout(self)

        left_container = QWidget()
        left_layout = QVBoxLayout(left_container)

        layout_group = QGroupBox("Layout")
        layout_form = QFormLayout(layout_group)

        self.cover_width = QSpinBox()
        self.cover_width.setRange(100, 5000)
        self.cover_width.setValue(1264)

        self.cover_height = QSpinBox()
        self.cover_height.setRange(100, 5000)
        self.cover_height.setValue(1680)

        self.line_ratio = QDoubleSpinBox()
        self.line_ratio.setRange(0.05, 0.95)
        self.line_ratio.setSingleStep(0.01)
        self.line_ratio.setDecimals(3)
        self.line_ratio.setValue(0.60)

        self.line_side_margin = QSpinBox()
        self.line_side_margin.setRange(0, 2000)
        self.line_side_margin.setValue(0)

        self.line_thickness = QSpinBox()
        self.line_thickness.setRange(1, 500)
        self.line_thickness.setValue(40)

        layout_form.addRow("Cover width", self.cover_width)
        layout_form.addRow("Cover height", self.cover_height)
        layout_form.addRow("White line vertical ratio", self.line_ratio)
        layout_form.addRow("White line side margin", self.line_side_margin)
        layout_form.addRow("White line thickness", self.line_thickness)
        left_layout.addWidget(layout_group)

        text_group = QGroupBox("Text")
        text_form = QFormLayout(text_group)

        self.title_font_combo = QComboBox()
        self.series_font_combo = QComboBox()
        self.author_font_combo = QComboBox()

        self.title_font_path = QLineEdit()
        self.series_font_path = QLineEdit()
        self.author_font_path = QLineEdit()

        self.title_font_browse = QPushButton("Browse")
        self.series_font_browse = QPushButton("Browse")
        self.author_font_browse = QPushButton("Browse")

        self.title_font_store = QPushButton("Store")
        self.series_font_store = QPushButton("Store")
        self.author_font_store = QPushButton("Store")

        def make_font_row(combo, path, browse, store):
            row = QHBoxLayout()
            row.addWidget(combo, 2)
            row.addWidget(path, 3)
            row.addWidget(browse)
            row.addWidget(store)
            wrap = QWidget()
            wrap.setLayout(row)
            return wrap

        text_form.addRow("Title font", make_font_row(self.title_font_combo, self.title_font_path, self.title_font_browse, self.title_font_store))
        text_form.addRow("Series font", make_font_row(self.series_font_combo, self.series_font_path, self.series_font_browse, self.series_font_store))
        text_form.addRow("Author font", make_font_row(self.author_font_combo, self.author_font_path, self.author_font_browse, self.author_font_store))

        self.title_size = QSpinBox()
        self.title_size.setRange(5, 500)
        self.title_size.setValue(98)

        self.series_size = QSpinBox()
        self.series_size.setRange(5, 500)
        self.series_size.setValue(63)

        self.author_size = QSpinBox()
        self.author_size.setRange(5, 500)
        self.author_size.setValue(63)

        self.line_spacing = QSpinBox()
        self.line_spacing.setRange(0, 500)
        self.line_spacing.setValue(60)

        self.title_line_spacing = QSpinBox()
        self.title_line_spacing.setRange(0, 500)
        self.title_line_spacing.setValue(60)

        self.lower_top_padding = QSpinBox()
        self.lower_top_padding.setRange(0, 1000)
        self.lower_top_padding.setValue(40)

        self.lower_bottom_padding = QSpinBox()
        self.lower_bottom_padding.setRange(0, 1000)
        self.lower_bottom_padding.setValue(40)

        self.lower_side_padding = QSpinBox()
        self.lower_side_padding.setRange(0, 1000)
        self.lower_side_padding.setValue(80)

        text_form.addRow("Title font size", self.title_size)
        text_form.addRow("Series font size", self.series_size)
        text_form.addRow("Author font size", self.author_size)

        self.author_all_caps = QCheckBox("Convert author to ALL CAPS")
        self.lower_auto_shrink = QCheckBox("Auto-shrink lower text to fit padding")
        self.lower_auto_shrink.setChecked(True)
        text_form.addRow("Author text", self.author_all_caps)
        text_form.addRow("Lower text fit", self.lower_auto_shrink)

        text_form.addRow("Line spacing", self.line_spacing)
        text_form.addRow("Title line spacing", self.title_line_spacing)
        text_form.addRow("Lower top padding", self.lower_top_padding)
        text_form.addRow("Lower bottom padding", self.lower_bottom_padding)
        text_form.addRow("Lower left/right padding", self.lower_side_padding)
        left_layout.addWidget(text_group)

        top_group = QGroupBox("Top image / symbol")
        top_form = QFormLayout(top_group)

        self.top_image_path = QLineEdit()
        self.top_image_browse = QPushButton("Browse")
        top_path_row = QHBoxLayout()
        top_path_row.addWidget(self.top_image_path)
        top_path_row.addWidget(self.top_image_browse)
        top_path_wrap = QWidget()
        top_path_wrap.setLayout(top_path_row)

        self.top_w_ratio = QDoubleSpinBox()
        self.top_w_ratio.setRange(0.01, 2.0)
        self.top_w_ratio.setSingleStep(0.01)
        self.top_w_ratio.setDecimals(3)
        self.top_w_ratio.setValue(0.28)

        self.top_h_ratio = QDoubleSpinBox()
        self.top_h_ratio.setRange(0.01, 2.0)
        self.top_h_ratio.setSingleStep(0.01)
        self.top_h_ratio.setDecimals(3)
        self.top_h_ratio.setValue(0.20)

        self.top_x_offset = QSpinBox()
        self.top_x_offset.setRange(-5000, 5000)
        self.top_x_offset.setValue(0)

        self.top_y_offset = QSpinBox()
        self.top_y_offset.setRange(-5000, 5000)
        self.top_y_offset.setValue(0)

        top_form.addRow("Image path", top_path_wrap)
        top_form.addRow("Image width ratio", self.top_w_ratio)
        top_form.addRow("Image height ratio", self.top_h_ratio)
        top_form.addRow("Image X offset", self.top_x_offset)
        top_form.addRow("Image Y offset", self.top_y_offset)
        left_layout.addWidget(top_group)

        center_group = QGroupBox("Center symbol on white line")
        center_form = QFormLayout(center_group)

        self.center_image_path = QLineEdit()
        self.center_image_browse = QPushButton("Browse")
        center_path_row = QHBoxLayout()
        center_path_row.addWidget(self.center_image_path)
        center_path_row.addWidget(self.center_image_browse)
        center_path_wrap = QWidget()
        center_path_wrap.setLayout(center_path_row)

        self.center_auto_middle = QCheckBox("Auto-center on white line")
        self.center_auto_middle.setChecked(True)

        self.center_w_ratio = QDoubleSpinBox()
        self.center_w_ratio.setRange(0.01, 2.0)
        self.center_w_ratio.setSingleStep(0.01)
        self.center_w_ratio.setDecimals(3)
        self.center_w_ratio.setValue(0.18)

        self.center_h_ratio = QDoubleSpinBox()
        self.center_h_ratio.setRange(0.01, 2.0)
        self.center_h_ratio.setSingleStep(0.01)
        self.center_h_ratio.setDecimals(3)
        self.center_h_ratio.setValue(0.10)

        self.center_x_offset = QSpinBox()
        self.center_x_offset.setRange(-5000, 5000)
        self.center_x_offset.setValue(0)

        self.center_y_offset = QSpinBox()
        self.center_y_offset.setRange(-5000, 5000)
        self.center_y_offset.setValue(0)

        center_form.addRow("Symbol path", center_path_wrap)
        center_form.addRow("", self.center_auto_middle)
        center_form.addRow("Symbol width ratio", self.center_w_ratio)
        center_form.addRow("Symbol height ratio", self.center_h_ratio)
        center_form.addRow("Symbol X offset", self.center_x_offset)
        center_form.addRow("Symbol Y offset", self.center_y_offset)
        left_layout.addWidget(center_group)

        template_group = QGroupBox("Templates")
        template_layout = QVBoxLayout(template_group)

        self.template_combo = QComboBox()
        self.template_combo.setEditable(False)

        self.template_name = QLineEdit()
        self.template_name.setPlaceholderText("Template name")

        template_btn_row1 = QHBoxLayout()
        self.save_template_btn = QPushButton("Save current")
        self.load_template_btn = QPushButton("Load selected")
        template_btn_row1.addWidget(self.save_template_btn)
        template_btn_row1.addWidget(self.load_template_btn)

        template_btn_row2 = QHBoxLayout()
        self.delete_template_btn = QPushButton("Delete selected")
        self.overwrite_template_btn = QPushButton("Overwrite selected")
        template_btn_row2.addWidget(self.delete_template_btn)
        template_btn_row2.addWidget(self.overwrite_template_btn)

        template_layout.addWidget(QLabel("Saved templates"))
        template_layout.addWidget(self.template_combo)
        template_layout.addWidget(self.template_name)
        template_layout.addLayout(template_btn_row1)
        template_layout.addLayout(template_btn_row2)
        left_layout.addWidget(template_group)

        note = QLabel(
            "Left settings panel scrolls.\n"
            "Drag images in the preview.\n"
            "Use mouse wheel to resize the selected image.\n"
            "Enable auto-center to snap the white-line symbol to the exact middle."
        )
        note.setWordWrap(True)
        left_layout.addWidget(note)
        left_layout.addStretch(1)

        right_widget = QWidget()
        right_widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(6)

        right_layout.addWidget(QLabel("Preview"))

        scroll = QScrollArea()
        self.preview_scroll = scroll
        scroll.setWidgetResizable(True)
        preview_wrap = QWidget()
        preview_wrap_layout = QVBoxLayout(preview_wrap)
        preview_wrap_layout.setContentsMargins(0, 0, 0, 0)
        preview_wrap_layout.setSpacing(0)
        preview_wrap_layout.addWidget(self.preview_label, 0, Qt.AlignmentFlag.AlignCenter)
        scroll.setWidget(preview_wrap)
        right_layout.addWidget(scroll, 1)

        self.preview_book_label = QLabel()
        self.preview_book_label.setWordWrap(True)
        right_layout.addWidget(self.preview_book_label)

        buttons = QDialogButtonBox()
        self.ok_btn = buttons.addButton("OK", QDialogButtonBox.ButtonRole.AcceptRole)
        self.refresh_btn = buttons.addButton("Refresh preview", QDialogButtonBox.ButtonRole.ActionRole)
        self.cancel_btn = buttons.addButton("Cancel", QDialogButtonBox.ButtonRole.RejectRole)

        left_layout.addWidget(buttons)

        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        left_scroll.setWidget(left_container)
        left_scroll.setMinimumWidth(430)
        left_scroll.setMaximumWidth(560)
        left_scroll.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)

        root.addWidget(left_scroll, 0)
        root.addWidget(right_widget, 1)

        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

    def _connect_signals(self):
        self.title_font_browse.clicked.connect(lambda: self.browse_font_for(self.title_font_path))
        self.series_font_browse.clicked.connect(lambda: self.browse_font_for(self.series_font_path))
        self.author_font_browse.clicked.connect(lambda: self.browse_font_for(self.author_font_path))

        self.title_font_store.clicked.connect(lambda: self.store_font_from(self.title_font_path, "title"))
        self.series_font_store.clicked.connect(lambda: self.store_font_from(self.series_font_path, "series"))
        self.author_font_store.clicked.connect(lambda: self.store_font_from(self.author_font_path, "author"))

        self.title_font_combo.currentIndexChanged.connect(lambda: self.font_combo_changed(self.title_font_combo, self.title_font_path))
        self.series_font_combo.currentIndexChanged.connect(lambda: self.font_combo_changed(self.series_font_combo, self.series_font_path))
        self.author_font_combo.currentIndexChanged.connect(lambda: self.font_combo_changed(self.author_font_combo, self.author_font_path))

        self.top_image_browse.clicked.connect(self.browse_top_image)
        self.center_image_browse.clicked.connect(self.browse_center_image)

        self.refresh_btn.clicked.connect(self.refresh_preview)
        self.save_template_btn.clicked.connect(self.save_template)
        self.load_template_btn.clicked.connect(self.load_template)
        self.delete_template_btn.clicked.connect(self.delete_template)
        self.overwrite_template_btn.clicked.connect(self.overwrite_selected_template)
        self.template_combo.currentTextChanged.connect(self.template_combo_changed)

        self.center_auto_middle.toggled.connect(self.update_center_offset_enabled)

        widgets = [
            self.cover_width, self.cover_height, self.line_ratio, self.line_side_margin,
            self.line_thickness, self.title_size, self.series_size, self.author_size,
            self.line_spacing, self.title_line_spacing, self.lower_top_padding, self.lower_bottom_padding, self.lower_side_padding,
            self.top_w_ratio, self.top_h_ratio, self.top_x_offset, self.top_y_offset,
            self.center_w_ratio, self.center_h_ratio, self.center_x_offset, self.center_y_offset,
            self.title_font_path, self.series_font_path, self.author_font_path,
            self.top_image_path, self.center_image_path, self.center_auto_middle,
            self.author_all_caps, self.lower_auto_shrink
        ]

        for w in widgets:
            if hasattr(w, "valueChanged"):
                w.valueChanged.connect(self.refresh_preview)
            elif hasattr(w, "textChanged"):
                w.textChanged.connect(self.refresh_preview)
            elif hasattr(w, "toggled"):
                w.toggled.connect(self.refresh_preview)

        self.update_center_offset_enabled()

    def update_center_offset_enabled(self):
        enabled = not self.center_auto_middle.isChecked()
        self.center_x_offset.setEnabled(enabled)
        self.center_y_offset.setEnabled(enabled)

    def stored_font_files(self):
        files = []
        if os.path.exists(self.fonts_dir):
            for name in sorted(os.listdir(self.fonts_dir)):
                if name.lower().endswith((".ttf", ".otf")):
                    files.append(os.path.join(self.fonts_dir, name))
        return files

    def refresh_font_dropdowns(self):
        font_files = self.stored_font_files()
        combos = [self.title_font_combo, self.series_font_combo, self.author_font_combo]

        for combo in combos:
            current_data = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("(stored font)", "")
            for path in font_files:
                combo.addItem(os.path.basename(path), path)
            if current_data:
                idx = combo.findData(current_data)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
            combo.blockSignals(False)


    def refresh_template_dropdown(self, selected_name=None):
        all_templates = self.load_all_templates()
        names = sorted(all_templates.keys(), key=lambda x: x.lower())
        current_text = selected_name if selected_name is not None else self.template_combo.currentText()
        self.template_combo.blockSignals(True)
        self.template_combo.clear()
        self.template_combo.addItem("(select template)")
        for name in names:
            self.template_combo.addItem(name)
        if current_text:
            idx = self.template_combo.findText(current_text)
            if idx >= 0:
                self.template_combo.setCurrentIndex(idx)
        self.template_combo.blockSignals(False)

    def template_combo_changed(self, text):
        if text and text != "(select template)":
            self.template_name.setText(text)

    def font_combo_changed(self, combo, line_edit):
        path = combo.currentData()
        if path:
            line_edit.setText(path)

    def browse_font_for(self, line_edit):
        path, _ = QFileDialog.getOpenFileName(self, "Select font", "", "Fonts (*.ttf *.otf)")
        if path:
            line_edit.setText(path)

    def store_font_from(self, line_edit, role_name):
        src = line_edit.text().strip()
        if not src:
            QMessageBox.warning(self, "Store font", "Choose a font first.")
            return
        if not os.path.exists(src):
            QMessageBox.warning(self, "Store font", "Font file does not exist.")
            return

        ext = os.path.splitext(src)[1].lower()
        if ext not in (".ttf", ".otf"):
            QMessageBox.warning(self, "Store font", "Only .ttf and .otf are supported.")
            return

        base = os.path.splitext(os.path.basename(src))[0]
        dst_name = f"{role_name}_{base}_{uuid.uuid4().hex[:8]}{ext}"
        dst = os.path.join(self.fonts_dir, dst_name)

        try:
            shutil.copy2(src, dst)
            line_edit.setText(dst)
            self.refresh_font_dropdowns()
            QMessageBox.information(self, "Store font", f"Stored font:\n{dst}")
        except Exception as e:
            QMessageBox.warning(self, "Store font", f"Could not store font:\n{e}")

    def browse_top_image(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select top image", "", "Images (*.png *.jpg *.jpeg *.webp *.bmp)")
        if path:
            self.top_image_path.setText(path)

    def browse_center_image(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select center symbol", "", "Images (*.png *.jpg *.jpeg *.webp *.bmp)")
        if path:
            self.center_image_path.setText(path)

    def get_settings(self):
        return {
            "cover_width": self.cover_width.value(),
            "cover_height": self.cover_height.value(),
            "line_ratio": self.line_ratio.value(),
            "line_side_margin": self.line_side_margin.value(),
            "line_thickness": self.line_thickness.value(),

            "title_font_path": self.title_font_path.text().strip(),
            "series_font_path": self.series_font_path.text().strip(),
            "author_font_path": self.author_font_path.text().strip(),

            "title_size": self.title_size.value(),
            "series_size": self.series_size.value(),
            "author_size": self.author_size.value(),
            "author_all_caps": self.author_all_caps.isChecked(),
            "lower_auto_shrink": self.lower_auto_shrink.isChecked(),
            "line_spacing": self.line_spacing.value(),
            "title_line_spacing": self.title_line_spacing.value(),
            "lower_top_padding": self.lower_top_padding.value(),
            "lower_bottom_padding": self.lower_bottom_padding.value(),
            "lower_side_padding": self.lower_side_padding.value(),

            "top_image_path": self.top_image_path.text().strip(),
            "top_w_ratio": self.top_w_ratio.value(),
            "top_h_ratio": self.top_h_ratio.value(),
            "top_x_offset": self.top_x_offset.value(),
            "top_y_offset": self.top_y_offset.value(),

            "center_image_path": self.center_image_path.text().strip(),
            "center_auto_middle": self.center_auto_middle.isChecked(),
            "center_w_ratio": self.center_w_ratio.value(),
            "center_h_ratio": self.center_h_ratio.value(),
            "center_x_offset": self.center_x_offset.value(),
            "center_y_offset": self.center_y_offset.value(),
        }

    def apply_settings(self, s):
        self.cover_width.setValue(s.get("cover_width", self.cover_width.value()))
        self.cover_height.setValue(s.get("cover_height", self.cover_height.value()))
        self.line_ratio.setValue(s.get("line_ratio", self.line_ratio.value()))
        self.line_side_margin.setValue(s.get("line_side_margin", self.line_side_margin.value()))
        self.line_thickness.setValue(s.get("line_thickness", self.line_thickness.value()))

        self.title_font_path.setText(s.get("title_font_path", self.title_font_path.text()))
        self.series_font_path.setText(s.get("series_font_path", self.series_font_path.text()))
        self.author_font_path.setText(s.get("author_font_path", self.author_font_path.text()))

        self.title_size.setValue(s.get("title_size", self.title_size.value()))
        self.series_size.setValue(s.get("series_size", self.series_size.value()))
        self.author_size.setValue(s.get("author_size", self.author_size.value()))
        self.author_all_caps.setChecked(s.get("author_all_caps", self.author_all_caps.isChecked()))
        self.lower_auto_shrink.setChecked(s.get("lower_auto_shrink", self.lower_auto_shrink.isChecked()))
        self.line_spacing.setValue(s.get("line_spacing", self.line_spacing.value()))
        self.title_line_spacing.setValue(s.get("title_line_spacing", s.get("line_spacing", self.title_line_spacing.value())))
        self.lower_top_padding.setValue(s.get("lower_top_padding", self.lower_top_padding.value()))
        self.lower_bottom_padding.setValue(s.get("lower_bottom_padding", self.lower_bottom_padding.value()))
        self.lower_side_padding.setValue(s.get("lower_side_padding", self.lower_side_padding.value()))

        self.top_image_path.setText(s.get("top_image_path", self.top_image_path.text()))
        self.top_w_ratio.setValue(s.get("top_w_ratio", self.top_w_ratio.value()))
        self.top_h_ratio.setValue(s.get("top_h_ratio", self.top_h_ratio.value()))
        self.top_x_offset.setValue(s.get("top_x_offset", self.top_x_offset.value()))
        self.top_y_offset.setValue(s.get("top_y_offset", self.top_y_offset.value()))

        self.center_image_path.setText(s.get("center_image_path", self.center_image_path.text()))
        self.center_auto_middle.setChecked(s.get("center_auto_middle", self.center_auto_middle.isChecked()))
        self.center_w_ratio.setValue(s.get("center_w_ratio", self.center_w_ratio.value()))
        self.center_h_ratio.setValue(s.get("center_h_ratio", self.center_h_ratio.value()))
        self.center_x_offset.setValue(s.get("center_x_offset", self.center_x_offset.value()))
        self.center_y_offset.setValue(s.get("center_y_offset", self.center_y_offset.value()))
        self.update_center_offset_enabled()

    def load_all_templates(self):
        if not os.path.exists(self.templates_path):
            return {}
        try:
            with open(self.templates_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def save_all_templates(self, data):
        with open(self.templates_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def selected_template_name(self):
        text = self.template_combo.currentText().strip()
        if text and text != "(select template)":
            return text
        return ""

    def save_template(self):
        name = self.template_name.text().strip()
        if not name:
            QMessageBox.warning(self, "Template", "Enter a template name first.")
            return
        all_templates = self.load_all_templates()
        if name in all_templates:
            answer = QMessageBox.question(
                self,
                "Overwrite template",
                f"Template '{name}' already exists. Overwrite it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        all_templates[name] = self.get_settings()
        self.save_all_templates(all_templates)
        self.refresh_template_dropdown(selected_name=name)
        self.template_name.setText(name)
        QMessageBox.information(self, "Template", f"Saved template: {name}")

    def overwrite_selected_template(self):
        name = self.selected_template_name()
        if not name:
            QMessageBox.warning(self, "Template", "Select a template to overwrite.")
            return
        all_templates = self.load_all_templates()
        all_templates[name] = self.get_settings()
        self.save_all_templates(all_templates)
        self.refresh_template_dropdown(selected_name=name)
        self.template_name.setText(name)
        QMessageBox.information(self, "Template", f"Overwrote template: {name}")

    def load_template(self):
        name = self.selected_template_name() or self.template_name.text().strip()
        if not name:
            QMessageBox.warning(self, "Template", "Select or enter a template name first.")
            return
        all_templates = self.load_all_templates()
        if name not in all_templates:
            QMessageBox.warning(self, "Template", f"Template not found: {name}")
            return
        self.apply_settings(all_templates[name])
        self.refresh_template_dropdown(selected_name=name)
        self.template_name.setText(name)
        self.refresh_preview()

    def delete_template(self):
        name = self.selected_template_name() or self.template_name.text().strip()
        if not name:
            QMessageBox.warning(self, "Template", "Select a template to delete.")
            return
        all_templates = self.load_all_templates()
        if name not in all_templates:
            QMessageBox.warning(self, "Template", f"Template not found: {name}")
            return
        answer = QMessageBox.question(
            self,
            "Delete template",
            f"Delete template '{name}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        del all_templates[name]
        self.save_all_templates(all_templates)
        self.refresh_template_dropdown()
        self.template_name.clear()
        QMessageBox.information(self, "Template", f"Deleted template: {name}")

    def render_cover(self, book_data, include_rects=False):
        s = self.get_settings()

        width = s["cover_width"]
        height = s["cover_height"]
        line_y = int(height * s["line_ratio"])
        line_thickness = s["line_thickness"]
        side_margin = s["line_side_margin"]

        img = Image.new("RGBA", (width, height), (0, 0, 0, 255))
        draw = ImageDraw.Draw(img)

        line_top = line_y - line_thickness // 2
        line_bottom = line_top + line_thickness
        draw.rectangle([side_margin, line_top, width - side_margin, line_bottom], fill=(255, 255, 255, 255))

        top_rect = None
        top_path = s["top_image_path"]
        if top_path and os.path.exists(top_path):
            try:
                top_img = Image.open(top_path).convert("RGBA")
                target_w = max(1, int(width * s["top_w_ratio"]))
                target_h = max(1, int(height * s["top_h_ratio"]))
                fitted = fit_image_keep_ratio(top_img, target_w, target_h)
                if fitted:
                    x = (width - fitted.width) // 2 + s["top_x_offset"]
                    upper_area_h = max(1, line_top)
                    y = (upper_area_h - fitted.height) // 2 + s["top_y_offset"]
                    paste_rgba(img, fitted, x, y)
                    top_rect = (x, y, x + fitted.width, y + fitted.height)
            except Exception:
                pass

        center_rect = None
        center_path = s["center_image_path"]
        if center_path and os.path.exists(center_path):
            try:
                center_img = Image.open(center_path).convert("RGBA")
                target_w = max(1, int(width * s["center_w_ratio"]))
                target_h = max(1, int(height * s["center_h_ratio"]))
                fitted = fit_image_keep_ratio(center_img, target_w, target_h)
                if fitted:
                    if s["center_auto_middle"]:
                        x = (width - fitted.width) // 2
                        line_center_y = (line_top + line_bottom) // 2
                        y = line_center_y - fitted.height // 2
                    else:
                        x = (width - fitted.width) // 2 + s["center_x_offset"]
                        line_center_y = (line_top + line_bottom) // 2
                        y = line_center_y - fitted.height // 2 + s["center_y_offset"]

                    paste_rgba(img, fitted, x, y)
                    center_rect = (x, y, x + fitted.width, y + fitted.height)
            except Exception:
                pass

        title = (book_data.get("title") or "").strip()
        author = (book_data.get("author") or "").strip()
        if s.get("author_all_caps"):
            author = author.upper()
        series = (book_data.get("series") or "").strip()

        text_specs = []

        if title:
            text_specs.append((title, s["title_font_path"], s["title_size"], "title"))
        if series:
            text_specs.append((series, s["series_font_path"], s["series_size"], "series"))
        if author:
            text_specs.append((author, s["author_font_path"], s["author_size"], "author"))

        usable_top = line_bottom + s["lower_top_padding"]
        usable_bottom = height - s["lower_bottom_padding"]
        usable_height = max(1, usable_bottom - usable_top)
        usable_left = s.get("lower_side_padding", 0)
        usable_right = width - s.get("lower_side_padding", 0)
        usable_width = max(1, usable_right - usable_left)

        items, total_h, _max_w, _scaled_sizes, actual_spacing, actual_title_spacing = fit_text_block_proportionally(
            draw,
            text_specs,
            s["line_spacing"],
            s.get("title_line_spacing", s["line_spacing"]),
            usable_width,
            usable_height,
            auto_shrink=s.get("lower_auto_shrink", True),
        )

        start_y = usable_top + max(0, (usable_height - total_h) // 2)

        y = start_y
        for text, font, w, h, spacing_after, _role in items:
            x = usable_left + max(0, (usable_width - w) // 2)
            draw.text((x, y), text, font=font, fill=(255, 255, 255, 255))
            y += h + spacing_after

        if include_rects:
            return img, top_rect, center_rect
        return img

    def refresh_preview(self):
        img, top_rect, center_rect = self.render_cover(self.book_data, include_rects=True)

        title = self.book_data.get("title", "")
        author = self.book_data.get("author", "")
        series = self.book_data.get("series", "")

        self.preview_book_label.setText(
            f"Preview book:\nTitle: {title}\n"
            + (f"Series: {series}\n" if series else "")
            + f"Author: {author}"
        )

        qimage = ImageQt(img)
        pixmap = QPixmap.fromImage(qimage)

        viewport = self.preview_scroll.viewport() if self.preview_scroll is not None else None
        if viewport is not None:
            avail_w = max(1, viewport.width() - 2)
            avail_h = max(1, viewport.height() - 2)
        else:
            avail_w = max(1, self.preview_label.width() - 2)
            avail_h = max(1, self.preview_label.height() - 2)

        scaled = pixmap.scaled(
            avail_w,
            avail_h,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

        self._refreshing_preview = True
        try:
            self.preview_label.setFixedSize(avail_w, avail_h)
            self.preview_label.setPixmap(scaled)
        finally:
            self._refreshing_preview = False

        self.preview_scale = min(float(scaled.width()) / float(img.width), float(scaled.height()) / float(img.height))
        self.preview_offset_x = (self.preview_label.width() - scaled.width()) / 2.0
        self.preview_offset_y = (self.preview_label.height() - scaled.height()) / 2.0

        def scale_rect(rect):
            if not rect:
                return None
            x1, y1, x2, y2 = rect
            return (
                x1 * self.preview_scale + self.preview_offset_x,
                y1 * self.preview_scale + self.preview_offset_y,
                x2 * self.preview_scale + self.preview_offset_x,
                y2 * self.preview_scale + self.preview_offset_y,
            )

        self.preview_top_rect = scale_rect(top_rect)
        self.preview_center_rect = scale_rect(center_rect)
        self.preview_label.update()

    def get_selected_preview_rect(self):
        if self.selected_layer == "top":
            return self.preview_top_rect
        if self.selected_layer == "center":
            return self.preview_center_rect
        return None

    def get_resize_handles(self, rect):
        if not rect:
            return {}
        x1, y1, x2, y2 = rect
        return {
            "nw": (x1, y1),
            "ne": (x2, y1),
            "sw": (x1, y2),
            "se": (x2, y2),
        }

    def hit_test_resize_handle(self, px, py):
        rect = self.get_selected_preview_rect()
        if not rect:
            return None
        hs = self.preview_label.HANDLE_SIZE
        for name, (hx, hy) in self.get_resize_handles(rect).items():
            if abs(px - hx) <= hs and abs(py - hy) <= hs:
                return name
        return None

    def hit_test_preview(self, px, py):
        def inside(rect):
            if not rect:
                return False
            x1, y1, x2, y2 = rect
            return x1 <= px <= x2 and y1 <= py <= y2

        if inside(self.preview_center_rect):
            self.selected_layer = "center"
            self.preview_label.update()
            return "center"
        if inside(self.preview_top_rect):
            self.selected_layer = "top"
            self.preview_label.update()
            return "top"
        return None

    def drag_preview_layer(self, layer, dx, dy):
        if self.preview_scale == 0:
            return
        real_dx = int(dx / self.preview_scale)
        real_dy = int(dy / self.preview_scale)

        if layer == "top":
            self.top_x_offset.setValue(self.top_x_offset.value() + real_dx)
            self.top_y_offset.setValue(self.top_y_offset.value() + real_dy)
        elif layer == "center":
            if self.center_auto_middle.isChecked():
                return
            self.center_x_offset.setValue(self.center_x_offset.value() + real_dx)
            self.center_y_offset.setValue(self.center_y_offset.value() + real_dy)

        self.refresh_preview()

    def resize_preview_layer_from_corner(self, corner, dx, dy):
        layer = self.selected_layer
        if not layer or self.preview_scale == 0:
            return

        rect = self.get_selected_preview_rect()
        if not rect:
            return

        x1, y1, x2, y2 = rect
        width_px = max(1.0, x2 - x1)
        height_px = max(1.0, y2 - y1)

        sign_x = 1 if corner in ("se", "ne") else -1
        sign_y = 1 if corner in ("se", "sw") else -1
        delta_px = max(sign_x * dx, sign_y * dy)
        ratio_step = delta_px / max(width_px, height_px) * 0.25
        if abs(ratio_step) < 0.001:
            return

        if layer == "top":
            new_w = max(0.01, self.top_w_ratio.value() + ratio_step)
            new_h = max(0.01, self.top_h_ratio.value() + ratio_step)
            self.top_w_ratio.setValue(new_w)
            self.top_h_ratio.setValue(new_h)
        elif layer == "center":
            new_w = max(0.01, self.center_w_ratio.value() + ratio_step)
            new_h = max(0.01, self.center_h_ratio.value() + ratio_step)
            self.center_w_ratio.setValue(new_w)
            self.center_h_ratio.setValue(new_h)

        self.refresh_preview()

    def resize_preview_layer(self, layer, delta):
        step = 0.01 if delta > 0 else -0.01

        if layer == "top":
            self.top_w_ratio.setValue(max(0.01, self.top_w_ratio.value() + step))
            self.top_h_ratio.setValue(max(0.01, self.top_h_ratio.value() + step))
        elif layer == "center":
            self.center_w_ratio.setValue(max(0.01, self.center_w_ratio.value() + step))
            self.center_h_ratio.setValue(max(0.01, self.center_h_ratio.value() + step))

        self.refresh_preview()


class CoverGeneratorUI(InterfaceAction):
    name = PLUGIN_NAME
    action_spec = ("Cover Generator", None, "Generate custom covers", None)

    def genesis(self):
        self.qaction.triggered.connect(self.show_dialog)

    def get_selected_book_ids(self):
        try:
            return list(self.gui.library_view.get_selected_ids())
        except Exception:
            return []

    def get_book_data(self, db, book_id):
        mi = db.get_metadata(book_id, get_cover=False)
        title = mi.title or ""
        author = ", ".join(mi.authors) if mi.authors else ""
        series = mi.series or ""
        return {"title": title, "author": author, "series": series}

    def pil_to_jpeg_bytes(self, img):
        rgb = img.convert("RGB")
        bio = io.BytesIO()
        rgb.save(bio, format="JPEG", quality=95)
        return bio.getvalue()

    def show_dialog(self):
        db = self.gui.current_db.new_api
        selected_ids = self.get_selected_book_ids()

        if not selected_ids:
            error_dialog(self.gui, "Cover Generator", "No books selected.", show=True)
            return

        preview_book = self.get_book_data(db, selected_ids[0])
        dlg = CoverGeneratorDialog(self.gui, preview_book, self.gui)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        ok_count = 0
        errors = []
        canceled = False
        total = len(selected_ids)

        progress = QProgressDialog("Generating covers...", "Cancel", 0, total, self.gui)
        progress.setWindowTitle("Cover Generator")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.setValue(0)
        progress.show()
        QApplication.processEvents()

        for index, book_id in enumerate(selected_ids, start=1):
            if progress.wasCanceled():
                canceled = True
                break

            try:
                book_data = self.get_book_data(db, book_id)
                label_title = (book_data.get("title") or "Untitled").strip() or "Untitled"
                progress.setLabelText(f"Generating covers...\n{index}/{total}\n{label_title}")
                progress.setValue(index - 1)
                QApplication.processEvents()

                if progress.wasCanceled():
                    canceled = True
                    break

                img = dlg.render_cover(book_data)
                cover_bytes = self.pil_to_jpeg_bytes(img)
                db.set_cover({book_id: cover_bytes})
                ok_count += 1
            except Exception as e:
                errors.append(f"Book ID {book_id}: {e}")
            finally:
                progress.setValue(index)
                QApplication.processEvents()

        progress.close()

        self.gui.library_view.model().refresh()

        if canceled:
            if errors:
                msg = (
                    f"Canceled after generating covers for {ok_count} of {total} book(s).\n\n"
                    f"Some errors occurred:\n\n" + "\n".join(errors[:20])
                )
                error_dialog(self.gui, "Cover Generator", msg, show=True)
            else:
                info_dialog(self.gui, "Cover Generator", f"Canceled after generating covers for {ok_count} of {total} book(s).", show=True)
        elif errors:
            msg = f"Generated covers for {ok_count} book(s).\n\nSome errors occurred:\n\n" + "\n".join(errors[:20])
            error_dialog(self.gui, "Cover Generator", msg, show=True)
        else:
            info_dialog(self.gui, "Cover Generator", f"Generated covers for {ok_count} book(s).", show=True)

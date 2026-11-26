"""The native Kindlegarten desktop application."""

import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from threading import Event, Lock

from PySide6.QtCore import (
    QCollator, QEvent, QFileSystemWatcher, QPersistentModelIndex, QRect, QRectF, QPointF, QLocale,
    QObject, QRunnable, QSettings, QSize, QStandardPaths, Qt, QThreadPool, QTimer, QUrl, Signal,
)
from PySide6.QtGui import QActionGroup, QColor, QDesktopServices, QIcon, QPainter, QPainterPath, QPalette, QPen, QPixmap, QPolygonF, QTextLayout
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QListView, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
    QStackedWidget, QStyle, QStyledItemDelegate, QTextBrowser, QVBoxLayout, QWidget,
)

from kindlegarten import __version__, cli
from kindlegarten.browser import open_amazon_login
from kindlegarten.catalog_art import cached_cover, cover
from kindlegarten.comipo import COMIPO, DLSITE, import_session, login, saved_account, session_path
from kindlegarten.desktop_download import DownloadRequest, run_download
from kindlegarten.formats import FORMATS
from kindlegarten.i18n import LANGUAGE, SUPPORTED, tr
from kindlegarten.library import Book, Series, cache_library, cached_library, series_for_book, store_page, task_entries
from kindlegarten.local_downloads import folder_stamps, scan_downloads
from kindlegarten.marketplaces import MARKETPLACES
from kindlegarten.network import proxy_address, using_proxy
from kindlegarten.progress import PROGRESS, STATUS, DownloadCancelled
from kindlegarten.play_library import missing_legacy_books, volumes
from kindlegarten.update_install import (
    READY_VARIABLE, RESULT_VARIABLE, ManualInstall, can_install, installed_package, stage_update, start_installer,
)
from kindlegarten.updates import REPOSITORY, check_for_update, download_update

ROLE = Qt.ItemDataRole.UserRole
CAPTION_ROLE = ROLE + 1
DOWNLOADED_ROLE = ROLE + 2
COVER_LOADED_ROLE = ROLE + 3
APP_ICON = Path(__file__).with_name("app-icon.png")
PROVIDERS = {"amazon": "Kindle", "dlsite": "DLsite"}
# Older builds saved a single index for both the provider and the Kindle store.
LEGACY_PROVIDERS = (("amazon", "jp"), ("amazon", "us"), ("comipo", "jp"), ("dlsite", "jp"))
SORT_ORDERS = {
    "newest": "Purchase date · newest first",
    "oldest": "Purchase date · oldest first",
    "title_asc": "Title · A–Z",
    "title_desc": "Title · Z–A",
}
GUIDE = (
    ("Sign in", "Open the provider’s browser and complete sign-in. If needed, choose Paste credentials."),
    ("Paste credentials", "Amazon uses the final redirect URL. DLsite accepts the Cookie request header from Play or a session JSON file."),
    ("Your bookshelf", "Choose a source. For Kindle, choose your store. Open a series to select individual volumes, or select the whole series."),
    ("Export", "Choose a folder and a format. Auto picks EPUB for text and CBZ for comics. ZIP holds the same comic page archive as CBZ."),
    ("Downloads", "Use Download selected or add one ID per line with the plus button in Downloads. The top-right download icon toggles the panel. A check mark means the book or series is downloaded. A number shows how many volumes are saved in the series folder."),
    ("Calibre and Kindle DRM", "Set the Calibre path in Settings or leave it blank for automatic detection. Turn off Remove Kindle DRM to save an original KFX archive without conversion."),
    ("Proxy", "Open Settings to choose system, direct, or custom routing. Kindle exports require Calibre."),
    ("Command line", "Run kindlegarten help for commands, or kindlegarten library to list purchased books."),
)


def open_link(url):
    QDesktopServices.openUrl(QUrl(url))


class JobSignals(QObject):
    result = Signal(object)
    failed = Signal(str)
    progress = Signal(int, str)
    status = Signal(str)
    cancelled = Signal()
    finished = Signal()


class Job(QRunnable):
    def __init__(self, action, proxy, locale):
        super().__init__()
        self.action = action
        self.proxy = proxy
        self.locale = locale
        self.signals = JobSignals()

    def run(self):
        token = LANGUAGE.set(self.locale)
        progress = PROGRESS.set(self.signals.progress.emit)
        status = STATUS.set(self.signals.status.emit)
        try:
            with using_proxy(self.proxy):
                self.signals.result.emit(self.action())
        except DownloadCancelled:
            self.signals.cancelled.emit()
        except cli.KindleError as error:
            self.signals.failed.emit(str(error))
        except Exception:
            self.signals.failed.emit(tr("The operation failed. Check the connection, account, or destination."))
        finally:
            PROGRESS.reset(progress)
            STATUS.reset(status)
            LANGUAGE.reset(token)
            self.signals.finished.emit()


def button(text, action=None, *, primary=False):
    widget = QPushButton(tr(text))
    if action is not None:
        widget.clicked.connect(action)
    if primary:
        widget.setProperty("primary", True)
    return widget


GLYPHS = {
    "back": (((11, 5), (4, 12), (11, 19)), ((4, 12), (20, 12))),
    "download": (((12, 3), (12, 15)), ((7, 10), (12, 15), (17, 10)), ((4, 17), (4, 20), (20, 20), (20, 17))),
    "add": (((5, 12), (19, 12)), ((12, 5), (12, 19))),
    "external": (((13, 4), (20, 4), (20, 11)), ((20, 4), (11, 13)),
                 ((10, 5), (5, 5), (5, 19), (19, 19), (19, 14))),
    "check": (((5, 12), (10, 17), (19, 7)),),
    "folder": (((3, 19), (3, 5), (9, 5), (12, 8), (21, 8), (21, 19), (3, 19)),),
    "open-folder": (((3, 19), (3, 5), (9, 5), (12, 8), (20, 8), (20, 11)),
                    ((3, 19), (7, 11), (22, 11), (19, 19), (3, 19))),
    "filter": (((3, 5), (21, 5), (14, 13), (14, 18), (10, 20), (10, 13), (3, 5)),),
    "cancel": (((6, 6), (18, 18)), ((18, 6), (6, 18))),
    "chevron": (((6, 9), (12, 15), (18, 9)),),
    "user": (),
    "refresh": (((20, 3), (20, 9), (14, 9)), ((4, 21), (4, 15), (10, 15))),
    "settings": (((10, 2), (14, 2), (15, 5), (17, 6), (20, 5), (22, 9), (20, 11),
                  (20, 13), (22, 15), (20, 19), (17, 18), (15, 19), (14, 22), (10, 22),
                  (9, 19), (7, 18), (4, 19), (2, 15), (4, 13), (4, 11), (2, 9), (4, 5),
                  (7, 6), (9, 5), (10, 2)),),
}


def paint_glyph(painter, name, color):
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor(color), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    for line in GLYPHS[name]:
        painter.drawPolyline(QPolygonF([QPointF(*point) for point in line]))
    if name == "refresh":
        path = QPainterPath(QPointF(4.1, 9))
        path.cubicTo(6.5, 2.2, 15.7, 2.1, 20, 9)
        path.moveTo(19.9, 15)
        path.cubicTo(17.5, 21.8, 8.3, 21.9, 4, 15)
        painter.drawPath(path)
    if name == "settings":
        painter.drawEllipse(QRectF(9, 9, 6, 6))
    if name == "user":
        painter.drawEllipse(QRectF(8, 3, 8, 8))
        path = QPainterPath(QPointF(5, 21))
        path.lineTo(5, 19)
        path.cubicTo(5, 13, 19, 13, 19, 19)
        path.lineTo(19, 21)
        painter.drawPath(path)


def action_icon(name, color):
    pixmap = QPixmap(72, 72)
    pixmap.setDevicePixelRatio(3)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    paint_glyph(painter, name, color)
    painter.end()
    return QIcon(pixmap)


def paint_spinner(painter, bounds, angle):
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor("white"), 2.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    painter.drawArc(bounds, angle * 16, 245 * 16)


def icon_button(text, name, action):
    widget = button(text, action)
    widget.setText("")
    widget.setToolTip(tr(text))
    widget.setAccessibleName(tr(text))
    widget.setProperty("glyph", name)
    widget.setProperty("iconOnly", True)
    widget.setIconSize(QSize(19, 19))
    widget.setFixedSize(36, 36)
    return widget


class MenuField(QComboBox):
    def __init__(self):
        super().__init__()
        self.setView(QListView(self))

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        x = self.width() - 23 if self.layoutDirection() == Qt.LayoutDirection.LeftToRight else 9
        painter.translate(x, (self.height() - 14) / 2)
        painter.scale(14 / 24, 14 / 24)
        paint_glyph(painter, "chevron", self.palette().color(QPalette.ColorRole.ButtonText))
        painter.end()


def heading(text, role):
    label = QLabel(tr(text))
    label.setObjectName(role)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


class BookshelfGrid(QListWidget):
    action_requested = Signal(QListWidgetItem)
    menu_requested = Signal(QListWidgetItem, object)
    action_radius = 28

    def __init__(self):
        super().__init__()
        self.setMouseTracking(True)
        self.hovered = QPersistentModelIndex()
        self.hot = QPersistentModelIndex()
        self.pressed = QPersistentModelIndex()
        self.pointer = None
        self.pending = set()
        self.spinner_angle = 0

    def action_at(self, index, position):
        entry = index.data(ROLE)
        if entry is None or entry.id in self.pending:
            return False
        delta = position - BookTile.cover_rect(self.visualRect(index), entry).center()
        return delta.x() ** 2 + delta.y() ** 2 <= self.action_radius ** 2

    def refresh_pointer(self):
        index = self.indexAt(self.pointer) if self.pointer is not None else None
        hovered = QPersistentModelIndex(index) if index is not None else QPersistentModelIndex()
        hot = hovered if index is not None and self.action_at(index, self.pointer) else QPersistentModelIndex()
        if self.hovered != hovered or self.hot != hot:
            self.hovered, self.hot = hovered, hot
            self.viewport().update()
        self.viewport().setCursor(Qt.CursorShape.PointingHandCursor if hot.isValid() else Qt.CursorShape.ArrowCursor)

    def clear_hover(self):
        self.pointer = None
        self.refresh_pointer()

    def mouseMoveEvent(self, event):
        self.pointer = event.position().toPoint()
        self.refresh_pointer()
        super().mouseMoveEvent(event)

    def viewportEvent(self, event):
        if event.type() == QEvent.Type.Leave:
            self.clear_hover()
        return super().viewportEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and not event.modifiers():
            position = event.position().toPoint()
            index = self.indexAt(position)
            if self.action_at(index, position):
                self.pressed = QPersistentModelIndex(index)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        position = event.position().toPoint()
        index = self.indexAt(position)
        activate = (event.button() == Qt.MouseButton.LeftButton and not event.modifiers()
                    and index.isValid() and self.pressed == index and self.action_at(index, position))
        self.pressed = QPersistentModelIndex()
        super().mouseReleaseEvent(event)
        if activate:
            self.action_requested.emit(self.itemFromIndex(index))

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and not event.modifiers():
            self.pressed = QPersistentModelIndex()
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event):
        if event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter} and self.currentItem() is not None:
            event.accept()
            self.action_requested.emit(self.currentItem())
        else:
            super().keyPressEvent(event)

    def contextMenuEvent(self, event):
        if event.reason() == event.Reason.Keyboard:
            item = self.currentItem()
            if item is not None:
                self.menu_requested.emit(item, self.viewport().mapToGlobal(self.visualItemRect(item).center()))
        elif item := self.itemAt(event.pos()):
            self.menu_requested.emit(item, event.globalPos())
        event.accept()


class BookTile(QStyledItemDelegate):
    @staticmethod
    def cover_rect(rect, entry):
        bounds = rect.adjusted(5, 4, -5, -4)
        image = QRect(bounds.center().x() - 62, bounds.top() + 8, 124, 168)
        if isinstance(entry, Series) and len(entry.books) > 1:
            return image.adjusted(-2, 5, -9, -3)
        return image

    def sizeHint(self, option, index):
        return self.parent().gridSize()

    def paint(self, painter, option, index):
        book = index.data(ROLE)
        icon = index.data(Qt.ItemDataRole.DecorationRole)
        if book is None or icon is None:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        bounds = option.rect.adjusted(5, 4, -5, -4)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        palette = option.palette
        if selected:
            accent = palette.color(QPalette.ColorRole.Highlight)
            fill = QColor(accent)
            fill.setAlpha(44)
            painter.setPen(QPen(accent, 1.25))
            painter.setBrush(fill)
            painter.drawRoundedRect(bounds, 12, 12)
        image = QRect(bounds.center().x() - 62, bounds.top() + 8, 124, 168)
        front = self.cover_rect(option.rect, book)
        if isinstance(book, Series) and len(book.books) > 1:
            sheet_colors = ("#93b7a5", "#b3cdc0")
            layers = min(len(book.books), len(sheet_colors) + 1)
            painter.setPen(QPen(palette.color(QPalette.ColorRole.Base), 1))
            for depth in range(layers - 1, 0, -1):
                sheet = front.translated(4 * depth, -2 * depth)
                painter.setBrush(QColor(sheet_colors[depth - 1]))
                painter.drawRoundedRect(QRectF(sheet), 4, 4)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(palette.color(QPalette.ColorRole.Base))
            painter.drawRoundedRect(QRectF(front), 4, 4)
        icon.paint(painter, front, Qt.AlignmentFlag.AlignCenter)
        pending = book.id in self.parent().pending
        if pending or self.parent().hovered == index:
            center = QPointF(front.center())
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(17, 40, 34, 205 if not pending and self.parent().hot == index else 170))
            radius = self.parent().action_radius
            painter.drawEllipse(center, radius, radius)
            if pending:
                paint_spinner(painter, QRectF(center.x() - 14, center.y() - 14, 28, 28), self.parent().spinner_angle)
            else:
                painter.save()
                painter.translate(center - QPointF(16, 16))
                painter.scale(32 / 24, 32 / 24)
                paint_glyph(painter, "open-folder" if isinstance(book, Series) else "download", "#ffffff")
                painter.restore()
        color = palette.color(QPalette.ColorRole.Text)
        painter.setPen(color)
        title = QTextLayout(book.title, option.font)
        title.beginLayout()
        offset = 0.0
        for _ in range(2):
            line = title.createLine()
            if not line.isValid():
                break
            line.setLineWidth(bounds.width() - 16)
            line.setPosition(QPointF(0, offset))
            offset += line.height()
        title.endLayout()
        baseline = image.bottom() + 8
        title.draw(painter, QPointF(bounds.left() + 8, baseline))
        if isinstance(book, Series):
            caption = index.data(CAPTION_ROLE)
            color.setAlpha(175)
            painter.setPen(color)
            painter.drawText(QRect(bounds.left() + 8, round(baseline + offset) + 3, bounds.width() - 16, 20),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, caption)
        downloaded = index.data(DOWNLOADED_ROLE)
        if downloaded:
            done = not isinstance(book, Series) or downloaded == len(book.books)
            marker = "" if done else str(downloaded)
            width = max(23, 11 + option.fontMetrics.horizontalAdvance(marker))
            badge = QRectF(front.right() - width - 2, front.bottom() - 24, width, 21)
            painter.setPen(QPen(palette.color(QPalette.ColorRole.Base), 1))
            painter.setBrush(palette.color(QPalette.ColorRole.Highlight))
            painter.drawRoundedRect(badge, 10.5, 10.5)
            if done:
                painter.translate(badge.center() - QPointF(8, 8))
                painter.scale(16 / 24, 16 / 24)
                paint_glyph(painter, "check", "#ffffff")
            else:
                painter.setPen(QColor("#ffffff"))
                painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, marker)
        painter.restore()


class SettingsPage(QWidget):
    sections = (("general", "General"), ("accounts", "Accounts"), ("downloads", "Downloads"),
                ("network", "Network"), ("guide", "Guide"), ("about", "About & updates"))

    def __init__(self, window):
        super().__init__()
        self.window = window
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(17)
        header = QHBoxLayout()
        header.addWidget(icon_button("Back to bookshelf", "back", window.show_bookshelf))
        header.addWidget(heading("Settings", "pageTitle"), 1)
        layout.addLayout(header)
        body = QHBoxLayout()
        body.setSpacing(28)
        sidebar = QWidget()
        sidebar.setObjectName("settingsSidebar")
        sidebar.setFixedWidth(168)
        links = QVBoxLayout(sidebar)
        links.setContentsMargins(0, 4, 0, 0)
        links.setSpacing(5)
        self.buttons = QButtonGroup(self)
        self.content = QStackedWidget()
        for index, (key, title) in enumerate(self.sections):
            link = button(title, partial(self.select, key))
            link.setCheckable(True)
            link.setProperty("settingsLink", True)
            self.buttons.addButton(link, index)
            links.addWidget(link)
            make = {"guide": window.guide, "about": window.about}.get(key)
            if make is None:
                make = getattr(self, key)
            self.content.addWidget(make())
        links.addStretch()
        body.addWidget(sidebar)
        body.addWidget(self.content, 1)
        layout.addLayout(body, 1)
        self.select("general")

    def select(self, key):
        index = next(i for i, (section, _) in enumerate(self.sections) if section == key)
        self.section = key
        self.content.setCurrentIndex(index)
        self.buttons.button(index).setChecked(True)
        if key == "accounts" and getattr(self.window, "settings_view", None) is self:
            self.refresh_accounts()

    def form(self, title):
        page = QWidget()
        page.setObjectName("settingsContent")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(25, 19, 25, 20)
        layout.setSpacing(16)
        layout.addWidget(heading(title, "sectionTitle"))
        fields = QWidget()
        fields.setProperty("settingsGroup", True)
        fields.setMaximumWidth(680)
        form = QFormLayout(fields)
        form.setContentsMargins(0, 0, 0, 0)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setHorizontalSpacing(24)
        form.setVerticalSpacing(15)
        row = QHBoxLayout()
        row.addWidget(fields, 1)
        row.addStretch()
        layout.addLayout(row)
        layout.addStretch()
        scroll = QScrollArea()
        scroll.setObjectName("settingsScroll")
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        return scroll, form

    def general(self):
        page, form = self.form("General")
        language = MenuField()
        for code, name in (("", "System language"), ("zh", "简体中文"), ("en", "English"), ("ja", "日本語")):
            language.addItem(tr(name), code)
        language.setCurrentIndex(language.findData(self.window.settings.value("language", "")))
        language.currentIndexChanged.connect(lambda: self.window.change_language(language.currentData()))
        form.addRow(tr("Language"), language)

        theme = MenuField()
        for code, name in (("system", "System appearance"), ("light", "Light"), ("dark", "Dark")):
            theme.addItem(tr(name), code)
        theme.setCurrentIndex(theme.findData(self.window.settings.value("theme", "system")))
        def change_theme():
            self.window.settings.setValue("theme", theme.currentData())
            self.window.apply_appearance()
        theme.currentIndexChanged.connect(change_theme)
        form.addRow(tr("Appearance"), theme)
        return page

    def downloads(self):
        page, form = self.form("Downloads")
        self.calibre = QLineEdit(self.window.settings.value("calibre_path", ""))
        self.calibre.setPlaceholderText(tr("Find Calibre automatically"))
        row = QHBoxLayout()
        row.addWidget(self.calibre, 1)
        row.addWidget(icon_button("Choose Calibre folder", "folder", self.choose_calibre))
        form.addRow(tr("Calibre path"), row)
        self.calibre_status = heading("", "muted")
        form.addRow(self.calibre_status)
        actions = QHBoxLayout()
        actions.addWidget(button("Save Calibre path", self.save_calibre, primary=True))
        actions.addWidget(button("Download Calibre", lambda: open_link(cli.CALIBRE_URL)))
        actions.addStretch()
        form.addRow(actions)
        remind = self.calibre_reminder = QCheckBox(tr("Remind me at startup if Calibre is missing"))
        remind.setChecked(self.window.settings.value("calibre_reminder", True, type=bool))
        remind.toggled.connect(lambda value: self.window.settings.setValue("calibre_reminder", value))
        form.addRow(remind)
        dedrm = QCheckBox(tr("Remove Kindle DRM"))
        dedrm.setChecked(self.window.settings.value("dedrm", True, type=bool))
        def change_drm(value):
            self.window.settings.setValue("dedrm", value)
            self.window.update_format()
        dedrm.toggled.connect(change_drm)
        form.addRow(dedrm)
        return page

    def choose_calibre(self):
        path = QFileDialog.getExistingDirectory(self, tr("Choose Calibre folder"), self.calibre.text())
        if path:
            self.calibre.setText(path)

    def save_calibre(self):
        path = self.calibre.text().strip()
        path = str(Path(path).expanduser().absolute()) if path else ""
        try:
            with cli.using_calibre(path):
                cli.check_calibre()
        except cli.CalibreUnavailable as error:
            if path:
                self.calibre_status.setText(str(error))
                return
            self.calibre_status.setText(str(error))
        else:
            self.calibre_status.setText(tr("Calibre is available."))
        self.window.settings.setValue("calibre_path", path)
        self.calibre.setText(path)

    def network(self):
        page, form = self.form("Network")
        settings = self.window.settings
        self.mode = MenuField()
        for code, name in (("system", "System proxy"), ("direct", "Direct connection"), ("custom", "Custom proxy")):
            self.mode.addItem(tr(name), code)
        self.mode.setCurrentIndex(self.mode.findData(settings.value("proxy_mode", "system")))
        self.address = QLineEdit(settings.value("proxy_address", ""))
        self.address.setPlaceholderText("http://127.0.0.1:7890")
        self.username = QLineEdit(settings.value("proxy_username", ""))
        self.password = QLineEdit(self.window.proxy_password)
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.network_status = heading("", "muted")
        form.addRow(tr("Network"), self.mode)
        form.addRow(tr("Proxy address"), self.address)
        form.addRow(tr("Proxy username"), self.username)
        form.addRow(tr("Proxy password"), self.password)
        form.addRow(self.network_status)
        actions = QHBoxLayout()
        actions.addWidget(button("Save settings", self.save_network, primary=True))
        actions.addStretch()
        form.addRow(actions)
        def change_mode():
            for field in (self.address, self.username, self.password):
                field.setEnabled(self.mode.currentData() == "custom")
        change_mode()
        self.mode.currentIndexChanged.connect(change_mode)
        return page

    def save_network(self):
        from urllib.parse import quote, urlsplit

        mode = self.mode.currentData()
        address = self.address.text().strip()
        username, password = self.username.text(), self.password.text()
        if mode == "custom":
            try:
                parts = urlsplit(address)
                if parts.username or parts.password or parts.path not in {"", "/"} or parts.query or parts.fragment:
                    raise ValueError
                auth = f"{quote(username, safe='')}:{quote(password, safe='')}@" if username else ""
                with using_proxy(f"{parts.scheme}://{auth}{parts.netloc}"):
                    proxy_address()
            except ValueError:
                self.network_status.setText(tr("Enter a valid proxy address."))
                return
        for key, value in (("proxy_mode", mode), ("proxy_address", address), ("proxy_username", username)):
            self.window.settings.setValue(key, value)
        self.window.proxy_password = password
        self.network_status.setText(tr("Settings saved."))

    def accounts(self):
        page, form = self.form("Accounts")
        self.provider_menu = MenuField()
        for code, label in PROVIDERS.items():
            self.provider_menu.addItem(label, code)
        self.market = MenuField()
        for code, market in MARKETPLACES.items():
            self.market.addItem(tr(market.name), code)
        self.market_label = QLabel(tr("Kindle store"))
        form.addRow(tr("Source"), self.provider_menu)
        form.addRow(self.market_label, self.market)
        self.account_status = heading("", "muted")
        form.addRow(self.account_status)
        actions = QHBoxLayout()
        self.login_action = button("Sign in", lambda: self.window.open_login(self.account_selection()), primary=True)
        self.reauthorize = button("Reauthorize", lambda: self.window.open_login(self.account_selection(), force=True))
        self.import_action = button("Paste credentials", lambda: self.window.open_login(self.account_selection(), manual=True))
        self.logout_action = button("Sign out", lambda: self.window.sign_out(*self.account_selection()[:2]))
        self.logout_action.setProperty("danger", True)
        for action in (self.login_action, self.reauthorize, self.import_action, self.logout_action):
            actions.addWidget(action)
        actions.addStretch()
        form.addRow(actions)
        self.compatibility = QWidget()
        self.compatibility.setProperty("settingsGroup", True)
        legacy = QVBoxLayout(self.compatibility)
        legacy.setContentsMargins(0, 12, 0, 0)
        legacy.addWidget(heading("Comipo compatibility", "sectionTitle"))
        self.legacy_status = heading("", "muted")
        legacy.addWidget(self.legacy_status)
        self.legacy_check = button("Check for missing Comipo purchases", self.check_legacy)
        legacy.addWidget(self.legacy_check, alignment=Qt.AlignmentFlag.AlignLeft)
        self.legacy_toggle = QCheckBox(tr("Include missing Comipo purchases"))
        self.legacy_toggle.setChecked(self.window.settings.value("legacy_comipo", False, type=bool))
        self.legacy_toggle.setEnabled(self.legacy_toggle.isChecked())
        self.legacy_toggle.toggled.connect(self.window.set_legacy)
        legacy.addWidget(self.legacy_toggle)
        form.addRow(self.compatibility)
        self.provider_menu.currentIndexChanged.connect(self.refresh_accounts)
        self.market.currentIndexChanged.connect(self.refresh_accounts)
        return page

    def account_selection(self):
        source = self.provider_menu.currentData()
        region = self.market.currentData() if source == "amazon" else "jp"
        name = PROVIDERS[source]
        if source == "amazon":
            name += f" · {tr(MARKETPLACES[region].name)}"
        return source, region, name

    def choose_account(self, source, region):
        self.provider_menu.blockSignals(True)
        self.market.blockSignals(True)
        self.provider_menu.setCurrentIndex(self.provider_menu.findData(source))
        self.market.setCurrentIndex(self.market.findData(region))
        self.provider_menu.blockSignals(False)
        self.market.blockSignals(False)
        if self.section == "accounts":
            self.refresh_accounts()

    def refresh_accounts(self):
        source, region, _ = selection = self.account_selection()
        signed = self.window.saved_session_path(source, region).is_file()
        self.market.setVisible(source == "amazon")
        self.market_label.setVisible(source == "amazon")
        self.login_action.setVisible(not signed)
        self.reauthorize.setVisible(signed)
        self.logout_action.setVisible(signed)
        self.account_status.setText(tr("Checking account…") if signed else tr("Signed out."))
        old = source == "dlsite" and session_path(cli.DATA, COMIPO).is_file()
        self.compatibility.setVisible(old)
        self.legacy_check.setEnabled(signed)
        self.legacy_status.setText(tr("Sign in to DLsite with the same viviON ID as your saved Comipo account.") if old and not signed else "")
        if not signed:
            return

        def current():
            return self.window.settings_view is self and self.account_selection() == selection

        def ready(profile):
            if current():
                status = tr("Signed in") if profile is not None else tr("This session has expired. Sign in again.")
                if profile:
                    status += "\n" + " · ".join(value for value in (profile.get("login"), profile.get("id")) if value)
                self.account_status.setText(status)

        self.window.run(partial(self.window.read_account, source, region), ready,
                        lambda message: self.account_status.setText(message) if current() else None)

    def check_legacy(self):
        from kindlegarten.play_library import read_play_library

        self.legacy_check.setEnabled(False)
        self.legacy_status.setText(tr("Comparing purchased libraries…"))
        def load():
            with self.window.operation_lock, cli.project_lock():
                return sum(book.legacy for book in volumes(read_play_library(cli.DATA, cli.COMMAND, legacy=True)).values())
        def ready(count):
            if self.window.settings_view is self:
                self.legacy_check.setEnabled(True)
                self.legacy_toggle.setEnabled(bool(count))
                if not count and self.legacy_toggle.isChecked():
                    self.legacy_toggle.setChecked(False)
                self.legacy_status.setText(tr("{count} purchases are available through Comipo compatibility.", count=count)
                                           if count else tr("No missing Comipo purchases were found."))
        def failed(message):
            if self.window.settings_view is self:
                self.legacy_check.setEnabled(True)
                self.legacy_status.setText(message)
        self.window.run(load, ready, failed)

class LoginDialog(QDialog):
    def __init__(self, window, selection, *, force=False, manual=False):
        super().__init__(window)
        self.window = window
        self.source, self.region, name = selection
        self.force = force
        self.authorization = None
        self.setWindowTitle(tr("Sign in"))
        layout = QVBoxLayout(self)
        layout.addWidget(heading(name, "pageTitle"))
        layout.addWidget(heading("Complete sign-in in your browser.", "muted"))
        self.browser = button("Open browser", self.open_browser, primary=True)
        layout.addWidget(self.browser)
        self.manual = button("Paste credentials", self.show_manual)
        layout.addWidget(self.manual)
        self.paste = QWidget()
        fields = QVBoxLayout(self.paste)
        fields.setContentsMargins(0, 0, 0, 0)
        message = ("Open the Amazon link below, then paste the full final address after signing in."
                   if self.source == "amazon" else
                   "In the signed-in Play site, open Network in developer tools and reload. Copy the Cookie value from the authorize request.")
        fields.addWidget(heading(message, "muted"))
        if self.source == "amazon":
            fields.addWidget(button("Open Amazon sign-in link", self.open_manual_link))
        self.secret = QLineEdit()
        self.secret.setEchoMode(QLineEdit.EchoMode.Password)
        self.secret.setPlaceholderText(tr("Paste credentials"))
        fields.addWidget(self.secret)
        fields.addWidget(button("Verify and save", self.submit, primary=True))
        if self.source != "amazon":
            fields.addWidget(button("Import session file", self.import_file))
        self.paste.setVisible(manual)
        layout.addWidget(self.paste)
        self.status = heading("", "muted")
        layout.addWidget(self.status)
        self.resize(440, self.sizeHint().height())

    def show_manual(self):
        self.paste.setVisible(not self.paste.isVisible())
        self.secret.clear()
        self.adjustSize()

    def challenge(self):
        if self.authorization is None:
            cli.setup_worker_runtime()
            self.authorization = json.loads(cli.run_worker("_prepare_login", self.region, capture=True))
        return self.authorization

    def call(self, action, callback=None):
        self.setEnabled(False)
        self.status.setText(tr("Waiting for sign-in…"))
        def failed(message):
            self.setEnabled(True)
            self.status.setText(message)
        def success(value):
            self.setEnabled(True)
            self.status.clear()
            if callback:
                callback(value)
            else:
                self.accept()
        self.window.run(action, success, failed)

    def open_manual_link(self):
        def prepare():
            with self.window.operation_lock, cli.project_lock():
                return self.challenge()

        self.call(prepare, lambda authorization: open_link(authorization["url"]))

    def save_amazon(self, authorization, callback):
        payload = json.dumps({"authorization": authorization, "callback": callback})
        cli.run_worker("_register_login", cli.account_path(self.region), input_text=payload)

    def open_browser(self):
        def sign_in():
            with self.window.operation_lock, cli.project_lock():
                if self.source == "amazon":
                    state = cli.account_path(self.region)
                    if state.is_file() and not self.force:
                        cli.setup_worker_runtime()
                        if json.loads(cli.run_worker("_validate_login", state, capture=True)):
                            return
                    authorization = self.challenge()
                    profile = cli.DATA / "browser" / f"amazon-{self.region}"
                    callback = open_amazon_login(authorization["url"], authorization["site"], cli.COMMAND,
                                                 profile=None if self.force else profile)
                    return self.save_amazon(authorization, callback)
                return login(cli.DATA, cli.COMMAND, site=DLSITE, interactive=False, force=self.force)
        self.call(sign_in)

    def import_file(self):
        name, _ = QFileDialog.getOpenFileName(self, tr("Import session file"), "", "JSON (*.json)")
        if name:
            self.import_play(Path(name), None)

    def import_play(self, path, text):
        def save():
            with self.window.operation_lock, cli.project_lock():
                import_session(cli.DATA, cli.COMMAND, path, site=DLSITE, pasted=text)
        self.call(save)

    def submit(self):
        secret = self.secret.text().strip()
        self.secret.clear()
        if not secret:
            self.status.setText(tr("Paste credentials first."))
        elif self.source == "amazon":
            if self.authorization is None:
                self.status.setText(tr("Open the Amazon sign-in link first."))
                return
            def save():
                with self.window.operation_lock, cli.project_lock():
                    self.save_amazon(self.authorization, secret)
            self.call(save)
        else:
            self.import_play(None, secret)


@dataclass(eq=False)
class DownloadTask:
    request: DownloadRequest
    cancelled: Event = field(default_factory=Event)
    state: str = "queued"
    percent: int | None = None
    speed: str = ""
    detail: str = ""


class DownloadRow(QWidget):
    def __init__(self, task, action):
        super().__init__()
        self.task = task
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 8)
        layout.setSpacing(5)
        top = QHBoxLayout()
        title = heading(task.request.entry.title, "downloadTitle")
        title.setToolTip(task.request.entry.title)
        top.addWidget(title, 1)
        self.action = icon_button("Cancel download", "cancel", action)
        self.action.setFixedSize(28, 28)
        self.action.setIconSize(QSize(16, 16))
        top.addWidget(self.action)
        layout.addLayout(top)
        self.info = heading("", "downloadDetail")
        layout.addWidget(self.info)
        self.line = QProgressBar()
        self.line.setObjectName("downloadProgress")
        self.line.setTextVisible(False)
        self.line.setFixedHeight(6)
        layout.addWidget(self.line)
        self.update_task()

    def update_task(self):
        task = self.task
        state = task.state
        info = {
            "queued": "Queued",
            "running": "Downloading",
            "cancelling": "Cancelling…",
            "complete": "Completed",
            "cancelled": "Cancelled.",
            "failed": "Download failed",
        }[state]
        text = tr(info)
        if state == "running":
            if task.percent is not None:
                text += f" · {task.percent}%"
            if task.speed:
                text += f" · {task.speed}"
        if task.detail and state in {"running", "failed"}:
            text += f"\n{task.detail}"
        self.info.setText(text)
        self.line.setVisible(state != "cancelled")
        if state == "running" and task.percent is None:
            self.line.setRange(0, 0)
        else:
            self.line.setRange(0, 100)
            self.line.setValue(100 if state == "complete" else task.percent or 0)
        self.action.setVisible(state != "cancelled")
        self.action.setEnabled(state != "cancelling")
        title, glyph = {
            "complete": ("Open folder", "open-folder"),
            "failed": ("Retry download", "refresh"),
        }.get(state, ("Cancel download", "cancel"))
        self.action.setToolTip(tr(title))
        self.action.setAccessibleName(tr(title))
        if self.action.property("glyph") != glyph or self.action.icon().isNull():
            self.action.setProperty("glyph", glyph)
            self.action.setIcon(action_icon(glyph, self.palette().color(QPalette.ColorRole.ButtonText)))


class AddDownloadsDialog(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.busy = False
        source, region, _ = window.provider()
        self.setWindowTitle(tr("Add download tasks"))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(12)
        fields = QFormLayout()
        self.source = MenuField()
        for code, name in PROVIDERS.items():
            self.source.addItem(name, code)
        self.source.setCurrentIndex(self.source.findData(source))
        fields.addRow(tr("Source"), self.source)
        self.market = MenuField()
        for code, market in MARKETPLACES.items():
            self.market.addItem(tr(market.name), code)
        saved_market = window.marketplace.currentData() if source != "amazon" else region
        self.market.setCurrentIndex(self.market.findData(saved_market))
        self.market_label = QLabel(tr("Kindle store"))
        fields.addRow(self.market_label, self.market)
        self.source.currentIndexChanged.connect(self.change_source)
        layout.addLayout(fields)
        self.ids = QPlainTextEdit()
        self.ids.setPlaceholderText(tr("One book or series ID per line"))
        self.ids.setAccessibleName(tr("Book or series IDs"))
        layout.addWidget(self.ids, 1)
        self.status = heading("", "muted")
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.status)
        scroll.setMaximumHeight(112)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.feedback = scroll
        self.feedback.hide()
        layout.addWidget(scroll)
        actions = QHBoxLayout()
        actions.addStretch(1)
        self.cancel = button("Cancel", self.reject)
        self.submit_button = button("Add to queue", self.submit, primary=True)
        actions.addWidget(self.cancel)
        actions.addWidget(self.submit_button)
        layout.addLayout(actions)
        self.change_source()
        self.resize(460, 360)
        self.ids.setFocus()

    def change_source(self):
        kindle = self.source.currentData() == "amazon"
        self.market_label.setVisible(kindle)
        self.market.setVisible(kindle)

    def inform(self, message):
        self.status.setText(message)
        self.feedback.setVisible(bool(message))

    def set_busy(self, busy):
        self.busy = busy
        for widget in (self.source, self.market, self.ids, self.cancel, self.submit_button):
            widget.setEnabled(not busy)

    def reject(self):
        if not self.busy:
            super().reject()

    def submit(self):
        lines = [(number, line.strip()) for number, line in enumerate(self.ids.toPlainText().splitlines(), 1)
                 if line.strip()]
        if not lines:
            self.inform(tr("Enter at least one book or series ID."))
            return
        window = self.window
        source = self.source.currentData()
        region = self.market.currentData() if source == "amazon" else "jp"
        if not window.location.text().strip():
            self.inform(tr("Choose a destination folder first."))
            return
        if not window.saved_session_path(source, region).is_file():
            self.inform(tr("Sign in to the selected source before adding tasks."))
            return
        current = window.library if window.provider()[:2] == (source, region) else None
        self.set_busy(True)
        self.inform(tr("Looking up IDs…"))

        def resolve():
            library = current or cached_library(cli.DATA, source, region)
            entries, errors = task_entries(lines, library, source=source)
            if any(reason == "ID not found in your purchased library." for _, _, reason in errors):
                try:
                    snapshot = window.read_purchases(source, region)
                except Exception as error:
                    message = str(error) if isinstance(error, cli.KindleError) else tr(
                        "The operation failed. Check the connection, account, or destination.")
                    return library, entries, [
                        (number, text, tr("Could not refresh the purchased library: {detail}", detail=message))
                        if reason == "ID not found in your purchased library." else (number, text, reason)
                        for number, text, reason in errors
                    ]
                if snapshot.complete or library is None or not library.complete:
                    library = snapshot
                entries, errors = task_entries(lines, library, source=source)
                if not snapshot.complete:
                    errors = [(number, text, "The service did not return the complete library.")
                              if reason == "ID not found in your purchased library." else (number, text, reason)
                              for number, text, reason in errors]
            return library, entries, errors

        def ready(result):
            library, entries, errors = result
            count = window.queue_requests(window.make_requests(entries, source, region, library))
            self.set_busy(False)
            if errors:
                self.ids.setPlainText("\n".join(text for _, text, _ in errors))
                self.inform("\n".join(tr("Line {line}: {id} — {detail}", line=number, id=text, detail=tr(reason))
                                      for number, text, reason in errors))
            elif count:
                self.accept()
            else:
                self.inform(tr("These downloads are already in the queue."))

        def failed(message):
            self.set_busy(False)
            self.inform(message)

        window.run(resolve, ready, failed)


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings("Kindlegarten", "Kindlegarten")
        self.proxy_password = ""
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(4)
        self.download_pool = QThreadPool(self)
        self.download_pool.setMaxThreadCount(10)
        self.operation_lock = Lock()
        self.jobs = set()
        self.downloads = []
        self.download_rows = {}
        self.downloads_visible = False
        self.release = None
        self.update_stage = ""
        self.update_percent = 0
        self.update_error = ""
        self.update_cancelled = None
        self.update_job = None
        self.update_file = None
        self.update_timer = QTimer(self, interval=400)
        self.update_timer.timeout.connect(self.maybe_install_update)
        self.enqueueing = set()
        self.pending_cards = set()
        self.bulk_starting = False
        self.spinner_angle = 0
        self.spinner_timer = QTimer(self)
        self.spinner_timer.setInterval(36)
        self.spinner_timer.timeout.connect(self.spin)
        self.library = None
        self.series = None
        self.needs_sign_in = False
        self.refreshing = None
        self.pending_refresh = None
        self.covers_requested = set()
        self.cover_misses = set()
        self.library_revision = 0
        self.downloaded = set()
        self.scan_revision = 0
        self.scan_active = False
        self.watched_stamps = {}
        self.folder_watcher = QFileSystemWatcher(self)
        self.folder_watcher.directoryChanged.connect(self.schedule_download_scan)
        self.scan_timer = QTimer(self, singleShot=True, interval=160)
        self.scan_timer.timeout.connect(self.scan_download_folder)
        self.folder_poll = QTimer(self, interval=2000)
        self.folder_poll.timeout.connect(lambda: self.scan_download_folder(poll=True))
        self.folder_poll.start()
        QApplication.instance().applicationStateChanged.connect(self.application_state_changed)
        self.resize(1240, 760)
        self.setMinimumSize(920, 530)
        self.setWindowTitle(f"Kindlegarten v{__version__}")
        self.setWindowIcon(QIcon(str(APP_ICON)))
        token = LANGUAGE.set(self.locale())
        try:
            self.build()
            self.load_cached_library()
            self.apply_appearance()
        finally:
            LANGUAGE.reset(token)
        QApplication.styleHints().colorSchemeChanged.connect(self.apply_appearance)

    def locale(self):
        selected = self.settings.value("language", "") or QLocale.system().name().split("_", 1)[0]
        return selected if selected in SUPPORTED else "en"

    def proxy(self):
        from urllib.parse import quote, urlsplit

        mode = self.settings.value("proxy_mode", "system")
        if mode != "custom":
            return mode
        address = urlsplit(self.settings.value("proxy_address", ""))
        username = self.settings.value("proxy_username", "")
        auth = f"{quote(username, safe='')}:{quote(self.proxy_password, safe='')}@" if username else ""
        return f"{address.scheme}://{auth}{address.netloc}"

    def run(self, action, success=None, failure=None, progress=None, status=None, *, priority=0, pool=None, cancelled=None):
        job = Job(action, self.proxy(), self.locale())
        self.jobs.add(job)
        if success:
            job.signals.result.connect(success, Qt.ConnectionType.QueuedConnection)
        if failure:
            job.signals.failed.connect(failure, Qt.ConnectionType.QueuedConnection)
        if progress:
            job.signals.progress.connect(progress, Qt.ConnectionType.QueuedConnection)
        if status:
            job.signals.status.connect(status, Qt.ConnectionType.QueuedConnection)
        if cancelled:
            job.signals.cancelled.connect(cancelled, Qt.ConnectionType.QueuedConnection)
        job.signals.finished.connect(lambda: self.jobs.discard(job), Qt.ConnectionType.QueuedConnection)
        (self.pool if pool is None else pool).start(job, priority)

    def build(self, page=0):
        saved = None
        if hasattr(self, "grid"):
            saved = (self.search.text(), self.location.text(), self.format.currentData(),
                     self.grid.verticalScrollBar().value(),
                     {(type(item.data(ROLE)), item.data(ROLE).id) for item in self.grid.selectedItems()},
                     self.settings_view.section, self.settings_view.account_selection())
        language_token = LANGUAGE.set(self.locale())
        try:
            shell = QWidget()
            outer = QVBoxLayout(shell)
            outer.setContentsMargins(38, 26, 38, 22)
            outer.setSpacing(22)
            nav = QHBoxLayout()
            nav.setSpacing(9)
            brand_icon = QLabel()
            brand_icon.setPixmap(self.windowIcon().pixmap(QSize(29, 29)))
            nav.addWidget(brand_icon)
            nav.addWidget(heading("Kindlegarten", "brand"))
            nav.addWidget(heading(f"v{__version__}", "versionTag"), alignment=Qt.AlignmentFlag.AlignVCenter)
            nav.addStretch()
            self.download_toggle = icon_button("Toggle downloads", "download", self.set_downloads_visible)
            self.download_toggle.setCheckable(True)
            self.download_toggle.setChecked(self.downloads_visible)
            nav.addWidget(self.download_toggle)
            nav.addWidget(icon_button("Settings", "settings", self.open_settings))
            outer.addLayout(nav)

            self.pages = QStackedWidget()
            home = QWidget()
            body = QHBoxLayout(home)
            body.setContentsMargins(0, 0, 0, 0)
            body.setSpacing(22)
            body.addWidget(self.bookshelf(), 1)
            self.download_area = self.download_panel()
            self.download_area.setVisible(self.downloads_visible)
            body.addWidget(self.download_area)
            self.pages.addWidget(home)
            self.settings_view = SettingsPage(self)
            self.pages.addWidget(self.settings_view)
            self.pages.setCurrentIndex(page)
            self.download_toggle.setVisible(page == 0)
            outer.addWidget(self.pages, 1)
            self.setCentralWidget(shell)
            if saved is None:
                self.settings_view.choose_account(*self.provider()[:2])
            if saved:
                query, location, file_format, scroll, selection, section, account = saved
                self.search.setText(query)
                self.location.blockSignals(True)
                self.location.setText(location)
                self.location.blockSignals(False)
                self.format.setCurrentIndex(self.format.findData(file_format))
                for row in range(self.grid.count()):
                    item = self.grid.item(row)
                    entry = item.data(ROLE)
                    item.setSelected((type(entry), entry.id) in selection)
                scrollbar = self.grid.verticalScrollBar()
                scrollbar.setValue(scroll)
                QTimer.singleShot(0, partial(scrollbar.setValue, scroll))
                self.settings_view.choose_account(*account[:2])
                self.settings_view.select(section)
            self.present_update()
            self.update_starting()
        finally:
            LANGUAGE.reset(language_token)

    def bookshelf(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 9, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(heading("Your bookshelf", "pageTitle"))
        toolbar = QHBoxLayout()
        old_index = self.settings.value("provider", 0, type=int)
        old_source, old_region = LEGACY_PROVIDERS[old_index] if 0 <= old_index < len(LEGACY_PROVIDERS) else ("amazon", "jp")
        source = cli.canonical_source(self.settings.value("source", old_source))
        self.settings.setValue("source", source)
        region = self.settings.value("kindle_region", old_region)

        self.source = MenuField()
        self.source.setAccessibleName(tr("Source"))
        self.source.setMinimumWidth(118)
        for code, name in PROVIDERS.items():
            self.source.addItem(name, code)
        self.source.setCurrentIndex(self.source.findData(source if source in PROVIDERS else "amazon"))
        self.source.currentIndexChanged.connect(self.change_source)
        toolbar.addWidget(self.source)

        self.marketplace = MenuField()
        self.marketplace.setAccessibleName(tr("Kindle store"))
        self.marketplace.setMaxVisibleItems(len(MARKETPLACES))
        for code, market in MARKETPLACES.items():
            self.marketplace.addItem(tr(market.name), code)
        self.marketplace.setCurrentIndex(self.marketplace.findData(region if region in MARKETPLACES else "jp"))
        self.marketplace.currentIndexChanged.connect(self.change_source)
        if self.source.currentData() != "amazon":
            self.marketplace.hide()
        toolbar.addWidget(self.marketplace)

        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Search titles"))
        self.search.setAccessibleName(tr("Search titles"))
        self.search.textChanged.connect(self.render_library)
        toolbar.addWidget(self.search, 1)
        self.sort_button = icon_button("Sort bookshelf", "filter", self.open_sort)
        self.sort_menu = QMenu(self.sort_button)
        self.sort_actions = QActionGroup(self.sort_menu)
        for code, label in SORT_ORDERS.items():
            action = self.sort_menu.addAction(tr(label))
            action.setData(code)
            action.setCheckable(True)
            action.setChecked(code == self.sort_order())
            self.sort_actions.addAction(action)
        self.sort_actions.triggered.connect(self.change_sort)
        toolbar.addWidget(self.sort_button)
        toolbar.addWidget(icon_button("Refresh", "refresh", lambda: self.refresh()))
        self.account_button = icon_button("Account", "user", self.open_account)
        toolbar.addWidget(self.account_button)
        layout.addLayout(toolbar)
        self.back = icon_button("Back to bookshelf", "back", self.leave_series)
        self.back.hide()
        layout.addWidget(self.back, alignment=Qt.AlignmentFlag.AlignLeft)
        self.status = heading("Choose a source and refresh to see your purchases." if self.library is None else "", "muted")
        layout.addWidget(self.status)
        self.grid = BookshelfGrid()
        self.grid.setViewMode(QListView.ViewMode.IconMode)
        self.grid.setResizeMode(QListView.ResizeMode.Adjust)
        self.grid.setMovement(QListView.Movement.Static)
        self.grid.setIconSize(QSize(124, 168))
        self.grid.setGridSize(QSize(170, 247))
        self.grid.setSpacing(8)
        self.grid.setItemDelegate(BookTile(self.grid))
        self.grid.setWordWrap(True)
        self.grid.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.grid.action_requested.connect(self.open_item)
        self.grid.menu_requested.connect(self.open_card_menu)
        self.grid.verticalScrollBar().valueChanged.connect(self.queue_covers)
        self.grid.verticalScrollBar().valueChanged.connect(self.grid.refresh_pointer)
        self.shelf_stack = QStackedWidget()
        self.shelf_stack.addWidget(self.grid)
        sign_in_page = QWidget()
        sign_in_layout = QVBoxLayout(sign_in_page)
        sign_in_layout.setContentsMargins(0, 0, 0, 0)
        self.shelf_login = button("Sign in", self.open_account, primary=True)
        self.shelf_login.setMinimumWidth(120)
        sign_in_layout.addWidget(self.shelf_login, alignment=Qt.AlignmentFlag.AlignCenter)
        self.shelf_stack.addWidget(sign_in_page)
        layout.addWidget(self.shelf_stack, 1)
        footer = QHBoxLayout()
        self.location = QLineEdit(self.settings.value("destination", str(Path.home() / "Downloads" / "Kindlegarten")))
        self.location.setPlaceholderText(tr("Destination folder"))
        self.location.setAccessibleName(tr("Destination folder"))
        self.location.textChanged.connect(self.destination_changed)
        footer.addWidget(self.location, 1)
        footer.addWidget(icon_button("Choose folder", "folder", self.choose_folder))
        self.format = MenuField()
        self.format.setAccessibleName(tr("Format"))
        self.format.setMinimumWidth(92)
        for option in FORMATS:
            self.format.addItem(tr("Auto") if option == "auto" else option.upper(), option)
        footer.addWidget(self.format)
        self.original_format = heading("Original KFX", "originalFormat")
        footer.addWidget(self.original_format)
        self.download_button = button("Download selected", lambda: self.download(), primary=True)
        self.download_button.setProperty("glyph", "download")
        self.download_button.setIconSize(QSize(17, 17))
        footer.addWidget(self.download_button)
        layout.addLayout(footer)
        self.shelf_download_notice = heading("", "muted")
        self.shelf_download_notice.hide()
        layout.addWidget(self.shelf_download_notice)
        self.update_format()
        self.render_library()
        return page

    def download_panel(self):
        panel = QWidget()
        panel.setObjectName("downloadPanel")
        panel.setMinimumWidth(245)
        panel.setMaximumWidth(310)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(18, 9, 0, 0)
        layout.setSpacing(12)
        title = QHBoxLayout()
        title.addWidget(heading("Downloads", "downloadHeading"), 1)
        title.addWidget(icon_button("Add download tasks", "add", self.add_downloads))
        layout.addLayout(title)
        settings = QHBoxLayout()
        settings.addWidget(heading("Concurrent downloads", "muted"), 1)
        self.limit = MenuField()
        self.limit.setAccessibleName(tr("Concurrent downloads"))
        for count in range(1, 11):
            self.limit.addItem(str(count), count)
        saved = self.settings.value("download_limit", 3, type=int)
        self.limit.setCurrentIndex(self.limit.findData(saved if 1 <= saved <= 10 else 3))
        self.limit.currentIndexChanged.connect(self.change_download_limit)
        settings.addWidget(self.limit)
        layout.addLayout(settings)
        self.download_notice = heading("", "muted")
        self.download_notice.hide()
        layout.addWidget(self.download_notice)

        self.download_scroll = QScrollArea()
        self.download_scroll.setObjectName("downloadScroll")
        self.download_scroll.setWidgetResizable(True)
        rows = QWidget()
        self.download_layout = QVBoxLayout(rows)
        self.download_layout.setContentsMargins(0, 0, 5, 0)
        self.download_layout.setSpacing(6)
        self.download_empty = heading("No downloads yet.", "muted")
        self.download_layout.addWidget(self.download_empty)
        self.download_layout.addStretch()
        self.download_rows = {}
        for task in self.downloads:
            self.add_download_row(task)
        self.download_scroll.setWidget(rows)
        layout.addWidget(self.download_scroll, 1)
        return panel

    def set_downloads_visible(self, visible):
        self.downloads_visible = visible
        self.download_area.setVisible(visible)
        self.download_toggle.setChecked(visible)

    def add_downloads(self):
        AddDownloadsDialog(self).exec()

    def add_download_row(self, task):
        row = DownloadRow(task, lambda: self.download_action(task))
        self.download_rows[task] = row
        self.download_layout.insertWidget(self.download_layout.count() - 1, row)
        self.download_empty.hide()

    def guide(self):
        browser = QTextBrowser()
        browser.setOpenExternalLinks(True)
        from html import escape
        entries = "".join(f"<h3>{escape(tr(title))}</h3><p>{escape(tr(text))}</p>" for title, text in GUIDE)
        browser.setHtml(f"<h1>{tr('Getting started')}</h1>{entries}")
        return browser

    def about(self):
        page = QWidget()
        page.setObjectName("settingsContent")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(25, 19, 25, 20)
        layout.setSpacing(14)
        layout.addWidget(heading("Kindlegarten", "sectionTitle"))
        layout.addWidget(heading(tr("Version {version}", version=__version__), "muted"))
        actions = QHBoxLayout()
        self.check_update_button = button("Check for updates", self.check_updates, primary=True)
        actions.addWidget(self.check_update_button)
        actions.addWidget(button("GitHub repository", lambda: open_link(REPOSITORY)))
        actions.addStretch()
        layout.addLayout(actions)
        self.update_status = heading("", "muted")
        layout.addWidget(self.update_status)
        self.update_progress = QProgressBar()
        self.update_progress.setRange(0, 100)
        layout.addWidget(self.update_progress)
        actions = QHBoxLayout()
        self.update_button = button("Download update", self.begin_update, primary=True)
        self.cancel_update_button = button("Cancel", self.cancel_update)
        self.update_page_button = button("Open release", lambda: open_link(self.release.page))
        self.update_folder_button = button("Show downloaded package", self.show_update_folder)
        for action in (self.update_button, self.cancel_update_button, self.update_folder_button, self.update_page_button):
            actions.addWidget(action)
        actions.addStretch()
        layout.addLayout(actions)
        layout.addStretch()
        return page

    def provider(self):
        source = self.source.currentData()
        name = PROVIDERS[source]
        if source == "amazon":
            region = self.marketplace.currentData()
            return source, region, f"{name} · {tr(MARKETPLACES[region].name)}"
        return source, "jp", name

    def change_source(self):
        source = self.source.currentData()
        self.settings.setValue("source", source)
        self.settings.setValue("kindle_region", self.marketplace.currentData())
        self.marketplace.setVisible(source == "amazon")
        self.series = None
        self.update_format()
        self.load_cached_library()
        self.update_starting()
        self.refresh(silent=True)

    def saved_session_path(self, source, region):
        if source == "amazon":
            return cli.account_path(region)
        return session_path(cli.DATA, DLSITE)

    def load_cached_library(self):
        source, region, _ = self.provider()
        self.needs_sign_in = not self.saved_session_path(source, region).is_file()
        self.library = None if self.needs_sign_in else cached_library(cli.DATA, source, region)
        self.downloaded.clear()
        self.status.clear()
        self.render_library()
        self.schedule_download_scan()

    def show_sign_in(self):
        self.needs_sign_in = True
        self.library = None
        self.series = None
        self.downloaded.clear()
        self.status.clear()
        self.render_library()
        self.schedule_download_scan()

    def read_purchases(self, source, region):
        with self.operation_lock, cli.project_lock():
            legacy = source == "dlsite" and self.legacy_enabled()
            snapshot = cli.read_library(source, region, legacy_comipo=True) if legacy else cli.read_library(source, region)
            if not legacy and snapshot.complete and snapshot != cached_library(cli.DATA, source, region):
                try:
                    cache_library(cli.DATA, source, region, snapshot)
                except OSError:
                    # The live library remains usable when the cache cannot be written.
                    pass
            return snapshot

    def refresh(self, *, silent=False):
        source, region, _ = self.provider()
        if not self.saved_session_path(source, region).is_file():
            if not self.needs_sign_in:
                self.show_sign_in()
            return
        selection = (source, region, self.library_revision)
        if self.refreshing is not None:
            if self.refreshing != selection:
                self.pending_refresh = selection, silent
            return
        self.refreshing = selection
        quiet = silent and self.library is not None
        if not quiet:
            self.status.setText(tr("Refreshing bookshelf…"))
        if not silent:
            self.cover_misses.clear()

        def current():
            self.refreshing = None
            active = (*self.provider()[:2], self.library_revision)
            pending = self.pending_refresh
            self.pending_refresh = None
            if pending and pending[0] == active:
                self.refresh(silent=pending[1])
            return active == selection

        def opened(snapshot):
            if not current():
                return
            if snapshot is None:
                self.show_sign_in()
                return
            self.needs_sign_in = False
            if not snapshot.complete and self.library is not None and self.library.complete:
                if not quiet:
                    self.status.setText(tr("The service did not return the complete library."))
                return
            if snapshot != self.library:
                self.library = snapshot
                if self.series is not None:
                    self.series = next((group for group in snapshot.series if group.id == self.series.id), None)
                self.render_library(preserve=True)
            self.schedule_download_scan()
            self.status.setText(tr("The service did not return the complete library.") if not snapshot.complete
                                else tr("No purchased books found.") if not snapshot.series and not snapshot.books else "")
            if source == "dlsite" and not self.legacy_enabled() and missing_legacy_books(cli.DATA, snapshot):
                self.status.setText(tr("You can check for missing Comipo purchases in Settings → Accounts."))
            self.queue_covers()

        def rejected(message):
            if current() and not quiet:
                self.status.setText(message)

        def load():
            try:
                return self.read_purchases(source, region)
            except cli.SignInRequired:
                return None
        self.run(load, opened, rejected)

    def sort_order(self):
        selected = self.settings.value("library_sort", "newest")
        return selected if selected in SORT_ORDERS else "newest"

    def open_sort(self):
        anchor = self.sort_button.mapToGlobal(self.sort_button.rect().bottomRight())
        anchor.setX(anchor.x() - self.sort_menu.sizeHint().width() + 1)
        self.sort_menu.popup(anchor)

    def change_sort(self, action):
        self.settings.setValue("library_sort", action.data())
        self.render_library(preserve=True)

    def shelf_entries(self):
        if self.series:
            return self.series.books
        if self.library is None:
            return ()
        order = self.sort_order()
        collator = QCollator(QLocale(self.locale()))
        collator.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        collator.setNumericMode(True)
        entries = sorted((*self.library.series, *self.library.books),
                         key=lambda item: collator.sortKey(item.title), reverse=order == "title_desc")
        if order in {"newest", "oldest"}:
            direction = -1 if order == "newest" else 1
            entries.sort(key=lambda item: (item.purchased_at is None, direction * (item.purchased_at or 0)))
        return entries

    def update_format(self):
        original = self.source.currentData() == "amazon" and not self.settings.value("dedrm", True, type=bool)
        self.format.setVisible(not original)
        self.original_format.setVisible(original)

    def destination_changed(self):
        self.downloaded.clear()
        self.update_download_badges()
        self.schedule_download_scan()

    def application_state_changed(self, state):
        if state == Qt.ApplicationState.ApplicationActive:
            self.schedule_download_scan()

    def schedule_download_scan(self, *_):
        self.scan_revision += 1
        self.scan_timer.start()

    def scan_download_folder(self, *, poll=False):
        if self.scan_active or (poll and (not self.isVisible() or self.isMinimized() or not self.watched_stamps)):
            return
        destination = self.location.text().strip()
        if self.library is None or not destination:
            self.set_watched_folders(set())
            self.watched_stamps = {}
            return
        folder = Path(destination).expanduser().absolute()
        library = self.library
        source, region, _ = self.provider()
        revision = self.scan_revision
        stamps = self.watched_stamps
        self.scan_active = True

        def scan():
            before = folder_stamps(stamps)
            if poll and before == stamps:
                return None
            downloaded, watch = scan_downloads(cli.DATA, folder, library, source, region)
            after = folder_stamps(watch)
            return downloaded, watch, {path: before.get(path, stamp) for path, stamp in after.items()}

        def finished(result):
            self.scan_active = False
            if revision != self.scan_revision:
                self.scan_timer.start()
            elif result is not None:
                self.downloaded, watch, self.watched_stamps = result
                self.set_watched_folders(watch)
                self.update_download_badges()

        self.run(scan, finished, lambda _: finished(None))

    def set_watched_folders(self, folders):
        watched = set(self.folder_watcher.directories())
        if removed := watched - folders:
            self.folder_watcher.removePaths(list(removed))
        if added := folders - watched:
            self.folder_watcher.addPaths(list(added))

    def update_download_badge(self, tile):
        entry = tile.data(ROLE)
        if isinstance(entry, Series):
            count = sum((entry.id, book.id) in self.downloaded for book in entry.books)
            label = tr("{count} of {total} books downloaded", count=count, total=len(entry.books)) if count else ""
        else:
            count = int((self.series.id if self.series else None, entry.id) in self.downloaded)
            label = tr("Downloaded") if count else ""
        tooltip = f"{entry.title} · {entry.id}"
        tile.setData(DOWNLOADED_ROLE, count)
        tile.setData(Qt.ItemDataRole.AccessibleDescriptionRole, label)
        tile.setToolTip(f"{tooltip}\n{label}" if label else tooltip)

    def update_download_badges(self):
        for row in range(self.grid.count()):
            self.update_download_badge(self.grid.item(row))

    def placeholder(self, is_series):
        pixmap = QPixmap(124, 168)
        pixmap.fill(QColor("#315950") if is_series else QColor("#c8d7c1"))
        painter = QPainter(pixmap)
        painter.setPen(QColor("#f4f6ef") if is_series else QColor("#37544b"))
        font = painter.font()
        font.setPointSize(28)
        painter.setFont(font)
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "▤" if is_series else "K")
        painter.end()
        return QIcon(pixmap)

    def render_library(self, *, preserve=False):
        if not hasattr(self, "grid"):
            return
        selection = {(type(item.data(ROLE)), item.data(ROLE).id) for item in self.grid.selectedItems()} if preserve else set()
        scroll = self.grid.verticalScrollBar().value() if preserve else 0
        self.grid.clear_hover()
        self.grid.clear()
        self.shelf_stack.setCurrentIndex(int(self.needs_sign_in))
        self.back.setVisible(self.series is not None)
        query = self.search.text().strip().casefold()
        placeholders = {False: self.placeholder(False), True: self.placeholder(True)}
        for entry in self.shelf_entries():
            series = isinstance(entry, Series)
            if query and query not in entry.title.casefold() and query not in entry.id.casefold():
                if not series or not any(query in item.title.casefold() for item in entry.books):
                    continue
            caption = tr("{count} purchased books", count=len(entry.books)) if series else ""
            label = f"{entry.title}\n{caption}" if caption else entry.title
            tile = QListWidgetItem(placeholders[series], label)
            tile.setData(CAPTION_ROLE, caption)
            tile.setData(ROLE, entry)
            self.update_download_badge(tile)
            self.grid.addItem(tile)
            if (type(entry), entry.id) in selection:
                tile.setSelected(True)
        self.grid.verticalScrollBar().setValue(scroll)
        QTimer.singleShot(0, self.queue_covers)

    @staticmethod
    def cover_entry(entry):
        return entry.books[0] if isinstance(entry, Series) and not entry.cover else entry

    @staticmethod
    def set_cover(item, payload):
        image = QPixmap()
        if not image.loadFromData(payload):
            return
        item.setIcon(QIcon(image))
        item.setData(COVER_LOADED_ROLE, True)

    def queue_covers(self, *_):
        if not hasattr(self, "grid"):
            return
        viewport = self.grid.viewport().rect()
        source, region, _ = self.provider()
        for index in range(self.grid.count()):
            item = self.grid.item(index)
            if item.data(COVER_LOADED_ROLE) or not self.grid.visualItemRect(item).intersects(viewport):
                continue
            entry = self.cover_entry(item.data(ROLE))
            if payload := cached_cover(cli.DATA, source, region, entry):
                self.set_cover(item, payload)
                continue
            key = (source, region, entry.id, entry.cover)
            if key in self.covers_requested or key in self.cover_misses:
                continue
            self.covers_requested.add(key)
            self.run(partial(cover, cli.DATA, source, region, entry),
                     lambda payload, key=key: self.show_cover(key, payload),
                     lambda _, key=key: self.show_cover(key, b""), priority=-1)

    def show_cover(self, key, payload):
        self.covers_requested.discard(key)
        if not payload:
            self.cover_misses.add(key)
            return
        if self.provider()[:2] != key[:2]:
            return
        for index in range(self.grid.count()):
            item = self.grid.item(index)
            entry = self.cover_entry(item.data(ROLE))
            if (entry.id, entry.cover) == key[2:]:
                self.set_cover(item, payload)

    def open_item(self, item):
        entry = item.data(ROLE)
        if isinstance(entry, Series):
            self.open_series(entry)
        else:
            self.download((entry,))

    def open_series(self, entry):
        self.series = entry
        self.search.clear()
        self.render_library()

    def open_card_menu(self, item, position):
        entry = item.data(ROLE)
        source, region, _ = self.provider()
        color = self.palette().color(QPalette.ColorRole.ButtonText)
        menu = QMenu(self.grid)
        menu.setObjectName("cardMenu")
        actions = []
        if isinstance(entry, Series):
            actions.append(("Open", "open-folder", lambda: self.open_series(entry)))
        actions.extend((
            ("Download", "download", lambda: self.download((entry,))),
            ("Store page", "external", lambda: open_link(store_page(entry, source, region))),
        ))
        for label, glyph, callback in actions:
            menu.addAction(action_icon(glyph, color), tr(label), callback)
        menu.exec(position)
        menu.deleteLater()

    def leave_series(self):
        self.series = None
        self.search.clear()
        self.render_library()

    def choose_folder(self):
        path = QFileDialog.getExistingDirectory(self, tr("Choose folder"), self.location.text())
        if path:
            self.location.setText(path)
            self.settings.setValue("destination", path)

    def unfinished_downloads(self):
        return self.enqueueing | {
            task.request for task in self.downloads if task.state in {"queued", "running", "cancelling"}
        }

    def make_requests(self, entries, source, region, library, series=None):
        folder = Path(self.location.text().strip()).expanduser().absolute()
        dedrm = source != "amazon" or self.settings.value("dedrm", True, type=bool)
        requested = self.format.currentData() if dedrm else "auto"
        calibre_path = self.settings.value("calibre_path", "")
        return [DownloadRequest(source, region, entry, folder, requested,
                                (series or series_for_book(library, entry.id)) if isinstance(entry, Book) else None,
                                calibre_path, dedrm)
                for entry in entries]

    def download(self, entries=None):
        bulk = entries is None
        if bulk:
            if self.bulk_starting:
                return
            entries = tuple(item.data(ROLE) for item in self.grid.selectedItems())
        if not entries:
            self.show_download_notice("Select at least one book or series.")
            return
        if not self.location.text().strip():
            self.show_download_notice("Choose a destination folder first.")
            return
        source, region, _ = self.provider()
        self.queue_requests(self.make_requests(entries, source, region, self.library, self.series),
                            bulk=bulk, card_feedback=not bulk)

    def queue_requests(self, requests, *, bulk=False, card_feedback=False):
        unfinished = {request.key for request in self.unfinished_downloads()}
        requests = {request.key: request for request in requests if request.key not in unfinished}
        if not requests:
            return 0
        requests = tuple(requests.values())
        self.settings.setValue("destination", str(requests[0].folder))
        self.clear_download_notice()
        self.enqueueing.update(requests)
        if bulk:
            self.bulk_starting = True
        if card_feedback:
            self.pending_cards.update(requests)
        self.update_starting()

        def enqueue():
            for request in requests:
                task = DownloadTask(request)
                self.downloads.append(task)
                self.add_download_row(task)
            self.enqueueing.difference_update(requests)
            self.pending_cards.difference_update(requests)
            if bulk:
                self.bulk_starting = False
            self.update_starting()
            self.set_downloads_visible(True)
            self.start_downloads()
            QTimer.singleShot(0, lambda: self.download_scroll.ensureWidgetVisible(self.download_rows[task]))

        # Give the initiating control one paint before the queue takes ownership.
        QTimer.singleShot(90 if bulk or card_feedback else 0, enqueue)
        return len(requests)

    def clear_download_notice(self):
        self.download_notice.hide()
        self.shelf_download_notice.hide()

    def show_download_notice(self, message):
        notice = self.download_notice if self.downloads_visible else self.shelf_download_notice
        notice.setText(tr(message))
        notice.show()

    def update_starting(self):
        source, region, _ = self.provider()
        self.grid.pending = {request.entry.id for request in self.pending_cards
                             if (request.source, request.region) == (source, region)}
        self.grid.refresh_pointer()
        self.download_button.setEnabled(not self.bulk_starting)
        self.download_button.setText(tr("Starting…") if self.bulk_starting else tr("Download selected"))
        if self.bulk_starting or self.pending_cards:
            if not self.spinner_timer.isActive():
                self.spinner_timer.start()
            self.spin()
        else:
            self.spinner_timer.stop()
        if not self.bulk_starting:
            self.download_button.setIcon(action_icon("download", "#ffffff"))
        self.grid.viewport().update()

    def spin(self):
        self.spinner_angle = (self.spinner_angle - 35) % 360
        self.grid.spinner_angle = self.spinner_angle
        self.grid.viewport().update()
        if self.bulk_starting:
            pixmap = QPixmap(51, 51)
            pixmap.setDevicePixelRatio(3)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            paint_spinner(painter, QRectF(3, 3, 11, 11), self.spinner_angle)
            painter.end()
            icon = QIcon()
            icon.addPixmap(pixmap, QIcon.Mode.Disabled)
            self.download_button.setIcon(icon)

    def change_download_limit(self):
        self.settings.setValue("download_limit", self.limit.currentData())
        self.start_downloads()

    def start_downloads(self):
        active = sum(item.state in {"running", "cancelling"} for item in self.downloads)
        available = self.limit.currentData() - active
        for task in (item for item in self.downloads if item.state == "queued"):
            if available <= 0:
                break
            self.start_download(task)
            available -= 1

    def start_download(self, task):
        task.state = "running"
        self.download_rows[task].update_task()

        def progress(percent, speed):
            task.percent, task.speed = percent, speed
            self.download_rows[task].update_task()

        def status(text):
            task.detail = text
            self.download_rows[task].update_task()

        def finish(state, detail=""):
            task.state, task.detail = state, detail
            self.download_rows[task].update_task()
            self.schedule_download_scan()
            self.start_downloads()

        self.run(partial(run_download, task.request, task.cancelled, self.operation_lock),
                 lambda _: finish("complete"),
                 lambda message: finish("cancelled" if task.cancelled.is_set() else "failed", message),
                 progress, status, pool=self.download_pool, cancelled=lambda: finish("cancelled"))

    def download_action(self, task):
        if task.state == "complete":
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(task.request.directory)))
        elif task.state == "failed":
            if any(item.key == task.request.key for item in self.unfinished_downloads()):
                self.show_download_notice("This download is already in the queue.")
                return
            self.clear_download_notice()
            task.state = "queued"
            task.percent, task.speed, task.detail = None, "", ""
            self.downloads.remove(task)
            self.downloads.append(task)
            row = self.download_rows[task]
            self.download_layout.removeWidget(row)
            self.download_layout.insertWidget(self.download_layout.count() - 1, row)
            row.update_task()
            self.download_scroll.ensureWidgetVisible(row)
            self.start_downloads()
        elif task.state in {"queued", "running"}:
            task.cancelled.set()
            task.state = "cancelled" if task.state == "queued" else "cancelling"
            self.download_rows[task].update_task()
            self.start_downloads()

    def read_account(self, source, region):
        with self.operation_lock, cli.project_lock():
            if source == "amazon":
                cli.setup_worker_runtime()
                return json.loads(cli.run_worker("_account", self.saved_session_path(source, region), capture=True))
            return saved_account(cli.DATA, cli.COMMAND, DLSITE)

    def open_account(self):
        self.open_settings("accounts", account=self.provider())

    def open_login(self, selection, *, force=False, manual=False):
        dialog = LoginDialog(self, selection, force=force, manual=manual)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            source, region, _ = selection
            if source == "dlsite":
                self.settings.setValue("legacy_comipo", False)
                self.settings_view.legacy_toggle.blockSignals(True)
                self.settings_view.legacy_toggle.setChecked(False)
                self.settings_view.legacy_toggle.setEnabled(False)
                self.settings_view.legacy_toggle.blockSignals(False)
            self.settings_view.refresh_accounts()
            if self.provider()[:2] == (source, region):
                self.library_revision += 1
                self.series = None
                self.load_cached_library()
                self.refresh()

    def sign_out(self, source, region):
        page = self.settings_view
        page.logout_action.setEnabled(False)

        def clear():
            with self.operation_lock, cli.project_lock():
                cli.logout_source(source, region)

        def cleared(_):
            if source == "dlsite":
                self.settings.setValue("legacy_comipo", False)
                self.settings_view.legacy_toggle.blockSignals(True)
                self.settings_view.legacy_toggle.setChecked(False)
                self.settings_view.legacy_toggle.setEnabled(False)
                self.settings_view.legacy_toggle.blockSignals(False)
            if self.settings_view is page:
                page.logout_action.setEnabled(True)
                page.refresh_accounts()
            if self.provider()[:2] == (source, region):
                self.library_revision += 1
                self.show_sign_in()
                self.status.setText(tr("Signed out."))

        def rejected(reason):
            if self.settings_view is page:
                page.logout_action.setEnabled(True)
                page.account_status.setText(reason)

        self.run(clear, cleared, rejected)

    def legacy_enabled(self):
        return (self.settings.value("legacy_comipo", False, type=bool)
                and session_path(cli.DATA, COMIPO).is_file()
                and session_path(cli.DATA, DLSITE).is_file())

    def set_legacy(self, value):
        self.settings.setValue("legacy_comipo", value)
        if self.provider()[0] == "dlsite":
            self.library_revision += 1
            self.refresh()

    def open_settings(self, section=None, *, account=None):
        self.pages.setCurrentIndex(1)
        self.download_toggle.hide()
        if account:
            self.settings_view.choose_account(*account[:2])
        if isinstance(section, str):
            self.settings_view.select(section)

    def show_bookshelf(self):
        self.pages.setCurrentIndex(0)
        self.download_toggle.show()

    def change_language(self, language):
        self.settings.setValue("language", language)
        LANGUAGE.set(self.locale())
        self.build(self.pages.currentIndex())
        self.apply_appearance()

    def check_calibre_at_startup(self):
        if not self.settings.value("calibre_reminder", True, type=bool):
            return
        try:
            with cli.using_calibre(self.settings.value("calibre_path", "")):
                cli.check_calibre()
        except cli.CalibreUnavailable:
            dialog = QMessageBox(self)
            dialog.setWindowTitle(tr("Calibre is missing"))
            dialog.setText(tr("Install Calibre to convert Kindle books or specify its location in Settings."))
            fetch = dialog.addButton(tr("Download Calibre"), QMessageBox.ButtonRole.ActionRole)
            configure = dialog.addButton(tr("Set Calibre path"), QMessageBox.ButtonRole.ActionRole)
            dialog.addButton(tr("Close"), QMessageBox.ButtonRole.RejectRole)
            hide = QCheckBox(tr("Do not remind me next time"))
            dialog.setCheckBox(hide)
            dialog.exec()
            if hide.isChecked():
                self.settings_view.calibre_reminder.setChecked(False)
            if dialog.clickedButton() is fetch:
                open_link(cli.CALIBRE_URL)
            elif dialog.clickedButton() is configure:
                self.open_settings("downloads")

    def apply_appearance(self, *_):
        theme = self.settings.value("theme", "system")
        dark = theme == "dark" or (theme == "system" and QApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark)
        background, panel, ink, quiet, border = (
            ("#192321", "#202d2a", "#f1f5ec", "#b6c8bc", "#40534c") if dark
            else ("#f6f7f2", "#ffffff", "#203831", "#52675e", "#cbd9ce")
        )
        hover, active = ("#2b3a35", "#344941") if dark else ("#e9efe8", "#e0eae1")
        danger = "#ffaca7" if dark else "#9c3736"
        palette = QPalette()
        for role, color in (
            (QPalette.ColorRole.Window, background), (QPalette.ColorRole.Base, panel),
            (QPalette.ColorRole.WindowText, ink), (QPalette.ColorRole.Text, ink),
            (QPalette.ColorRole.ButtonText, ink), (QPalette.ColorRole.Button, panel),
            (QPalette.ColorRole.Highlight, "#407462"), (QPalette.ColorRole.HighlightedText, "#ffffff"),
        ):
            palette.setColor(role, QColor(color))
        QApplication.setPalette(palette)
        icons = {name: action_icon(name, ink) for name in GLYPHS if name != "chevron"}
        for widget in self.findChildren(QPushButton):
            if name := widget.property("glyph"):
                widget.setIcon(action_icon(name, "#ffffff") if widget.property("primary") else icons[name])
        QApplication.instance().setStyleSheet(f"""
            QWidget {{ background: {background}; color: {ink}; font-size: 14px; }}
            QLabel#brand {{ font-size: 21px; font-weight: 700; }}
            QLabel#versionTag {{
                background: {active}; color: {quiet}; border: 1px solid {border}; border-radius: 9px;
                font-size: 11px; font-weight: 650; padding: 2px 7px;
            }}
            QWidget#settingsSidebar {{ border-right: 1px solid {border}; }}
            QScrollArea#settingsScroll {{ border: none; }}
            QWidget#settingsContent {{ background: {panel}; border-radius: 14px; }}
            QWidget#settingsContent QWidget[settingsGroup="true"],
            QWidget#settingsContent QLabel, QWidget#settingsContent QCheckBox {{ background: transparent; }}
            QWidget#settingsContent QComboBox {{ max-width: 300px; }}
            QPushButton[settingsLink="true"] {{
                text-align: left; border: 1px solid transparent; background: transparent; padding: 9px 12px;
                margin-right: 10px;
            }}
            QPushButton[settingsLink="true"]:hover {{ background: {hover}; }}
            QPushButton[settingsLink="true"]:checked {{ background: {active}; font-weight: 650; }}
            QLabel#sectionTitle {{ font-size: 22px; font-weight: 680; }}
            QLabel#pageTitle {{ font-size: 35px; font-weight: 650; }}
            QLabel#muted {{ color: {quiet}; }}
            QLabel#downloadHeading {{ font-size: 23px; font-weight: 680; }}
            QLabel#downloadTitle {{ font-size: 13px; font-weight: 650; }}
            QLabel#downloadDetail {{ color: {quiet}; font-size: 12px; }}
            QLabel#originalFormat {{ color: {quiet}; padding: 0 8px; }}
            QWidget#downloadPanel {{ border-left: 1px solid {border}; }}
            QScrollArea#downloadScroll {{ border: none; }}
            QPushButton, QLineEdit, QComboBox, QPlainTextEdit {{
                border: 1px solid {border}; border-radius: 9px; padding: 7px 12px; background: {panel};
            }}
            QPushButton:hover, QPushButton:focus, QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus {{ border-color: #588a75; }}
            QPushButton[tab="true"], QPushButton[iconOnly="true"] {{
                border: 1px solid transparent; background: transparent; padding: 6px 10px;
            }}
            QPushButton[iconOnly="true"] {{ padding: 0; }}
            QPushButton[tab="true"]:hover, QPushButton[iconOnly="true"]:hover {{ background: {hover}; }}
            QPushButton[tab="true"]:checked, QPushButton[iconOnly="true"]:checked {{ background: {active}; font-weight: 650; }}
            QPushButton[tab="true"]:focus, QPushButton[iconOnly="true"]:focus {{ border-color: #588a75; }}
            QLineEdit, QComboBox {{ min-height: 19px; }}
            QComboBox {{ padding-right: 31px; combobox-popup: 0; }}
            QComboBox::drop-down {{ border: none; background: transparent; width: 29px; }}
            QComboBox::down-arrow {{ image: none; width: 0; height: 0; }}
            QComboBox QAbstractItemView {{
                border: 1px solid {border}; background: {panel}; padding: 3px;
                selection-background-color: {active}; selection-color: {ink}; outline: none;
            }}
            QComboBox QAbstractItemView::item {{ min-height: 28px; padding: 3px 8px; }}
            QMenu {{ background: {panel}; border: 1px solid {border}; border-radius: 8px; padding: 5px; }}
            QMenu::item {{ padding: 7px 18px 7px 26px; border-radius: 5px; }}
            QMenu::item:selected {{ background: {active}; }}
            QMenu#cardMenu {{ border-radius: 12px; padding: 6px; }}
            QMenu#cardMenu::item {{ padding: 8px 18px 8px 38px; border-radius: 7px; }}
            QMenu#cardMenu::icon {{ padding-left: 9px; }}
            QScrollBar:vertical {{ border: none; background: transparent; width: 8px; margin: 3px 0; }}
            QScrollBar::handle:vertical {{ background: {border}; border-radius: 4px; min-height: 30px; }}
            QScrollBar::handle:vertical:hover {{ background: #588a75; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
            QToolTip {{ background: {panel}; color: {ink}; border: 1px solid {border}; padding: 5px; }}
            QPushButton[primary="true"] {{ background: #346553; color: #ffffff; font-weight: 650; }}
            QPushButton[danger="true"] {{ color: {danger}; }}
            QPushButton:disabled {{ color: {quiet}; }}
            QListWidget, QTextBrowser {{ border: none; background: {background}; }}
            QListWidget::item {{ padding: 7px; border-radius: 10px; }}
            QListWidget::item:selected {{ background: #346553; color: #ffffff; }}
            QProgressBar#downloadProgress {{ border: none; border-radius: 3px; background: {active}; }}
            QProgressBar#downloadProgress::chunk {{ background: #588a75; border-radius: 3px; }}
        """)

    def present_update(self):
        state = self.update_stage
        release = self.release
        active = state in {"checking", "downloading", "cancelling", "waiting", "installing"}
        self.check_update_button.setEnabled(not active)
        detail = {"checking": "Checking for updates…", "current": "You are using the latest version.",
                  "downloading": "Downloading update…", "cancelling": "Cancelling…",
                  "waiting": "The update is ready. It will install after your book downloads finish.",
                  "installing": "Restarting to install the update…",
                  "manual": "Update downloaded. Install it manually from the saved package."}.get(state, "")
        text = tr(detail) if detail else ""
        if state == "available" and release:
            text = tr("Version {version} is available.", version=release.tag)
            if not getattr(sys, "frozen", False):
                text += "\n" + tr('For a pip installation, run python -m pip install --upgrade "kindlegarten[gui]".')
            if not release.asset:
                text += "\n" + tr("A verifiable update package is not available.")
        if self.update_error:
            text += ("\n" if text else "") + self.update_error
        self.update_status.setText(text)
        self.update_progress.setVisible(state in {"downloading", "cancelling", "waiting", "installing"})
        self.update_progress.setValue(self.update_percent)
        self.update_button.setVisible(bool(release and release.asset and state == "available"))
        self.update_button.setText(tr("Update and restart" if self.installation_for_update() else "Download update"))
        self.cancel_update_button.setVisible(state in {"downloading", "waiting"})
        self.update_page_button.setVisible(release is not None and not active)
        self.update_folder_button.setVisible(self.update_file is not None and not active)

    def installation_for_update(self):
        try:
            installation = installed_package()
            if not can_install(installation):
                return None
            target = installation.target.resolve()
            if any(path.resolve().is_relative_to(target) for path in self.protected_update_paths()):
                return None
            return installation
        except (OSError, RuntimeError):
            return None

    def protected_update_paths(self):
        yield cli.DATA
        if self.location.text().strip():
            yield Path(self.location.text().strip()).expanduser()
        yield from (task.request.directory for task in self.downloads)

    def check_updates(self):
        self.release = None
        self.update_file = None
        self.update_error = ""
        self.update_stage = "checking"
        self.present_update()

        def checked(release):
            self.release = release
            self.update_stage = "available" if release else "current"
            self.present_update()

        def failed(_):
            self.update_stage = ""
            self.update_error = tr("Could not check for updates.")
            self.present_update()

        self.run(check_for_update, checked, failed)

    def begin_update(self):
        release = self.release
        installation = self.installation_for_update()
        downloads = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation)
        directory = Path(downloads or Path.home() / "Downloads") / "Kindlegarten updates"
        stop = self.update_cancelled = Event()
        self.update_percent = 0
        self.update_stage = "downloading"
        self.update_error = ""
        self.present_update()

        def transfer():
            path = download_update(release, directory, stop, PROGRESS.get())
            job = None
            warning = ""
            if installation and not stop.is_set():
                try:
                    job = stage_update(path, installation, release)
                except ManualInstall:
                    pass
                except (OSError, ValueError, RuntimeError):
                    warning = tr("The update could not be prepared for automatic installation.")
            return path, job, warning, installation

        def progress(percent, _):
            self.update_percent = percent
            self.update_progress.setValue(percent)

        def downloaded(result):
            self.update_file, job, self.update_error, installation = result
            self.update_cancelled = None
            self.update_stage = "manual"
            if job and not stop.is_set():
                self.update_job = (job, installation)
                self.update_stage = "waiting"
                self.update_timer.start()
            elif job:
                shutil.rmtree(job.parent, ignore_errors=True)
            self.present_update()

        def failed(message=""):
            self.update_cancelled = None
            self.update_stage = "available"
            self.update_error = message
            self.present_update()

        self.run(transfer, downloaded, failed, progress=progress,
                 cancelled=lambda: failed(tr("Cancelled.")))

    def cancel_update(self):
        if self.update_cancelled is not None:
            self.update_cancelled.set()
            self.update_stage = "cancelling"
        elif self.update_job:
            job, _ = self.update_job
            self.update_job = None
            self.update_timer.stop()
            shutil.rmtree(job.parent, ignore_errors=True)
            self.update_stage = "manual"
        self.present_update()

    def show_update_folder(self):
        if self.update_file:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.update_file.parent)))

    def maybe_install_update(self):
        if not self.update_job or self.unfinished_downloads() or QApplication.activeModalWidget():
            return
        self.update_timer.stop()
        job, installation = self.update_job
        self.update_job = None
        if self.installation_for_update() != installation:
            shutil.rmtree(job.parent, ignore_errors=True)
            self.update_stage = "manual"
            self.present_update()
            return
        try:
            start_installer(job)
        except OSError:
            shutil.rmtree(job.parent, ignore_errors=True)
            self.update_stage = "manual"
            self.update_error = tr("The update could not be prepared for automatic installation.")
            self.present_update()
            return
        self.update_stage = "installing"
        self.present_update()
        self.close()

    def closeEvent(self, event):
        if self.enqueueing or any(task.state in {"queued", "running", "cancelling"} for task in self.downloads):
            self.set_downloads_visible(True)
            self.show_download_notice("Wait for the current download to finish before closing.")
            event.ignore()
            return
        if self.update_cancelled is not None or self.update_job is not None:
            self.cancel_update()
        if self.jobs:
            self.pool.clear()
        super().closeEvent(event)


def entrypoint():
    os.umask(0o077)
    if getattr(sys, "frozen", False):
        for name in list(os.environ):
            if name.startswith("KINDL_"):
                del os.environ[name]
        os.environ["KINDL_PROFILE"] = "ios-ipad"
    cli.initialize()
    app = QApplication(sys.argv)
    smoke = "--smoke-test" in sys.argv
    if smoke:
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(cli.DATA))
    app.setApplicationName("Kindlegarten")
    app.setApplicationVersion(__version__)
    app.setWindowIcon(QIcon(str(APP_ICON)))
    window = Window()
    LANGUAGE.set(window.locale())
    window.show()
    if smoke:
        from kindl.client import AmazonDeviceClient
        from dlsite_async.play.epub import EpubReflowableSession
        from PIL import Image
        from playwright.sync_api import sync_playwright
        import img2pdf

        assert all((AmazonDeviceClient, EpubReflowableSession, Image, img2pdf))
        with sync_playwright() as playwright:
            assert playwright.chromium.name == "chromium"
        app.processEvents()
        flag = os.environ.get("KINDLEGARTEN_SMOKE_FILE")
        if flag:
            Path(flag).touch()
        return 0
    if ready := os.environ.pop(READY_VARIABLE, ""):
        try:
            with Path(ready).open("x", encoding="utf-8") as marker:
                marker.write(__version__)
        except OSError:
            return 1
    if os.environ.pop(RESULT_VARIABLE, ""):
        QTimer.singleShot(0, lambda: QMessageBox.warning(window, tr("Update failed"),
                          tr("The updated app could not start. Your previous installation is available.")))
    QTimer.singleShot(0, lambda: window.refresh(silent=True))
    QTimer.singleShot(0, window.check_calibre_at_startup)
    return app.exec()


if __name__ == "__main__":
    sys.exit(entrypoint())

"""The native Kindlegarten desktop application."""

import argparse
import json
import sys
from functools import partial
from pathlib import Path
from threading import Lock

from PySide6.QtCore import QCollator, QEvent, QPersistentModelIndex, QRect, QRectF, QPointF, QLocale, QObject, QRunnable, QSettings, QSize, Qt, QThreadPool, QTimer, QUrl, Signal
from PySide6.QtGui import QActionGroup, QColor, QDesktopServices, QIcon, QPainter, QPainterPath, QPalette, QPen, QPixmap, QPolygonF, QTextLayout
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QListView, QListWidget, QListWidgetItem, QMainWindow, QMenu, QProgressBar, QPushButton,
    QStackedWidget, QStyle, QStyledItemDelegate, QTextBrowser, QVBoxLayout, QWidget,
)

from kindlegarten import __version__, cli
from kindlegarten.browser import open_amazon_login
from kindlegarten.catalog_art import cached_cover, cover
from kindlegarten.comipo import COMIPO, DLSITE, import_session, login, saved_account, session_path
from kindlegarten.formats import FORMATS
from kindlegarten.i18n import LANGUAGE, SUPPORTED, tr
from kindlegarten.library import Series, cache_library, cached_library
from kindlegarten.marketplaces import MARKETPLACES
from kindlegarten.network import proxy_address, using_proxy
from kindlegarten.progress import PROGRESS, STATUS, report_book
from kindlegarten.updates import REPOSITORY, check_for_update

ROLE = Qt.ItemDataRole.UserRole
APP_ICON = Path(__file__).with_name("app-icon.png")
PROVIDERS = {"amazon": "Kindle", "comipo": "Comipo", "dlsite": "DLsite"}
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
    ("Paste credentials", "Amazon uses the final redirect URL. Comipo and DLsite use the Cookie request header from Play or a session JSON file."),
    ("Your bookshelf", "Choose a source. For Kindle, choose your store. Open a series to select individual volumes, or select the whole series."),
    ("Export", "Choose a folder and a format. Auto picks EPUB for text and CBZ for comics. ZIP holds the same comic page archive as CBZ."),
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
    "folder": (((3, 19), (3, 5), (9, 5), (12, 8), (21, 8), (21, 19), (3, 19)),),
    "open-folder": (((3, 19), (3, 5), (9, 5), (12, 8), (20, 8), (20, 11)),
                    ((3, 19), (7, 11), (22, 11), (19, 19), (3, 19))),
    "filter": (((3, 5), (21, 5), (14, 13), (14, 18), (10, 20), (10, 13), (3, 5)),),
    "chevron": (((6, 9), (12, 15), (18, 9)),),
    "user": (),
    "refresh": (((20, 3), (20, 9), (14, 9)), ((4, 21), (4, 15), (10, 15))),
    "settings": (((4, 7), (9, 7)), ((13, 7), (20, 7)), ((9, 4), (9, 10)),
                 ((4, 17), (14, 17)), ((18, 17), (20, 17)), ((14, 14), (14, 20))),
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

    def __init__(self):
        super().__init__()
        self.setMouseTracking(True)
        self.hovered = QPersistentModelIndex()
        self.pressed = QPersistentModelIndex()

    def set_hovered(self, index=None):
        hovered = QPersistentModelIndex(index) if index is not None else QPersistentModelIndex()
        if self.hovered != hovered:
            self.hovered = hovered
            self.viewport().update()

    def mouseMoveEvent(self, event):
        self.set_hovered(self.indexAt(event.position().toPoint()))
        super().mouseMoveEvent(event)

    def viewportEvent(self, event):
        if event.type() == QEvent.Type.Leave:
            self.set_hovered()
        return super().viewportEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and not event.modifiers():
            self.pressed = QPersistentModelIndex(self.indexAt(event.position().toPoint()))
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        index = self.indexAt(event.position().toPoint())
        activate = (event.button() == Qt.MouseButton.LeftButton and not event.modifiers()
                    and index.isValid() and self.pressed == index)
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


class BookTile(QStyledItemDelegate):
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
        front = image
        if isinstance(book, Series) and len(book.books) > 1:
            layers = min(len(book.books), 3)
            front = image.adjusted(-2, 5, -9, -3)
            for depth in range(layers - 1, 0, -1):
                sheet = front.translated(7 * depth, -3 * depth)
                painter.setPen(QPen(palette.color(QPalette.ColorRole.Base), 1))
                painter.setBrush(palette.color(QPalette.ColorRole.Highlight).lighter(115 + depth * 16))
                painter.drawRoundedRect(QRectF(sheet), 5, 5)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(palette.color(QPalette.ColorRole.Base))
            painter.drawRoundedRect(QRectF(front), 4, 4)
        icon.paint(painter, front, Qt.AlignmentFlag.AlignCenter)
        if self.parent().hovered == index:
            center = QPointF(front.center())
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(17, 40, 34, 170))
            painter.drawEllipse(center, 28, 28)
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
            caption = index.data(ROLE + 1)
            color.setAlpha(175)
            painter.setPen(color)
            painter.drawText(QRect(bounds.left() + 8, round(baseline + offset) + 3, bounds.width() - 16, 20),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, caption)
        painter.restore()


class SettingsDialog(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.setWindowTitle(tr("Settings"))
        form = QFormLayout(self)
        form.setContentsMargins(24, 22, 24, 22)
        form.setSpacing(11)
        self.locale = MenuField()
        for code, name in (("", "System language"), ("zh", "简体中文"), ("en", "English"), ("ja", "日本語")):
            self.locale.addItem(tr(name), code)
        self.locale.setCurrentIndex(self.locale.findData(window.settings.value("language", "")))
        self.theme = MenuField()
        for key, name in (("system", "System appearance"), ("light", "Light"), ("dark", "Dark")):
            self.theme.addItem(tr(name), key)
        self.theme.setCurrentIndex(self.theme.findData(window.settings.value("theme", "system")))
        self.mode = MenuField()
        for key, name in (("system", "System proxy"), ("direct", "Direct connection"), ("custom", "Custom proxy")):
            self.mode.addItem(tr(name), key)
        self.mode.setCurrentIndex(self.mode.findData(window.settings.value("proxy_mode", "system")))
        self.address = QLineEdit(window.settings.value("proxy_address", ""))
        self.address.setPlaceholderText("http://127.0.0.1:7890")
        self.username = QLineEdit(window.settings.value("proxy_username", ""))
        self.password = QLineEdit(window.proxy_password)
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.status = QLabel()
        self.status.setWordWrap(True)
        form.addRow(tr("Language"), self.locale)
        form.addRow(tr("Appearance"), self.theme)
        form.addRow(tr("Network"), self.mode)
        form.addRow(tr("Proxy address"), self.address)
        form.addRow(tr("Proxy username"), self.username)
        form.addRow(tr("Proxy password"), self.password)
        form.addRow(self.status)
        form.addRow(button("Save settings", self.apply, primary=True))
        self.resize(440, self.sizeHint().height())

    def apply(self):
        from urllib.parse import quote, urlsplit

        mode = self.mode.currentData()
        address = self.address.text().strip()
        if mode == "custom":
            if not address:
                self.status.setText(tr("Enter a valid proxy address."))
                return
            try:
                url = urlsplit(address)
                if url.username or url.password:
                    raise ValueError
                username, password = self.username.text(), self.password.text()
                auth = f"{quote(username, safe='')}:{quote(password, safe='')}@" if username else ""
                with using_proxy(f"{url.scheme}://{auth}{url.netloc}"):
                    proxy_address()
            except ValueError:
                self.status.setText(tr("Enter a valid proxy address."))
                return
        for key, value in (
            ("language", self.locale.currentData()), ("theme", self.theme.currentData()),
            ("proxy_mode", mode), ("proxy_address", address), ("proxy_username", self.username.text()),
        ):
            self.window.settings.setValue(key, value)
        self.window.proxy_password = self.password.text()
        self.accept()


class LoginDialog(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.source, self.region, name = window.provider()
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
        self.paste.hide()
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
                    if state.is_file():
                        cli.setup_worker_runtime()
                        if json.loads(cli.run_worker("_validate_login", state, capture=True)):
                            return
                    authorization = self.challenge()
                    profile = cli.DATA / "browser" / f"amazon-{self.region}"
                    callback = open_amazon_login(authorization["url"], authorization["site"], cli.COMMAND,
                                                 profile=profile)
                    return self.save_amazon(authorization, callback)
                return login(cli.DATA, cli.COMMAND, site=COMIPO if self.source == "comipo" else DLSITE,
                             interactive=False)
        self.call(sign_in)

    def import_file(self):
        name, _ = QFileDialog.getOpenFileName(self, tr("Import session file"), "", "JSON (*.json)")
        if name:
            self.import_play(Path(name), None)

    def import_play(self, path, text):
        def save():
            with self.window.operation_lock, cli.project_lock():
                import_session(cli.DATA, cli.COMMAND, path, site=COMIPO if self.source == "comipo" else DLSITE,
                               pasted=text)
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


class AccountDialog(QDialog):
    def __init__(self, window, provider, profile, warning=None):
        super().__init__(window)
        self.setWindowTitle(tr("Account"))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(14)
        layout.addWidget(heading(provider, "pageTitle"))
        layout.addWidget(heading(warning or "Signed in", "muted"))

        details = QFormLayout()
        details.setSpacing(9)
        for key, title in (("login", "Login ID"), ("id", "Account ID")):
            if value := profile.get(key):
                label = heading("", "")
                label.setText(value)
                label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                details.addRow(tr(title), label)
        if details.rowCount():
            layout.addLayout(details)

        actions = QHBoxLayout()
        actions.addStretch()
        dismiss = button("Close", self.reject)
        dismiss.setDefault(True)
        actions.addWidget(dismiss)
        logout = button("Sign out", self.accept)
        logout.setAutoDefault(False)
        logout.setProperty("danger", True)
        actions.addWidget(logout)
        layout.addLayout(actions)
        self.setMinimumWidth(350)


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings("Kindlegarten", "Kindlegarten")
        self.proxy_password = ""
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(4)
        self.operation_lock = Lock()
        self.jobs = set()
        self.library = None
        self.series = None
        self.needs_sign_in = False
        self.busy = False
        self.refreshing = None
        self.pending_refresh = None
        self.covers_requested = set()
        self.cover_misses = set()
        self.library_revision = 0
        self.resize(1040, 760)
        self.setMinimumSize(700, 530)
        self.setWindowTitle("Kindlegarten")
        self.setWindowIcon(QIcon(str(APP_ICON)))
        self.build()
        self.load_cached_library()
        self.apply_appearance()
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

    def run(self, action, success=None, failure=None, progress=None, status=None, *, priority=0):
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
        job.signals.finished.connect(lambda: self.jobs.discard(job), Qt.ConnectionType.QueuedConnection)
        self.pool.start(job, priority)

    def build(self, page=0):
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
            nav.addSpacing(14)
            tabs = QButtonGroup(shell)
            for index, title in enumerate(("Bookshelf", "Guide", "About")):
                tab = button(title, lambda _, i=index: self.pages.setCurrentIndex(i))
                tab.setProperty("tab", True)
                tab.setCheckable(True)
                tabs.addButton(tab, index)
                nav.addWidget(tab)
            nav.addStretch()
            nav.addWidget(icon_button("Settings", "settings", self.open_settings))
            outer.addLayout(nav)

            self.pages = QStackedWidget()
            self.pages.addWidget(self.bookshelf())
            self.pages.addWidget(self.guide())
            self.pages.addWidget(self.about())
            self.pages.setCurrentIndex(page)
            tabs.button(page).setChecked(True)
            self.pages.currentChanged.connect(lambda index: tabs.button(index).setChecked(True))
            outer.addWidget(self.pages)
            self.setCentralWidget(shell)
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
        source = self.settings.value("source", old_source)
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
        self.status = heading("Choose a source and refresh to see your purchases.", "muted")
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
        self.grid.verticalScrollBar().valueChanged.connect(self.queue_covers)
        self.shelf_stack = QStackedWidget()
        self.shelf_stack.addWidget(self.grid)
        sign_in_page = QWidget()
        sign_in_layout = QVBoxLayout(sign_in_page)
        sign_in_layout.setContentsMargins(0, 0, 0, 0)
        self.shelf_login = button("Sign in", self.open_login, primary=True)
        self.shelf_login.setMinimumWidth(120)
        sign_in_layout.addWidget(self.shelf_login, alignment=Qt.AlignmentFlag.AlignCenter)
        self.shelf_stack.addWidget(sign_in_page)
        layout.addWidget(self.shelf_stack, 1)
        footer = QHBoxLayout()
        self.location = QLineEdit(self.settings.value("destination", str(Path.home() / "Downloads" / "Kindlegarten")))
        self.location.setPlaceholderText(tr("Destination folder"))
        self.location.setAccessibleName(tr("Destination folder"))
        footer.addWidget(self.location, 1)
        footer.addWidget(icon_button("Choose folder", "folder", self.choose_folder))
        self.format = MenuField()
        self.format.setAccessibleName(tr("Format"))
        self.format.setMinimumWidth(92)
        for option in FORMATS:
            self.format.addItem(tr("Auto") if option == "auto" else option.upper(), option)
        footer.addWidget(self.format)
        self.download_button = button("Download selected", lambda: self.download(), primary=True)
        self.download_button.setProperty("glyph", "download")
        self.download_button.setIconSize(QSize(17, 17))
        footer.addWidget(self.download_button)
        layout.addLayout(footer)
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(True)
        self.progress_bar.hide()
        layout.addWidget(self.progress_bar)
        self.render_library()
        return page

    def guide(self):
        browser = QTextBrowser()
        browser.setOpenExternalLinks(True)
        from html import escape
        entries = "".join(f"<h3>{escape(tr(title))}</h3><p>{escape(tr(text))}</p>" for title, text in GUIDE)
        browser.setHtml(f"<h1>{tr('Getting started')}</h1>{entries}")
        return browser

    def about(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addStretch(1)
        for widget in (
            heading("Kindlegarten", "pageTitle"),
            heading(tr("Version {version}", version=__version__), "muted"),
            button("GitHub repository", lambda: open_link(REPOSITORY)),
            button("Check for updates", self.check_updates, primary=True),
        ):
            layout.addWidget(widget, alignment=Qt.AlignmentFlag.AlignHCenter)
        self.update_status = heading("", "muted")
        layout.addWidget(self.update_status, alignment=Qt.AlignmentFlag.AlignHCenter)
        self.update_url = ""
        self.update_button = button("Open release", lambda: open_link(self.update_url))
        self.update_button.hide()
        layout.addWidget(self.update_button, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch(2)
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
        self.load_cached_library()
        self.refresh(silent=True)

    def saved_session_path(self, source, region):
        if source == "amazon":
            return cli.account_path(region)
        return session_path(cli.DATA, COMIPO if source == "comipo" else DLSITE)

    def load_cached_library(self):
        source, region, _ = self.provider()
        self.needs_sign_in = not self.saved_session_path(source, region).is_file()
        self.library = None if self.needs_sign_in else cached_library(cli.DATA, source, region)
        self.status.clear()
        self.render_library()

    def show_sign_in(self):
        self.needs_sign_in = True
        self.library = None
        self.series = None
        self.status.clear()
        self.render_library()

    def refresh(self, *, silent=False):
        if self.busy:
            return
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
            self.status.setText(tr("The service did not return the complete library.") if not snapshot.complete
                                else tr("No purchased books found.") if not snapshot.series and not snapshot.books else "")
            self.queue_covers()

        def rejected(message):
            if current() and not quiet:
                self.status.setText(message)

        def load():
            with self.operation_lock, cli.project_lock():
                try:
                    snapshot = cli.read_library(source, region)
                except cli.SignInRequired:
                    return None
                if snapshot.complete and snapshot != cached_library(cli.DATA, source, region):
                    try:
                        cache_library(cli.DATA, source, region, snapshot)
                    except OSError:
                        # The live library remains usable when the cache cannot be written.
                        pass
                return snapshot
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
        self.grid.set_hovered()
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
            tile.setData(ROLE + 1, caption)
            tile.setToolTip(f"{entry.title} · {entry.id}")
            tile.setData(ROLE, entry)
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
        item.setData(ROLE + 2, True)

    def queue_covers(self, *_):
        if not hasattr(self, "grid") or self.busy:
            return
        viewport = self.grid.viewport().rect()
        source, region, _ = self.provider()
        for index in range(self.grid.count()):
            item = self.grid.item(index)
            if item.data(ROLE + 2) or not self.grid.visualItemRect(item).intersects(viewport):
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
        if self.busy:
            return
        entry = item.data(ROLE)
        if isinstance(entry, Series):
            self.series = entry
            self.search.clear()
            self.render_library()
        else:
            self.download((entry,))

    def leave_series(self):
        self.series = None
        self.search.clear()
        self.render_library()

    def choose_folder(self):
        path = QFileDialog.getExistingDirectory(self, tr("Choose folder"), self.location.text())
        if path:
            self.location.setText(path)
            self.settings.setValue("destination", path)

    def prepare_kindle(self):
        if not cli.setup_ready():
            cli.setup(None)

    def download(self, entries=None):
        if self.busy:
            return
        if entries is None:
            entries = [item.data(ROLE) for item in self.grid.selectedItems()]
        if not entries:
            self.status.setText(tr("Select at least one book or series."))
            return
        folder = Path(self.location.text()).expanduser().absolute()
        if not self.location.text().strip():
            self.status.setText(tr("Choose a destination folder first."))
            return
        source, region, _ = self.provider()
        requested = self.format.currentData()
        self.settings.setValue("destination", str(folder))
        self.busy = True
        self.source.setEnabled(False)
        self.marketplace.setEnabled(False)
        self.download_button.setEnabled(False)
        self.progress_bar.setValue(0)
        self.progress_bar.show()
        self.status.setText(tr("Preparing download…"))
        def task():
            failures = []
            with self.operation_lock, cli.project_lock():
                if source == "amazon":
                    self.prepare_kindle()
                for entry in entries:
                    report_book(entry.title)
                    kind = "series" if isinstance(entry, Series) else "work"
                    args = argparse.Namespace(kind=kind, identifier=entry.id, source=source,
                                              region=region if source == "amazon" else None,
                                              format=requested, output=folder if kind == "series" else None,
                                              books=folder, resume=True)
                    try:
                        cli.download_selection(args)
                    except (cli.KindleError, OSError) as error:
                        failures.append(f"{entry.title}: {error}")
            return failures
        def done(failures):
            self.busy = False
            self.source.setEnabled(True)
            self.marketplace.setEnabled(True)
            self.download_button.setEnabled(True)
            self.progress_bar.hide()
            self.status.setText("\n".join(failures) if failures else tr("Downloads saved."))
        self.run(task, done, lambda reason: done([reason]), self.progress,
                 lambda title: self.status.setText(tr("Downloading {title}…", title=title)))

    def progress(self, percent, speed):
        self.progress_bar.setValue(percent)
        self.progress_bar.setFormat(f"%p%   {speed}")

    def open_account(self):
        if self.busy:
            return
        source, region, provider = selection = self.provider()
        site = COMIPO if source == "comipo" else DLSITE
        state = self.saved_session_path(source, region)
        if not state.is_file():
            self.show_sign_in()
            self.open_login()
            return

        account_button = self.account_button
        account_button.setEnabled(False)

        def load():
            if source == "amazon":
                cli.setup_worker_runtime()
                return json.loads(cli.run_worker("_account", state, capture=True))
            return saved_account(cli.DATA, cli.COMMAND, site)

        def active():
            if account_button is not self.account_button:
                return False
            account_button.setEnabled(True)
            return self.provider() == selection and not self.busy

        def opened(profile, warning=None):
            if not active():
                return
            if profile is None:
                self.show_sign_in()
                self.open_login()
            elif AccountDialog(self, provider, profile, warning).exec() == QDialog.DialogCode.Accepted:
                self.sign_out(source, region)

        def rejected(reason):
            opened({}, reason)

        self.run(load, opened, rejected)

    def open_login(self):
        if self.busy:
            return
        dialog = LoginDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.library_revision += 1
            self.series = None
            self.load_cached_library()
            self.refresh()

    def sign_out(self, source, region):
        account_button = self.account_button
        account_button.setEnabled(False)

        def clear():
            with self.operation_lock, cli.project_lock():
                cli.logout_source(source, region)

        def active():
            if account_button is self.account_button:
                account_button.setEnabled(True)
            return self.provider()[:2] == (source, region)

        def cleared(_):
            if not active():
                return
            self.library_revision += 1
            self.show_sign_in()
            self.status.setText(tr("Signed out."))

        def rejected(reason):
            if active():
                self.status.setText(reason)

        self.run(clear, cleared, rejected)

    def open_settings(self):
        if self.busy:
            return
        if SettingsDialog(self).exec() == QDialog.DialogCode.Accepted:
            LANGUAGE.set(self.locale())
            query, location = self.search.text(), self.location.text()
            output_format = self.format.currentData()
            self.build(self.pages.currentIndex())
            self.search.setText(query)
            self.location.setText(location)
            self.format.setCurrentIndex(self.format.findData(output_format))
            self.apply_appearance()

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
        icons = {name: action_icon(name, "#ffffff" if name == "download" else ink) for name in GLYPHS if name != "chevron"}
        for widget in self.findChildren(QPushButton):
            if name := widget.property("glyph"):
                widget.setIcon(icons[name])
        QApplication.instance().setStyleSheet(f"""
            QWidget {{ background: {background}; color: {ink}; font-size: 14px; }}
            QLabel#brand {{ font-size: 21px; font-weight: 700; }}
            QLabel#pageTitle {{ font-size: 35px; font-weight: 650; }}
            QLabel#muted {{ color: {quiet}; }}
            QPushButton, QLineEdit, QComboBox {{
                border: 1px solid {border}; border-radius: 9px; padding: 7px 12px; background: {panel};
            }}
            QPushButton:hover, QPushButton:focus, QLineEdit:focus, QComboBox:focus {{ border-color: #588a75; }}
            QPushButton[tab="true"], QPushButton[iconOnly="true"] {{
                border: 1px solid transparent; background: transparent; padding: 6px 10px;
            }}
            QPushButton[iconOnly="true"] {{ padding: 0; }}
            QPushButton[tab="true"]:hover, QPushButton[iconOnly="true"]:hover {{ background: {hover}; }}
            QPushButton[tab="true"]:checked {{ background: {active}; font-weight: 650; }}
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
            QProgressBar {{ border: 1px solid {border}; border-radius: 6px; text-align: center; }}
            QProgressBar::chunk {{ background: #588a75; }}
        """)

    def check_updates(self):
        self.update_status.setText(tr("Checking for updates…"))
        self.update_button.hide()
        def checked(release):
            if not release:
                self.update_status.setText(tr("You are using the latest version."))
                return
            version, url = release
            self.update_status.setText(tr("Version {version} is available.", version=version))
            self.update_url = url
            self.update_button.show()
        self.run(check_for_update, checked, lambda _: self.update_status.setText(tr("Could not check for updates.")))

    def closeEvent(self, event):
        if self.busy:
            self.status.setText(tr("Wait for the current download to finish before closing."))
            event.ignore()
        elif self.jobs:
            self.pool.clear()
            event.accept()
        else:
            super().closeEvent(event)


def entrypoint():
    import os

    os.umask(0o077)
    if getattr(sys, "frozen", False):
        os.environ["KINDL_PROFILE"] = "ios-ipad"
    cli.initialize()
    app = QApplication(sys.argv)
    app.setApplicationName("Kindlegarten")
    app.setApplicationVersion(__version__)
    app.setWindowIcon(QIcon(str(APP_ICON)))
    window = Window()
    LANGUAGE.set(window.locale())
    window.show()
    if "--smoke-test" in sys.argv:
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
    QTimer.singleShot(0, lambda: window.refresh(silent=True))
    return app.exec()


if __name__ == "__main__":
    sys.exit(entrypoint())

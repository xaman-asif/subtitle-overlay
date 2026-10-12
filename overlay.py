#!/usr/bin/env python3
"""Transparent subtitle overlay — standalone audio player with .txt subtitle sync.
Features: MPRIS2 (KDE media controls), UI toggle (H key / tray), copy on pause.
"""
import json
import os
import re
import sys
from pathlib import Path

# On Linux Wayland (e.g. KDE Plasma), prefer xcb (XWayland) if available so window
# position can be set and remembered; fall back to wayland otherwise.
os.environ.setdefault('QT_QPA_PLATFORM', 'xcb;wayland')

import dbus
import dbus.service
import dbus.mainloop.glib
import dbus.connection
from PyQt6.QtCore import QRect, QTimer, QUrl, Qt
from PyQt6.QtGui import (QClipboard, QColor, QFont, QFontMetrics, QIcon, QPainter,
                         QKeySequence, QPalette, QPixmap, QShortcut)
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
from PyQt6.QtWidgets import (QApplication, QFileDialog, QHBoxLayout, QLabel,
                              QListWidget, QMenu, QPushButton, QSlider,
                              QSystemTrayIcon, QVBoxLayout, QWidget,
                              QSizePolicy, QStyle, QStyledItemDelegate,
                              QStyleOptionViewItem)

POLL_MS = 200
MARQUEE_MS = 30   # marquee step interval (1px per step)
SEEK_S  = 15
MPRIS_SERVICE = 'org.mpris.MediaPlayer2.subtitleoverlay'
MPRIS_PATH    = '/org/mpris/MediaPlayer2'
MPRIS_PLAYER  = 'org.mpris.MediaPlayer2.Player'
MPRIS_IFACE   = 'org.mpris.MediaPlayer2'
PROPS_IFACE   = 'org.freedesktop.DBus.Properties'

CONFIG_DIR = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'subtitle-overlay'
STATE_FILE = CONFIG_DIR / 'state.json'


def load_state_file(path: Path | None = None) -> dict:
    target = STATE_FILE if path is None else path
    if not target.exists():
        return {}
    try:
        return json.loads(target.read_text(encoding='utf-8'))
    except Exception:
        return {}


def save_state_file(state: dict, path: Path | None = None) -> bool:
    target = STATE_FILE if path is None else path
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix('.tmp')
        tmp.write_text(json.dumps(state, indent=2), encoding='utf-8')
        tmp.replace(target)
        return True
    except Exception:
        return False

# ── subtitle parser ───────────────────────────────────────────────────────────
_TS = re.compile(r'^(?:(\d+):)?(\d{2}):(\d{2})\.(\d{3})$')

def _ms(ts: str) -> int:
    m = _TS.match(ts.strip())
    if not m:
        raise ValueError(ts)
    h, mn, s, ms = (int(x or 0) for x in m.groups())
    return ((h * 60 + mn) * 60 + s) * 1000 + ms

def load_txt(path: Path) -> list[tuple[int, int, str]]:
    blocks = re.split(r'\n\s*\n', path.read_text(encoding='utf-8-sig').strip())
    cues = []
    for block in blocks:
        lines = block.strip().splitlines()
        if len(lines) < 2:
            continue
        parts = lines[0].split('-->')
        if len(parts) != 2:
            continue
        try:
            start, end = _ms(parts[0]), _ms(parts[1])
        except ValueError:
            continue
        cues.append((start, end, ' '.join(l.strip() for l in lines[1:] if l.strip())))
    return cues

def cue_at(cues: list, pos_ms: int) -> str:
    for start, end, text in cues:
        if start <= pos_ms < end:
            return text
    return ''


def fit_font_size(text: str, target_w: int, target_h: int, min_size: int = 8, max_size: int = 28) -> int:
    if not text.strip():
        return max_size
    flags = int(Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignCenter)
    low, high = min_size, max_size
    best = min_size
    while low <= high:
        mid = (low + high) // 2
        f = QFont()
        f.setPixelSize(mid)
        f.setBold(True)
        fm = QFontMetrics(f)
        rect = fm.boundingRect(QRect(0, 0, target_w, 0), flags, text)
        if rect.width() <= target_w and rect.height() <= target_h:
            best = mid
            low = mid + 1
        else:
            high = mid - 1
    return best

# ── MPRIS2 DBus service ───────────────────────────────────────────────────────
class MprisService(dbus.service.Object):
    def __init__(self, overlay):
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        self._bus = dbus.SessionBus()
        self._bus.request_name(MPRIS_SERVICE)
        super().__init__(self._bus, MPRIS_PATH)
        self._ov = overlay

    def _props(self):
        ov = self._ov
        s = ov._player.playbackState()
        if s == QMediaPlayer.PlaybackState.PlayingState:   status = 'Playing'
        elif s == QMediaPlayer.PlaybackState.PausedState:  status = 'Paused'
        else:                                               status = 'Stopped'
        idx = ov._current_index
        if 0 <= idx < len(ov._playlist):
            p = ov._playlist[idx]
            meta = dbus.Dictionary({
                'mpris:trackid': dbus.ObjectPath(f'/track/{idx}'),
                'xesam:title':   dbus.String(p.stem),
                'xesam:url':     dbus.String(QUrl.fromLocalFile(str(p)).toString()),
            }, signature='sv')
        else:
            meta = dbus.Dictionary({
                'mpris:trackid': dbus.ObjectPath('/org/mpris/MediaPlayer2/TrackList/NoTrack'),
            }, signature='sv')
        return {
            MPRIS_IFACE: dbus.Dictionary({
                'CanQuit':            dbus.Boolean(True),
                'CanRaise':           dbus.Boolean(True),
                'HasTrackList':       dbus.Boolean(False),
                'Identity':           dbus.String('Subtitle Overlay'),
                'SupportedUriSchemes':dbus.Array(['file'], signature='s'),
                'SupportedMimeTypes': dbus.Array(['audio/mpeg','audio/ogg','audio/flac'], signature='s'),
            }, signature='sv'),
            MPRIS_PLAYER: dbus.Dictionary({
                'PlaybackStatus': dbus.String(status),
                'LoopStatus':     dbus.String('None'),
                'Rate':           dbus.Double(1.0),
                'Shuffle':        dbus.Boolean(False),
                'Metadata':       meta,
                'Volume':         dbus.Double(ov._audio.volume()),
                'Position':       dbus.Int64(ov._player.position() * 1000),
                'MinimumRate':    dbus.Double(1.0),
                'MaximumRate':    dbus.Double(1.0),
                'CanGoNext':      dbus.Boolean(idx + 1 < len(ov._playlist)),
                'CanGoPrevious':  dbus.Boolean(idx > 0),
                'CanPlay':        dbus.Boolean(bool(ov._playlist)),
                'CanPause':       dbus.Boolean(bool(ov._playlist)),
                'CanSeek':        dbus.Boolean(True),
                'CanControl':     dbus.Boolean(True),
            }, signature='sv'),
        }

    # org.freedesktop.DBus.Properties
    @dbus.service.method(PROPS_IFACE, in_signature='ss', out_signature='v')
    def Get(self, iface, prop):
        props = self._props().get(iface, {})
        if prop not in props:
            raise dbus.exceptions.DBusException(
                f'Unknown property {prop}',
                name='org.freedesktop.DBus.Error.UnknownProperty')
        return props[prop]

    @dbus.service.method(PROPS_IFACE, in_signature='s', out_signature='a{sv}')
    def GetAll(self, iface):
        return self._props().get(iface, dbus.Dictionary(signature='sv'))

    @dbus.service.method(PROPS_IFACE, in_signature='ssv')
    def Set(self, iface, prop, value):
        pass  # read-only properties

    # org.mpris.MediaPlayer2
    @dbus.service.method(MPRIS_IFACE)
    def Raise(self): self._ov.show(); self._ov.raise_()

    @dbus.service.method(MPRIS_IFACE)
    def Quit(self):
        self._ov._save_state()
        QApplication.quit()

    # org.mpris.MediaPlayer2.Player
    @dbus.service.method(MPRIS_PLAYER)
    def PlayPause(self): self._ov._toggle_play()

    @dbus.service.method(MPRIS_PLAYER)
    def Play(self):
        if self._ov._player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
            self._ov._player.play()

    @dbus.service.method(MPRIS_PLAYER)
    def Pause(self):
        if self._ov._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._ov._player.pause()
            self._ov._copy_current_text()

    @dbus.service.method(MPRIS_PLAYER)
    def Stop(self): self._ov._player.stop()

    @dbus.service.method(MPRIS_PLAYER)
    def Next(self): self._ov._next()

    @dbus.service.method(MPRIS_PLAYER)
    def Previous(self): self._ov._prev()

    @dbus.service.method(MPRIS_PLAYER, in_signature='x')
    def Seek(self, offset_us): self._ov._seek_rel(int(offset_us // 1000))

    @dbus.service.method(MPRIS_PLAYER, in_signature='ox')
    def SetPosition(self, track_id, pos_us):
        self._ov._player.setPosition(int(pos_us // 1000))

    def notify(self, *props):
        """Send PropertiesChanged as a raw message — no GLib loop needed."""
        all_props = self._props().get(MPRIS_PLAYER, {})
        changed = dbus.Dictionary(
            {p: all_props[p] for p in props if p in all_props}, signature='sv')
        msg = dbus.lowlevel.SignalMessage(MPRIS_PATH, PROPS_IFACE, 'PropertiesChanged')
        msg.append(MPRIS_PLAYER, changed, dbus.Array(signature='s'),
                   signature='sa{sv}as')
        self._bus.send_message(msg)


# ── overlay window ────────────────────────────────────────────────────────────
class MarqueeDelegate(QStyledItemDelegate):
    """Scrolls the current playlist entry right-to-left in a loop when it is too wide."""
    GAP = '     '

    def __init__(self, view: QListWidget):
        super().__init__(view)
        self._view = view
        self.offset = 0
        self.cycle = 0       # text width + gap of the current row; 0 when it fits
        self.row = -1

    def paint(self, painter, option, index):
        if index.row() != self.row:
            super().paint(painter, option, index)
            return
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        text = opt.text
        style = opt.widget.style() if opt.widget else QApplication.style()
        text_rect = style.subElementRect(QStyle.SubElement.SE_ItemViewItemText, opt, opt.widget)
        # the row may be wider than the view; only the visible part counts
        text_rect.setRight(min(text_rect.right(), self._view.viewport().rect().right() - 3))
        fm = opt.fontMetrics
        text_w = fm.horizontalAdvance(text)
        if text_w <= text_rect.width():
            self.cycle = 0
            self.offset = 0
            super().paint(painter, option, index)
            return
        self.cycle = text_w + fm.horizontalAdvance(self.GAP)
        self.offset %= self.cycle
        # background / selection without text, then the scrolling text twice for a seamless loop
        opt.text = ''
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        role = QPalette.ColorRole.HighlightedText if selected else QPalette.ColorRole.Text
        painter.save()
        painter.setClipRect(text_rect)
        painter.setFont(opt.font)
        painter.setPen(opt.palette.color(role))
        align = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        for x in (text_rect.x() - self.offset, text_rect.x() - self.offset + self.cycle):
            painter.drawText(QRect(x, text_rect.y(), text_w, text_rect.height()), align, text)
        painter.restore()

    def step(self):
        row = self._view.currentRow()
        if row != self.row:
            self.row, self.offset, self.cycle = row, 0, 0
            self._view.viewport().update()
            return
        if row < 0 or not self.cycle or not self._view.isVisible():
            return
        self.offset = (self.offset + 1) % self.cycle
        self._view.viewport().update(self._view.visualRect(self._view.model().index(row, 0)))


class Overlay(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('Subtitle Overlay')
        self.setWindowFlags(Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAcceptDrops(True)
        self.setMinimumSize(0, 0)  # allow window to shrink freely
        self._cues: list = []
        self._playlist: list[Path] = []
        self._current_index = -1
        self._pending_pos: int | None = None
        self._ui_visible = True
        self._opacity_val = 85
        # window size remembered separately for full UI and subtitle-only mode
        self._full_size: tuple[int, int] = (900, 340)
        self._compact_size: tuple[int, int] | None = None
        self._restoring = True
        self._drag_start_pos = None

        # ── media player ──
        self._player = QMediaPlayer(self)
        self._audio  = QAudioOutput(self)
        self._player.setAudioOutput(self._audio)
        self._audio.setVolume(1.0)
        self._player.mediaStatusChanged.connect(self._on_status)
        self._player.playbackStateChanged.connect(self._on_playback_state)

        # ── layout ──
        self._root_layout = QVBoxLayout(self)

        self.label = QLabel('Open audio file(s) to begin.')
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setWordWrap(True)
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse |
            Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self.label.setCursor(Qt.CursorShape.IBeamCursor)
        self.label.setMinimumSize(0, 0)
        initial_font = QFont()
        initial_font.setPixelSize(28)
        initial_font.setBold(True)
        self.label.setFont(initial_font)
        self.label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        # playlist
        self.playlist_widget = QListWidget()
        self.playlist_widget.setMaximumHeight(120)
        self.playlist_widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.playlist_widget.doubleClicked.connect(
            lambda idx: self._play_index(idx.row(), autoplay=True, start_pos_ms=0))
        # scroll the selected entry when its name is too long to fit
        self.playlist_widget.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._marquee = MarqueeDelegate(self.playlist_widget)
        self.playlist_widget.setItemDelegate(self._marquee)
        self._marquee_timer = QTimer(self)
        self._marquee_timer.timeout.connect(self._marquee.step)
        self._marquee_timer.start(MARQUEE_MS)

        # progress (will be added later)
        self._progress_row = QHBoxLayout()
        self.pos_label = QLabel('0:00')
        self.dur_label = QLabel('0:00')
        self.progress = QSlider(Qt.Orientation.Horizontal)
        self.progress.setRange(0, 0)
        self.progress.sliderMoved.connect(self._seek_to)
        self._progress_row.addWidget(self.pos_label)
        self._progress_row.addWidget(self.progress, 1)
        self._progress_row.addWidget(self.dur_label)

        # transport (stored for later rebuild)
        self._transport_layout = QHBoxLayout()
        btn_open  = QPushButton('📂 Open')
        btn_prev  = QPushButton('⏮')
        btn_seekb = QPushButton(f'−{SEEK_S}s')
        btn_play  = QPushButton('⏯')
        btn_seekf = QPushButton(f'+{SEEK_S}s')
        btn_next  = QPushButton('⏭')
        self._btn_copy = QPushButton('📋 Copy')
        self._btn_copy.setToolTip('Copy current subtitle to clipboard [Ctrl+C]')
        for b in (btn_prev, btn_seekb, btn_play, btn_seekf, btn_next):
            b.setFixedWidth(54)
        self._btn_copy.setFixedWidth(70)
        btn_open.clicked.connect(self._open_files)
        btn_prev.clicked.connect(self._prev)
        btn_seekb.clicked.connect(lambda: self._seek_rel(-SEEK_S * 1000))
        btn_play.clicked.connect(self._toggle_play)
        btn_seekf.clicked.connect(lambda: self._seek_rel(SEEK_S * 1000))
        btn_next.clicked.connect(self._next)
        self._btn_copy.clicked.connect(self._copy_current_text)
        for w in (btn_open, btn_prev, btn_seekb, btn_play, btn_seekf, btn_next, self._btn_copy):
            self._transport_layout.addWidget(w)
        self._transport_layout.addStretch()

        opacity_lbl = QLabel('Opacity')
        self._opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self._opacity_slider.setRange(10, 100)
        self._opacity_slider.setValue(85)
        self._opacity_slider.setFixedWidth(90)
        self._opacity_slider.valueChanged.connect(self._set_opacity)
        self._transport_layout.addWidget(opacity_lbl)
        self._transport_layout.addWidget(self._opacity_slider)

        self._hide_btn = QPushButton('Hide UI')
        self._hide_btn.clicked.connect(self._toggle_ui)
        self._transport_layout.addWidget(self._hide_btn)

        self._ui_widgets = [
            self.playlist_widget, self.progress, self.pos_label, self.dur_label,
            btn_open, btn_prev, btn_seekb, btn_play, btn_seekf, btn_next, self._btn_copy,
            opacity_lbl, self._opacity_slider,
        ]

        # two columns: subtitle left, playlist + progress right, transport below
        right_col = QVBoxLayout()
        right_col.addWidget(self.playlist_widget)
        right_col.addLayout(self._progress_row)
        container = QHBoxLayout()
        container.addWidget(self.label, 3)
        container.addLayout(right_col, 2)
        self._root_layout.addLayout(container, 1)
        self._root_layout.addLayout(self._transport_layout)

        # ── debounced state save timer ──
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(500)
        self._save_timer.timeout.connect(self._save_state)

        self.resize(900, 340)

        # Shortcuts:
        # H key (handled in keyPressEvent) toggles subtitle-only / full UI
        # Space key toggles play/pause
        QShortcut(QKeySequence(Qt.Key.Key_Space), self, self._toggle_play)
        # Ctrl+C or C copies current subtitle
        QShortcut(QKeySequence.StandardKey.Copy, self, self._copy_current_text)
        QShortcut(QKeySequence('C'), self, self._copy_current_text)

        # poll timer
        timer = QTimer(self)
        timer.timeout.connect(self._tick)
        timer.start(POLL_MS)

        # ── system tray ──
        # Create a tray icon with a book emoji
        px = QPixmap(32, 32)
        px.fill(Qt.GlobalColor.transparent)
        painter = QPainter(px)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = QFont()
        font.setPixelSize(24)
        painter.setFont(font)
        painter.drawText(px.rect(), Qt.AlignmentFlag.AlignCenter, "🍀")
        painter.end()
        self._tray = QSystemTrayIcon(QIcon(px), self)
        self.setWindowIcon(QIcon(px))
        tray_menu = QMenu()
        self._win_action = tray_menu.addAction('Hide overlay')
        self._win_action.triggered.connect(self._toggle_window)
        tray_menu.addAction('Show/Hide UI [H]').triggered.connect(self._toggle_ui)
        tray_menu.addSeparator()
        tray_menu.addAction('Quit').triggered.connect(self._quit)
        self._tray.setContextMenu(tray_menu)
        self._tray.setToolTip('Subtitle Overlay')
        self._tray.activated.connect(
            lambda r: self._toggle_window()
            if r == QSystemTrayIcon.ActivationReason.Trigger else None)
        self._tray.show()

        # ── MPRIS2 ──
        self._mpris = MprisService(self)

        app = QApplication.instance()
        if app:
            app.aboutToQuit.connect(self._save_state)

        # ── restore state ──
        self._restore_state()

    # ── mouse dragging to move window ──
    def mousePressEvent(self, event):
        child = self.childAt(event.position().toPoint())
        if child is self.label:
            super().mousePressEvent(event)
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.MouseButton.LeftButton and getattr(self, '_drag_start_pos', None) is not None:
            self.move(event.globalPosition().toPoint() - self._drag_start_pos)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_H:
            # Toggle between subtitle only and full UI (subtitle | playlist + controls)
            self._toggle_ui()
        else:
            super().keyPressEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_start_pos = None
        super().mouseReleaseEvent(event)

    # ── drag and drop media files ──
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = []
        for url in event.mimeData().urls():
            p = Path(url.toLocalFile())
            if p.is_file() and p.suffix.lower() in ('.mp3', '.ogg', '.flac', '.wav', '.m4a', '.opus'):
                paths.append(p)
            elif p.is_dir():
                for sub in sorted(p.glob('*')):
                    if sub.suffix.lower() in ('.mp3', '.ogg', '.flac', '.wav', '.m4a', '.opus'):
                        paths.append(sub)
        if paths:
            self._playlist = paths
            self.playlist_widget.clear()
            for p in self._playlist:
                self.playlist_widget.addItem(p.name)
            self._play_index(0, autoplay=True, start_pos_ms=0)

    # ── window move & resize events ──
    def moveEvent(self, event):
        super().moveEvent(event)
        self._schedule_save()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Adjust font size to fit current subtitle text
        if self.label.text():
            new_sz = fit_font_size(self.label.text(), self.label.width(), self.label.height())
            f = self.label.font()
            f.setPixelSize(new_sz)
            f.setBold(True)
            self.label.setFont(f)
        size = (self.width(), self.height())
        if self._ui_visible:
            self._full_size = size
        else:
            self._compact_size = size
        self._schedule_save()

    # ── state persistence ──
    def _schedule_save(self):
        if not getattr(self, '_restoring', False):
            self._save_timer.start()

    def _save_state(self):
        if getattr(self, '_restoring', False):
            return
        pos = self._player.position() if self._pending_pos is None else self._pending_pos
        dur = self._player.duration()
        if dur > 0 and pos >= dur:
            pos = 0

        state = {
            'window': {
                'x': self.x(),
                'y': self.y(),
                'width': self.width(),
                'height': self.height(),
                'geometry': self.saveGeometry().toHex().data().decode('ascii'),
                'opacity': getattr(self, '_opacity_val', 85),
                'full_size': list(self._full_size),
                'compact_size': list(self._compact_size) if self._compact_size else None,
                'ui_visible': self._ui_visible,
            },
            'playlist': [str(p) for p in self._playlist],
            'current_index': self._current_index,
            'position_ms': max(0, pos),
        }
        save_state_file(state)

    def _restore_state(self):
        state = load_state_file()
        win = state.get('window', {})

        op = win.get('opacity', 85)
        self._opacity_slider.setValue(op)
        self._set_opacity(op)

        geo_hex = win.get('geometry')
        restored_geo = False
        if geo_hex:
            try:
                restored_geo = self.restoreGeometry(bytes.fromhex(geo_hex))
            except Exception:
                pass
        if not restored_geo:
            w = win.get('width', 900)
            h = win.get('height', 340)
            self.resize(w, h)
            x = win.get('x')
            y = win.get('y')
            if x is not None and y is not None:
                self.move(x, y)
        else:
            x = win.get('x')
            y = win.get('y')
            if x is not None and y is not None:
                self.move(x, y)

        full = win.get('full_size')
        if full:
            self._full_size = (full[0], full[1])
        else:
            self._full_size = (self.width(), win.get('normal_height', self.height()))
        compact = win.get('compact_size')
        if compact:
            self._compact_size = (compact[0], compact[1])
        if not win.get('ui_visible', True):
            # saved geometry is the subtitle-only size, but we start with the full UI
            self.resize(*self._full_size)

        raw_playlist = state.get('playlist', [])
        valid_paths = [Path(p) for p in raw_playlist if Path(p).is_file()]
        if valid_paths:
            self._playlist = valid_paths
            self.playlist_widget.clear()
            for p in self._playlist:
                self.playlist_widget.addItem(p.name)
            idx = state.get('current_index', 0)
            idx = max(0, min(idx, len(self._playlist) - 1))
            pos_ms = max(0, state.get('position_ms', 0))
            self._play_index(idx, autoplay=False, start_pos_ms=pos_ms)
            self._copy_current_text()

        self._restoring = False

    # ── UI toggle ──
    def _toggle_ui(self):
        # pick the target size first: resize events from the switch would overwrite it
        showing = not self._ui_visible
        target = self._full_size if showing else (self._compact_size or (self.width(), 80))
        self._ui_visible = showing
        for w in self._ui_widgets:
            w.setVisible(showing)
        self._hide_btn.setVisible(showing)
        # recompute the minimum size for the new set of visible widgets before resizing
        self.layout().activate()
        self.resize(*target)
        self._schedule_save()

    # ── window toggle (tray) ──
    def _toggle_window(self):
        if self.isVisible():
            self._save_state()
            self.hide()
            self._win_action.setText('Show overlay')
        else:
            self.show()
            self.raise_()
            self._win_action.setText('Hide overlay')

    def closeEvent(self, event):
        self._save_state()
        event.ignore()
        self._toggle_window()

    def _quit(self):
        self._save_state()
        QApplication.quit()

    # ── opacity ──
    def _set_opacity(self, value: int):
        self._opacity_val = value
        alpha = round(value * 2.55)
        self.label.setStyleSheet(
            f'QLabel {{ color: rgba(255,255,255,{alpha}); '
            f'font-weight: bold; background: transparent; '
            f'selection-background-color: #2a75d3; selection-color: #ffffff; }}')
        self._schedule_save()

    def _update_font(self):
        """Adjust subtitle font size to fit the label area."""
        text = self.label.text()
        if not text:
            return
        new_sz = fit_font_size(text, self.label.width(), self.label.height())
        f = self.label.font()
        f.setPixelSize(new_sz)
        f.setBold(True)
        self.label.setFont(f)

    # ── copy text ──
    def _copy_current_text(self):
        text = self.label.selectedText().strip()
        if not text:
            pos = self._player.position() if self._pending_pos is None else self._pending_pos
            cue = cue_at(self._cues, pos)
            text = cue.strip() if cue else self.label.text().strip()
        if text and text != 'Open audio file(s) to begin.':
            cb = QApplication.clipboard()
            cb.setText(text, QClipboard.Mode.Clipboard)
            cb.setText(text, QClipboard.Mode.Selection)
            if hasattr(self, '_btn_copy'):
                self._btn_copy.setText('✓ Copied')
                QTimer.singleShot(1200, lambda: self._btn_copy.setText('📋 Copy'))
            return True
        return False

    # ── file open ──
    def _open_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, 'Open audio files', '',
            'Audio files (*.mp3 *.ogg *.flac *.wav *.m4a *.opus);;All files (*)')
        if not paths:
            return
        self._playlist = [Path(p) for p in paths]
        self.playlist_widget.clear()
        for p in self._playlist:
            self.playlist_widget.addItem(p.name)
        self._play_index(0, autoplay=True, start_pos_ms=0)

    # ── playback ──
    def _play_index(self, index: int, autoplay: bool = True, start_pos_ms: int = 0):
        if not 0 <= index < len(self._playlist):
            return
        self._current_index = index
        self.playlist_widget.setCurrentRow(index)
        path = self._playlist[index]
        self.setWindowTitle(path.name)
        self._cues = []
        txt = path.with_suffix('.txt')
        if txt.exists():
            try:
                self._cues = load_txt(txt)
            except Exception:
                pass
        self._pending_pos = start_pos_ms if start_pos_ms > 0 else None
        self._player.setSource(QUrl.fromLocalFile(str(path)))
        if autoplay:
            self._player.play()
        else:
            self._player.pause()
            if start_pos_ms > 0:
                self.pos_label.setText(_fmt(start_pos_ms))
                self.progress.setValue(start_pos_ms)
                cue = cue_at(self._cues, start_pos_ms)
                if cue:
                    self.label.setText(cue)
        self._mpris.notify('PlaybackStatus', 'Metadata', 'CanGoNext', 'CanGoPrevious')
        self._schedule_save()

    def _toggle_play(self):
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
            pos = self._player.position()
            cue = cue_at(self._cues, pos)
            if cue:
                self.label.setText(cue)
            self._copy_current_text()
        else:
            if not self._playlist:
                return
            if self._player.source().isEmpty() and 0 <= self._current_index < len(self._playlist):
                self._play_index(self._current_index, autoplay=True, start_pos_ms=0)
            else:
                self._player.play()

    def _prev(self): self._play_index(self._current_index - 1, autoplay=True, start_pos_ms=0)
    def _next(self): self._play_index(self._current_index + 1, autoplay=True, start_pos_ms=0)

    def _seek_rel(self, delta_ms: int):
        pos = self._player.position() if self._pending_pos is None else self._pending_pos
        dur = self._player.duration()
        new_pos = max(0, min(pos + delta_ms, dur)) if dur > 0 else max(0, pos + delta_ms)
        if self._pending_pos is not None:
            self._pending_pos = new_pos
        self._player.setPosition(new_pos)
        cue = cue_at(self._cues, new_pos)
        if cue:
            self.label.setText(cue)
            self._update_font()
            if self._player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
                self._copy_current_text()
        self._schedule_save()

    def _seek_to(self, ms: int):
        if self._pending_pos is not None:
            self._pending_pos = ms
        self._player.setPosition(ms)
        cue = cue_at(self._cues, ms)
        if cue:
            self.label.setText(cue)
            self._update_font()
            if self._player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
                self._copy_current_text()
        self._schedule_save()

    def _on_status(self, status):
        if status in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia):
            if self._pending_pos is not None:
                self._player.setPosition(self._pending_pos)
                self._player.pause()
                self._pending_pos = None
        elif status == QMediaPlayer.MediaStatus.EndOfMedia:
            if self._current_index + 1 < len(self._playlist):
                self._play_index(self._current_index + 1, autoplay=True, start_pos_ms=0)
            else:
                self.label.setText('')
                self._save_state()

    def _on_playback_state(self, state):
        self._mpris.notify('PlaybackStatus')
        if state == QMediaPlayer.PlaybackState.PausedState:
            pos = self._player.position()
            cue = cue_at(self._cues, pos)
            if cue:
                self.label.setText(cue)
                self._update_font()
            self._copy_current_text()
            self._save_state()
        elif state == QMediaPlayer.PlaybackState.StoppedState:
            self._save_state()

    # ── poll tick ──
    def _tick(self):
        pos = self._pending_pos if self._pending_pos is not None else self._player.position()
        dur = self._player.duration()
        self.progress.setMaximum(max(dur, 1))
        if not self.progress.isSliderDown():
            self.progress.setValue(pos)
        self.pos_label.setText(_fmt(pos))
        self.dur_label.setText(_fmt(dur))
        cue = cue_at(self._cues, pos)
        if cue:
            self.label.setText(cue)
            self._update_font()
        elif not self._playlist:
            self.label.setText('Open audio file(s) to begin.')
            self._update_font()
        else:
            self.label.setText('')
            self._update_font()

        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._tick_count = getattr(self, '_tick_count', 0) + 1
            if self._tick_count % 25 == 0:  # ~every 5 seconds
                self._save_state()


def _fmt(ms: int) -> str:
    s = max(0, ms) // 1000
    return f'{s // 60}:{s % 60:02d}'


# ── tests ─────────────────────────────────────────────────────────────────────
def _test():
    import os, tempfile
    content = "00:00.000 --> 00:05.000\nHello world\n\n00:05.000 --> 00:10.000\nSecond line\n"
    with tempfile.NamedTemporaryFile('w', suffix='.txt', delete=False) as f:
        f.write(content); name = f.name
    try:
        cues = load_txt(Path(name))
        assert cue_at(cues, 0)     == 'Hello world'
        assert cue_at(cues, 4999)  == 'Hello world'
        assert cue_at(cues, 5000)  == 'Second line'
        assert cue_at(cues, 10000) == ''
        assert cue_at([], 0)       == ''
        assert _fmt(0)     == '0:00'
        assert _fmt(61000) == '1:01'
        assert _fmt(-500)  == '0:00'
    finally:
        os.unlink(name)

    # Test state file save and load
    with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
        state_tmp = Path(f.name)
    try:
        test_state = {
            'window': {'x': 150, 'y': 250, 'width': 950, 'height': 350, 'opacity': 90},
            'playlist': [name],
            'current_index': 0,
            'position_ms': 42000,
        }
        assert save_state_file(test_state, state_tmp)
        loaded = load_state_file(state_tmp)
        assert loaded == test_state
        assert load_state_file(Path('/non/existent/path/state.json')) == {}
    finally:
        if state_tmp.exists():
            state_tmp.unlink()

    print('Tests passed')


if __name__ == '__main__':
    if '--test' in sys.argv:
        _test()
        sys.exit(0)
    app = QApplication(sys.argv)
    w = Overlay()
    w.show()
    sys.exit(app.exec())

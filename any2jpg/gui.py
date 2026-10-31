"""Finestra principale di any2jpg (PySide6)."""

from __future__ import annotations

import os
import sys
from collections import OrderedDict
from collections.abc import Iterable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from PIL import Image, UnidentifiedImageError
from PySide6.QtCore import (
    QObject,
    QRunnable,
    QSettings,
    QSize,
    QStandardPaths,
    Qt,
    QThread,
    QThreadPool,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import QDesktopServices, QIcon, QImage, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QSplitter,
    QStyle,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import DISPLAY_VERSION, __version__, converter
from .converter import MetadataOptions

ROLE_PATH = Qt.ItemDataRole.UserRole
ROLE_ROOT = Qt.ItemDataRole.UserRole + 1
ROLE_DONE = Qt.ItemDataRole.UserRole + 2

STATUS_PENDING = "In attesa"

THUMB_SIZE = 48
PREVIEW_SIZE = 480
PREVIEW_CACHE = 30  # anteprime grandi tenute in memoria
# Più thread significano più immagini decodificate in memoria contemporaneamente.
CONVERT_THREADS = min(os.cpu_count() or 1, 8)


@dataclass(frozen=True)
class Job:
    row: int
    source: Path
    root: Path | None


class ConvertWorker(QObject):
    """Converte i file in parallelo, coordinando un pool di thread da un QThread.

    Pillow rilascia il GIL durante decodifica, ridimensionamento e codifica,
    quindi i thread lavorano davvero in parallelo.
    """

    progress = Signal(int, bool, str)  # riga, riuscito, destinazione o messaggio d'errore
    finished = Signal()

    def __init__(
        self,
        jobs: list[Job],
        quality: int,
        metadata: MetadataOptions,
        output_dir: Path | None,
        overwrite: bool,
        max_side: int,
        keep_mtime: bool,
    ) -> None:
        super().__init__()
        self._jobs = jobs
        self._quality = quality
        self._metadata = metadata
        self._output_dir = output_dir
        self._overwrite = overwrite
        self._max_side = max_side
        self._keep_mtime = keep_mtime
        self._cancelled = False
        self._futures: list[Future] = []

    def cancel(self) -> None:
        """Annulla i file non ancora iniziati; quelli in corso vengono completati."""
        self._cancelled = True
        for future in self._futures:
            future.cancel()

    @Slot()
    def run(self) -> None:
        # Anche i sorgenti del lotto (per esempio dei JPG) sono originali da non sovrascrivere.
        reserved: set[Path] = {job.source.resolve() for job in self._jobs}
        with ThreadPoolExecutor(max_workers=CONVERT_THREADS) as pool:
            futures: dict[Future, Job] = {}
            for job in self._jobs:
                if self._cancelled:
                    break
                try:
                    destination = converter.plan_destination(
                        job.source, job.root, self._output_dir, self._overwrite, reserved
                    )
                except Exception as exc:
                    self._report_error(job, exc)
                    continue
                reserved.add(destination)
                futures[pool.submit(self._convert, job.source, destination)] = job
            self._futures = list(futures)
            if self._cancelled:
                self.cancel()
            for future in as_completed(futures):
                if future.cancelled():
                    continue
                job = futures[future]
                try:
                    destination = future.result()
                except Exception as exc:  # un file rotto non deve fermare il lotto
                    self._report_error(job, exc)
                else:
                    self.progress.emit(job.row, True, str(destination))
        self.finished.emit()

    def _convert(self, source: Path, destination: Path) -> Path:
        converter.convert_file(
            source, destination, self._quality, self._metadata,
            max_side=self._max_side, keep_mtime=self._keep_mtime,
        )
        return destination

    def _report_error(self, job: Job, exc: Exception) -> None:
        if isinstance(exc, UnidentifiedImageError):
            message = "formato non riconosciuto o file danneggiato"
        else:
            message = str(exc) or type(exc).__name__
        self.progress.emit(job.row, False, message)


class PreviewSignals(QObject):
    ready = Signal(str, int, QImage, str)  # percorso, lato richiesto, immagine, descrizione
    failed = Signal(str, int, str)  # percorso, lato richiesto, messaggio d'errore


class PreviewTask(QRunnable):
    """Genera un'anteprima in un thread del pool (anche per HEIC, AVIF, …)."""

    def __init__(self, path: str, size: int, signals: PreviewSignals) -> None:
        super().__init__()
        self._path = path
        self._size = size
        self._signals = signals

    def run(self) -> None:
        try:
            preview = converter.make_preview(Path(self._path), self._size)
        except UnidentifiedImageError:
            self._signals.failed.emit(self._path, self._size, "formato non riconosciuto o file danneggiato")
        except Exception as exc:
            self._signals.failed.emit(self._path, self._size, str(exc) or type(exc).__name__)
        else:
            description = f"{preview.format} · {preview.width} × {preview.height} px"
            self._signals.ready.emit(self._path, self._size, _to_qimage(preview.image), description)


def _to_qimage(image: Image.Image) -> QImage:
    """Converte un'immagine Pillow RGB o L in QImage (copia indipendente dai dati Pillow)."""
    if image.mode == "L":
        data = image.tobytes()
        qimage = QImage(data, image.width, image.height, image.width, QImage.Format.Format_Grayscale8)
    else:
        data = image.convert("RGB").tobytes()
        qimage = QImage(data, image.width, image.height, image.width * 3, QImage.Format.Format_RGB888)
    return qimage.copy()


class PreviewLabel(QLabel):
    """Mostra un'immagine adattandola allo spazio disponibile, senza ingrandirla."""

    def __init__(self) -> None:
        super().__init__()
        self._pixmap: QPixmap | None = None
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(200, 200)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.setFrameShape(QLabel.Shape.StyledPanel)

    def set_image(self, image: QImage | None, placeholder: str = "") -> None:
        self._pixmap = QPixmap.fromImage(image) if image is not None else None
        if self._pixmap is None:
            self.clear()
            self.setText(placeholder)
        else:
            self._rescale()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._pixmap is not None:
            self._rescale()

    def _rescale(self) -> None:
        area = self.contentsRect().size()
        pixmap = self._pixmap
        if pixmap.width() > area.width() or pixmap.height() > area.height():
            pixmap = pixmap.scaled(
                area, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
            )
        self.setPixmap(pixmap)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"any2jpg {DISPLAY_VERSION}")
        self.setWindowIcon(QIcon.fromTheme("image-x-generic"))
        self.setAcceptDrops(True)
        self.resize(1100, 720)

        self._settings = QSettings("any2jpg", "any2jpg")
        self._items: dict[str, QTreeWidgetItem] = {}
        self._thread: QThread | None = None
        self._worker: ConvertWorker | None = None
        self._errors = 0
        self._bytes_before = 0
        self._bytes_after = 0
        self._open_dir: Path | None = None

        self._preview_pool = QThreadPool(self)
        self._preview_pool.setMaxThreadCount(max(2, (os.cpu_count() or 2) // 2))
        self._preview_signals = PreviewSignals(self)
        self._preview_signals.ready.connect(self._on_preview_ready)
        self._preview_signals.failed.connect(self._on_preview_failed)
        self._preview_cache: OrderedDict[str, tuple[QImage, str]] = OrderedDict()
        self._preview_pending: set[str] = set()

        self._build_ui()
        self._load_settings()
        self._update_enabled()
        self.statusBar().showMessage("Aggiungi file o cartelle, oppure trascinali nella finestra.")

    # --- costruzione interfaccia -------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        layout = QVBoxLayout(central)

        toolbar = QHBoxLayout()
        self.button_add_files = QPushButton(QIcon.fromTheme("document-open"), "Aggiungi file…")
        self.button_add_files.clicked.connect(self._choose_files)
        self.button_add_folder = QPushButton(QIcon.fromTheme("folder-open"), "Aggiungi cartella…")
        self.button_add_folder.clicked.connect(self._choose_folder)
        self.check_recursive = QCheckBox("Includi sottocartelle")
        self.check_include_jpeg = QCheckBox("Includi anche i JPG")
        self.check_include_jpeg.setToolTip(
            "Nelle cartelle, ricomprime anche i JPG esistenti\n"
            "(utile per cambiare qualità, dimensioni o metadati)."
        )
        self.button_remove = QPushButton(QIcon.fromTheme("list-remove"), "Rimuovi")
        self.button_remove.clicked.connect(self._remove_selected)
        self.button_clear = QPushButton(QIcon.fromTheme("edit-clear"), "Svuota")
        self.button_clear.clicked.connect(self._clear)
        for widget in (self.button_add_files, self.button_add_folder, self.check_recursive, self.check_include_jpeg):
            toolbar.addWidget(widget)
        toolbar.addStretch()
        toolbar.addWidget(self.button_remove)
        toolbar.addWidget(self.button_clear)
        layout.addLayout(toolbar)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["File", "Stato"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.tree.setIconSize(QSize(THUMB_SIZE, THUMB_SIZE))
        self.tree.setUniformRowHeights(True)
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.resizeSection(1, 260)
        self.tree.currentItemChanged.connect(self._show_preview)
        QShortcut(QKeySequence.StandardKey.Delete, self.tree, activated=self._remove_selected)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.tree)
        splitter.addWidget(self._build_preview_panel())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        splitter.setChildrenCollapsible(False)
        layout.addWidget(splitter, stretch=1)

        layout.addWidget(self._build_quality_group())
        options = QHBoxLayout()
        options.addWidget(self._build_metadata_group(), stretch=1)
        options.addWidget(self._build_destination_group(), stretch=1)
        layout.addLayout(options)

        bottom = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setValue(0)
        self.button_convert = QPushButton(QIcon.fromTheme("media-playback-start"), "Converti")
        self.button_convert.setDefault(True)
        self.button_convert.setMinimumWidth(140)
        self.button_convert.clicked.connect(self._convert_or_cancel)
        bottom.addWidget(self.progress, stretch=1)
        bottom.addWidget(self.button_convert)
        layout.addLayout(bottom)

        self.setCentralWidget(central)

    def _build_preview_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        self.label_preview = PreviewLabel()
        self.label_preview.set_image(None, "Nessuna immagine selezionata")
        self.label_preview_name = QLabel()
        self.label_preview_name.setWordWrap(True)
        self.label_preview_name.setTextFormat(Qt.TextFormat.PlainText)
        font = self.label_preview_name.font()
        font.setBold(True)
        self.label_preview_name.setFont(font)
        self.label_preview_info = QLabel()
        self.label_preview_info.setWordWrap(True)
        self.label_preview_info.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.label_preview, stretch=1)
        layout.addWidget(self.label_preview_name)
        layout.addWidget(self.label_preview_info)
        return panel

    def _build_quality_group(self) -> QGroupBox:
        group = QGroupBox("Qualità e dimensioni")
        grid = QGridLayout(group)
        self.slider_quality = QSlider(Qt.Orientation.Horizontal)
        self.slider_quality.setRange(1, 100)
        self.slider_quality.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.slider_quality.setTickInterval(10)
        self.spin_quality = QSpinBox()
        self.spin_quality.setRange(1, 100)
        self.slider_quality.valueChanged.connect(self.spin_quality.setValue)
        self.spin_quality.valueChanged.connect(self.slider_quality.setValue)
        hint = QLabel("85–92 è un buon compromesso tra qualità e dimensione")
        hint.setEnabled(False)

        self.check_resize = QCheckBox("Riduci se il lato lungo supera")
        self.check_resize.setToolTip("Le proporzioni vengono mantenute.")
        self.check_resize.toggled.connect(self._update_enabled)
        self.spin_max_side = QSpinBox()
        self.spin_max_side.setRange(16, 30000)
        self.spin_max_side.setSingleStep(100)
        self.spin_max_side.setSuffix(" px")
        resize_hint = QLabel("le immagini più piccole non vengono ingrandite")
        resize_hint.setEnabled(False)
        resize_row = QHBoxLayout()
        resize_row.addWidget(self.spin_max_side)
        resize_row.addWidget(resize_hint)
        resize_row.addStretch()

        grid.addWidget(QLabel("Qualità:"), 0, 0)
        grid.addWidget(self.slider_quality, 0, 1)
        grid.addWidget(self.spin_quality, 0, 2)
        grid.addWidget(hint, 0, 3)
        grid.addWidget(self.check_resize, 1, 0, 1, 2)
        grid.addLayout(resize_row, 1, 2, 1, 2)
        grid.setColumnStretch(1, 1)
        return group

    def _build_metadata_group(self) -> QGroupBox:
        group = QGroupBox("Metadati")
        layout = QVBoxLayout(group)
        self.radio_keep = QRadioButton("Mantieni i metadati originali (EXIF, XMP)")
        self.radio_strip = QRadioButton("Rimuovi tutti i metadati")
        self.radio_strip.setToolTip(
            "Elimina data, fotocamera, GPS, ecc.\n"
            "Il profilo colore (ICC) viene mantenuto: non contiene dati personali."
        )
        metadata_mode = QButtonGroup(group)
        metadata_mode.addButton(self.radio_keep)
        metadata_mode.addButton(self.radio_strip)
        self.check_set_fields = QCheckBox("Imposta autore e copyright")
        self.check_set_fields.setToolTip(
            "Scritti nei campi EXIF Artist/Copyright e XMP dc:creator/dc:rights.\n"
            "Hanno la precedenza su quelli dell'originale."
        )
        self.check_set_fields.toggled.connect(self._update_enabled)

        self.edit_author = QLineEdit()
        self.edit_author.setPlaceholderText("Nome Cognome")
        self.edit_copyright = QLineEdit()
        self.edit_copyright.setPlaceholderText(f"© {date.today().year} Nome Cognome")
        self.button_fill_copyright = QToolButton()
        self.button_fill_copyright.setText("©")
        self.button_fill_copyright.setToolTip("Compila con «© anno autore»")
        self.button_fill_copyright.clicked.connect(self._fill_copyright)
        copyright_row = QHBoxLayout()
        copyright_row.addWidget(self.edit_copyright)
        copyright_row.addWidget(self.button_fill_copyright)
        form = QFormLayout()
        form.setContentsMargins(24, 0, 0, 0)
        form.addRow("Autore:", self.edit_author)
        form.addRow("Copyright:", copyright_row)

        layout.addWidget(self.radio_keep)
        layout.addWidget(self.radio_strip)
        layout.addWidget(self.check_set_fields)
        layout.addLayout(form)
        layout.addStretch()
        return group

    def _build_destination_group(self) -> QGroupBox:
        group = QGroupBox("Destinazione")
        layout = QGridLayout(group)
        self.radio_origin = QRadioButton("Cartella di origine di ogni file")
        self.radio_custom = QRadioButton("Altra cartella:")
        self.radio_custom.setToolTip("Per le cartelle aggiunte, la struttura delle sottocartelle viene ricreata.")
        destination_mode = QButtonGroup(group)
        destination_mode.addButton(self.radio_origin)
        destination_mode.addButton(self.radio_custom)
        self.radio_origin.setChecked(True)  # sempre all'avvio, come da specifica
        self.radio_custom.toggled.connect(self._update_enabled)
        self.edit_output_dir = QLineEdit()
        self.button_browse = QPushButton("Sfoglia…")
        self.button_browse.clicked.connect(self._choose_output_dir)
        self.check_overwrite = QCheckBox("Sovrascrivi i JPG esistenti")
        self.check_overwrite.setToolTip(
            "Se disattivato, aggiunge _1, _2… al nome invece di sovrascrivere.\n"
            "Il file originale non viene mai sovrascritto."
        )
        self.check_keep_mtime = QCheckBox("Mantieni la data di modifica degli originali")
        self.check_keep_mtime.setToolTip(
            "Il JPG riceve la stessa data di modifica del file di partenza,\n"
            "così resta in ordine nei programmi che ordinano per data."
        )
        layout.addWidget(self.radio_origin, 0, 0, 1, 3)
        layout.addWidget(self.radio_custom, 1, 0)
        layout.addWidget(self.edit_output_dir, 1, 1)
        layout.addWidget(self.button_browse, 1, 2)
        layout.addWidget(self.check_overwrite, 2, 0, 1, 3)
        layout.addWidget(self.check_keep_mtime, 3, 0, 1, 3)
        layout.setRowStretch(4, 1)
        return group

    # --- impostazioni ---------------------------------------------------------------

    def _load_settings(self) -> None:
        s = self._settings
        self.spin_quality.setValue(s.value("quality", 90, type=int))
        self.slider_quality.setValue(self.spin_quality.value())
        self.check_resize.setChecked(s.value("resize", False, type=bool))
        self.spin_max_side.setValue(s.value("max_side", 2560, type=int))
        keep = s.value("keep_metadata", True, type=bool)
        (self.radio_keep if keep else self.radio_strip).setChecked(True)
        self.check_set_fields.setChecked(s.value("set_fields", False, type=bool))
        self.edit_author.setText(s.value("author", "", type=str))
        self.edit_copyright.setText(s.value("copyright", "", type=str))
        self.edit_output_dir.setText(s.value("output_dir", "", type=str))
        self.check_overwrite.setChecked(s.value("overwrite", False, type=bool))
        self.check_keep_mtime.setChecked(s.value("keep_mtime", True, type=bool))
        self.check_recursive.setChecked(s.value("recursive", True, type=bool))
        self.check_include_jpeg.setChecked(s.value("include_jpeg", False, type=bool))

    def _save_settings(self) -> None:
        s = self._settings
        s.setValue("quality", self.spin_quality.value())
        s.setValue("resize", self.check_resize.isChecked())
        s.setValue("max_side", self.spin_max_side.value())
        s.setValue("keep_metadata", self.radio_keep.isChecked())
        s.setValue("set_fields", self.check_set_fields.isChecked())
        s.setValue("author", self.edit_author.text())
        s.setValue("copyright", self.edit_copyright.text())
        s.setValue("output_dir", self.edit_output_dir.text())
        s.setValue("overwrite", self.check_overwrite.isChecked())
        s.setValue("keep_mtime", self.check_keep_mtime.isChecked())
        s.setValue("recursive", self.check_recursive.isChecked())
        s.setValue("include_jpeg", self.check_include_jpeg.isChecked())

    def _last_dir(self) -> str:
        return self._settings.value("last_dir", str(Path.home()), type=str)

    # --- lista dei file -------------------------------------------------------------

    def add_paths(self, paths: Iterable[Path]) -> None:
        """Aggiunge file e cartelle (le cartelle vengono scansionate)."""
        extensions = converter.supported_extensions()
        skipped = 0
        for path in paths:
            if path.is_dir():
                self._add_folder(path)
            elif path.is_file():
                if path.suffix.lower() in extensions:
                    self._add_files([path], root=None)
                else:
                    skipped += 1
        if skipped:
            self.statusBar().showMessage(f"{skipped} file ignorati: formato non riconosciuto.", 5000)

    def _add_files(self, paths: Iterable[Path], root: Path | None) -> int:
        added = 0
        placeholder = QIcon.fromTheme("image-x-generic")
        for path in paths:
            key = str(path.resolve())
            if key in self._items:
                continue
            item = QTreeWidgetItem([key, STATUS_PENDING])
            item.setToolTip(0, key)
            item.setIcon(0, placeholder)
            item.setData(0, ROLE_PATH, key)
            item.setData(0, ROLE_ROOT, str(root.resolve()) if root else "")
            item.setData(0, ROLE_DONE, False)
            self.tree.addTopLevelItem(item)
            self._items[key] = item
            self._preview_pool.start(PreviewTask(key, THUMB_SIZE, self._preview_signals))
            added += 1
        self._update_enabled()
        return added

    def _add_folder(self, folder: Path) -> None:
        found = converter.scan_folder(
            folder, self.check_recursive.isChecked(), self.check_include_jpeg.isChecked()
        )
        added = self._add_files(found, root=folder)
        self.statusBar().showMessage(f"{folder.name}: {added} immagini aggiunte.", 5000)

    def _choose_files(self) -> None:
        patterns = sorted(converter.supported_extensions())
        patterns = " ".join(f"*{ext} *{ext.upper()}" for ext in patterns)
        files, _ = QFileDialog.getOpenFileNames(
            self, "Scegli le immagini", self._last_dir(),
            f"Immagini ({patterns});;Tutti i file (*)",
        )
        if files:
            self._settings.setValue("last_dir", str(Path(files[0]).parent))
            self._add_files((Path(f) for f in files), root=None)

    def _choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Scegli una cartella", self._last_dir())
        if folder:
            self._settings.setValue("last_dir", folder)
            self._add_folder(Path(folder))

    def _choose_output_dir(self) -> None:
        start = self.edit_output_dir.text() or self._last_dir()
        folder = QFileDialog.getExistingDirectory(self, "Cartella di destinazione", start)
        if folder:
            self.edit_output_dir.setText(folder)
            self.radio_custom.setChecked(True)

    def _remove_selected(self) -> None:
        if self._busy():
            return
        for item in self.tree.selectedItems():
            self._items.pop(item.data(0, ROLE_PATH), None)
            self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(item))
        self._update_enabled()

    def _clear(self) -> None:
        self._preview_pool.clear()  # le miniature non ancora iniziate non servono più
        self._preview_pending.clear()
        self.tree.clear()
        self._items.clear()
        self.progress.setValue(0)
        self._update_enabled()

    def _fill_copyright(self) -> None:
        author = self.edit_author.text().strip() or "Nome Cognome"
        self.edit_copyright.setText(f"© {date.today().year} {author}")

    # --- anteprime --------------------------------------------------------------------

    @Slot()
    def _show_preview(self) -> None:
        item = self.tree.currentItem()
        if item is None:
            self.label_preview.set_image(None, "Nessuna immagine selezionata")
            self.label_preview_name.clear()
            self.label_preview_info.clear()
            return
        path = item.data(0, ROLE_PATH)
        self.label_preview_name.setText(Path(path).name)
        cached = self._preview_cache.get(path)
        if cached is not None:
            self._preview_cache.move_to_end(path)
            self._display_preview(path, *cached)
            return
        self.label_preview.set_image(None, "Caricamento…")
        self.label_preview_info.setText(self._file_size(path))
        if path not in self._preview_pending:
            self._preview_pending.add(path)
            # Priorità sulle miniature: è l'immagine che l'utente sta guardando.
            self._preview_pool.start(PreviewTask(path, PREVIEW_SIZE, self._preview_signals), 1)

    def _display_preview(self, path: str, image: QImage, description: str) -> None:
        self.label_preview.set_image(image)
        size = self._file_size(path)
        self.label_preview_info.setText(f"{description} · {size}" if size else description)

    @Slot(str, int, QImage, str)
    def _on_preview_ready(self, path: str, size: int, image: QImage, description: str) -> None:
        if size == THUMB_SIZE:
            item = self._items.get(path)
            if item is not None:
                item.setIcon(0, QIcon(QPixmap.fromImage(image)))
                item.setToolTip(0, f"{path}\n{description}")
            return
        self._preview_pending.discard(path)
        self._preview_cache[path] = (image, description)
        while len(self._preview_cache) > PREVIEW_CACHE:
            self._preview_cache.popitem(last=False)
        current = self.tree.currentItem()
        if current is not None and current.data(0, ROLE_PATH) == path:
            self._display_preview(path, image, description)

    @Slot(str, int, str)
    def _on_preview_failed(self, path: str, size: int, message: str) -> None:
        if size == THUMB_SIZE:
            item = self._items.get(path)
            if item is not None:
                item.setIcon(0, QIcon.fromTheme("image-missing"))
                item.setToolTip(0, f"{path}\nAnteprima non disponibile: {message}")
            return
        self._preview_pending.discard(path)
        current = self.tree.currentItem()
        if current is not None and current.data(0, ROLE_PATH) == path:
            self.label_preview.set_image(None, f"Anteprima non disponibile:\n{message}")

    def _file_size(self, path: str) -> str:
        try:
            return self.locale().formattedDataSize(Path(path).stat().st_size)
        except OSError:
            return ""

    # --- drag & drop ------------------------------------------------------------------

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls() and not self._busy():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        paths = [Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()]
        self.add_paths(paths)
        event.acceptProposedAction()

    # --- conversione ------------------------------------------------------------------

    def _busy(self) -> bool:
        return self._thread is not None

    def _convert_or_cancel(self) -> None:
        if self._busy():
            if self._worker:
                self._worker.cancel()
            self.button_convert.setEnabled(False)
            self.button_convert.setText("Annullamento…")
            return
        self._start_conversion()

    def _start_conversion(self) -> None:
        output_dir: Path | None = None
        if self.radio_custom.isChecked():
            text = self.edit_output_dir.text().strip()
            if not text:
                QMessageBox.warning(self, "any2jpg", "Scegli la cartella di destinazione.")
                return
            output_dir = Path(text).expanduser()

        items = [self.tree.topLevelItem(row) for row in range(self.tree.topLevelItemCount())]
        rows = [row for row, item in enumerate(items) if not item.data(0, ROLE_DONE)]
        if not rows:
            answer = QMessageBox.question(
                self, "any2jpg", "Tutti i file sono già stati convertiti. Convertirli di nuovo?"
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            rows = list(range(len(items)))

        jobs = []
        for row in rows:
            item = items[row]
            root = item.data(0, ROLE_ROOT)
            jobs.append(Job(row, Path(item.data(0, ROLE_PATH)), Path(root) if root else None))
            item.setText(1, STATUS_PENDING)
            item.setIcon(1, QIcon())
            item.setToolTip(1, "")

        set_fields = self.check_set_fields.isChecked()
        metadata = MetadataOptions(
            keep_original=self.radio_keep.isChecked(),
            author=self.edit_author.text().strip() if set_fields else "",
            copyright=self.edit_copyright.text().strip() if set_fields else "",
        )
        self._save_settings()

        self._errors = 0
        self._bytes_before = 0
        self._bytes_after = 0
        self._open_dir = output_dir
        self.progress.setRange(0, len(jobs))
        self.progress.setValue(0)
        self._thread = QThread(self)
        self._worker = ConvertWorker(
            jobs, self.spin_quality.value(), metadata, output_dir, self.check_overwrite.isChecked(),
            max_side=self.spin_max_side.value() if self.check_resize.isChecked() else 0,
            keep_mtime=self.check_keep_mtime.isChecked(),
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._on_finished)
        self._update_enabled()
        self._thread.start()

    def _status_icon(self, pixmap: QStyle.StandardPixmap) -> QIcon:
        # Da una pixmap piccola: la lista usa icone grandi per le miniature e
        # ingrandirebbe le icone standard fino a THUMB_SIZE.
        return QIcon(self.style().standardIcon(pixmap).pixmap(16, 16))

    @Slot(int, bool, str)
    def _on_progress(self, row: int, ok: bool, message: str) -> None:
        item = self.tree.topLevelItem(row)
        if ok:
            destination = Path(message)
            item.setText(1, destination.name)
            item.setIcon(1, self._status_icon(QStyle.StandardPixmap.SP_DialogApplyButton))
            item.setData(0, ROLE_DONE, True)
            try:
                self._bytes_before += Path(item.data(0, ROLE_PATH)).stat().st_size
                self._bytes_after += destination.stat().st_size
            except OSError:
                pass
            if self._open_dir is None:
                self._open_dir = destination.parent
        else:
            self._errors += 1
            item.setText(1, f"Errore: {message}")
            item.setIcon(1, self._status_icon(QStyle.StandardPixmap.SP_MessageBoxCritical))
        item.setToolTip(1, message)
        self.progress.setValue(self.progress.value() + 1)

    @Slot()
    def _on_finished(self) -> None:
        done = self.progress.value()
        total = self.progress.maximum()
        self._thread.deleteLater()
        self._thread = None
        self._worker = None
        self._update_enabled()

        converted = done - self._errors
        message = f"Convertiti {converted} file su {total}"
        if self._errors:
            message += f", {self._errors} errori (passa il mouse sullo stato per i dettagli)"
        if done < total:
            message += " — annullato"
        self.statusBar().showMessage(message + ".")
        if self.isVisible():  # niente riepilogo se la finestra si sta chiudendo
            self._show_summary(converted, total, cancelled=done < total)

    def _show_summary(self, converted: int, total: int, cancelled: bool) -> None:
        lines = [f"Convertiti {converted} file su {total}."]
        if cancelled:
            lines[0] = f"Conversione annullata: convertiti {converted} file su {total}."
        if converted and self._bytes_before:
            size = self.locale().formattedDataSize
            change = (self._bytes_after - self._bytes_before) / self._bytes_before * 100
            lines.append(
                f"Dimensione: {size(self._bytes_before)} → {size(self._bytes_after)} ({change:+.0f}%)."
            )
        if self._errors:
            lines.append(f"{self._errors} errori: passa il mouse sullo stato per i dettagli.")

        box = QMessageBox(self)
        box.setWindowTitle("any2jpg")
        box.setIcon(QMessageBox.Icon.Warning if self._errors else QMessageBox.Icon.Information)
        box.setText("\n".join(lines))
        open_button = None
        if converted and self._open_dir is not None:
            open_button = box.addButton("Apri cartella", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Close)
        box.exec()
        if open_button is not None and box.clickedButton() is open_button:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._open_dir)))

    def _update_enabled(self) -> None:
        busy = self._busy()
        has_items = self.tree.topLevelItemCount() > 0
        for widget in (
            self.button_add_files, self.button_add_folder, self.check_recursive,
            self.check_include_jpeg, self.slider_quality, self.spin_quality, self.check_resize,
            self.radio_keep, self.radio_strip, self.check_set_fields,
            self.radio_origin, self.radio_custom, self.check_overwrite, self.check_keep_mtime,
        ):
            widget.setEnabled(not busy)
        self.button_remove.setEnabled(not busy and has_items)
        self.button_clear.setEnabled(not busy and has_items)
        self.spin_max_side.setEnabled(not busy and self.check_resize.isChecked())

        fields = not busy and self.check_set_fields.isChecked()
        for widget in (self.edit_author, self.edit_copyright, self.button_fill_copyright):
            widget.setEnabled(fields)
        custom = not busy and self.radio_custom.isChecked()
        self.edit_output_dir.setEnabled(custom)
        self.button_browse.setEnabled(not busy)

        self.button_convert.setEnabled(busy or has_items)
        if busy:
            self.button_convert.setText("Annulla")
            self.button_convert.setIcon(QIcon.fromTheme("process-stop"))
        else:
            self.button_convert.setText("Converti")
            self.button_convert.setIcon(QIcon.fromTheme("media-playback-start"))

    def closeEvent(self, event) -> None:
        if self._thread is not None:
            if self._worker:
                self._worker.cancel()
            self._thread.quit()
            self._thread.wait()
        self._preview_pool.clear()
        self._preview_pool.waitForDone()
        self._save_settings()
        super().closeEvent(event)


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("any2jpg")
    app.setApplicationVersion(__version__)
    # Il portale XDG rifiuta (con un avviso) un app ID senza .desktop installato.
    if QStandardPaths.locate(QStandardPaths.StandardLocation.ApplicationsLocation, "any2jpg.desktop"):
        app.setDesktopFileName("any2jpg")
    window = MainWindow()
    window.show()
    # File passati da riga di comando o da "Apri con…" del file manager.
    window.add_paths(Path(arg) for arg in app.arguments()[1:])
    sys.exit(app.exec())

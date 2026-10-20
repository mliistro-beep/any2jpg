"""Finestra principale di any2jpg (PySide6)."""

from __future__ import annotations

import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from PIL import UnidentifiedImageError
from PySide6.QtCore import QObject, QSettings, QStandardPaths, Qt, QThread, Signal, Slot
from PySide6.QtGui import QIcon, QKeySequence, QShortcut
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
    QSlider,
    QSpinBox,
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


@dataclass(frozen=True)
class Job:
    row: int
    source: Path
    root: Path | None


class ConvertWorker(QObject):
    """Converte i file in sequenza in un thread separato."""

    progress = Signal(int, bool, str)  # riga, riuscito, destinazione o messaggio d'errore
    finished = Signal()

    def __init__(
        self,
        jobs: list[Job],
        quality: int,
        metadata: MetadataOptions,
        output_dir: Path | None,
        overwrite: bool,
    ) -> None:
        super().__init__()
        self._jobs = jobs
        self._quality = quality
        self._metadata = metadata
        self._output_dir = output_dir
        self._overwrite = overwrite
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    @Slot()
    def run(self) -> None:
        reserved: set[Path] = set()
        for job in self._jobs:
            if self._cancelled:
                break
            try:
                destination = converter.plan_destination(
                    job.source, job.root, self._output_dir, self._overwrite, reserved
                )
                reserved.add(destination)
                converter.convert_file(job.source, destination, self._quality, self._metadata)
            except UnidentifiedImageError:
                self.progress.emit(job.row, False, "formato non riconosciuto o file danneggiato")
            except Exception as exc:  # un file rotto non deve fermare il lotto
                self.progress.emit(job.row, False, str(exc) or type(exc).__name__)
            else:
                self.progress.emit(job.row, True, str(destination))
        self.finished.emit()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"any2jpg {DISPLAY_VERSION}")
        self.setWindowIcon(QIcon.fromTheme("image-x-generic"))
        self.setAcceptDrops(True)
        self.resize(920, 680)

        self._settings = QSettings("any2jpg", "any2jpg")
        self._paths: set[Path] = set()
        self._thread: QThread | None = None
        self._worker: ConvertWorker | None = None
        self._errors = 0

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
            "(utile per cambiare qualità o metadati)."
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
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.resizeSection(1, 260)
        QShortcut(QKeySequence.StandardKey.Delete, self.tree, activated=self._remove_selected)
        layout.addWidget(self.tree, stretch=1)

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

    def _build_quality_group(self) -> QGroupBox:
        group = QGroupBox("Qualità JPG")
        row = QHBoxLayout(group)
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
        row.addWidget(self.slider_quality, stretch=1)
        row.addWidget(self.spin_quality)
        row.addWidget(hint)
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
        layout.addWidget(self.radio_origin, 0, 0, 1, 3)
        layout.addWidget(self.radio_custom, 1, 0)
        layout.addWidget(self.edit_output_dir, 1, 1)
        layout.addWidget(self.button_browse, 1, 2)
        layout.addWidget(self.check_overwrite, 2, 0, 1, 3)
        layout.setRowStretch(3, 1)
        return group

    # --- impostazioni ---------------------------------------------------------------

    def _load_settings(self) -> None:
        s = self._settings
        self.spin_quality.setValue(s.value("quality", 90, type=int))
        self.slider_quality.setValue(self.spin_quality.value())
        keep = s.value("keep_metadata", True, type=bool)
        (self.radio_keep if keep else self.radio_strip).setChecked(True)
        self.check_set_fields.setChecked(s.value("set_fields", False, type=bool))
        self.edit_author.setText(s.value("author", "", type=str))
        self.edit_copyright.setText(s.value("copyright", "", type=str))
        self.edit_output_dir.setText(s.value("output_dir", "", type=str))
        self.check_overwrite.setChecked(s.value("overwrite", False, type=bool))
        self.check_recursive.setChecked(s.value("recursive", True, type=bool))
        self.check_include_jpeg.setChecked(s.value("include_jpeg", False, type=bool))

    def _save_settings(self) -> None:
        s = self._settings
        s.setValue("quality", self.spin_quality.value())
        s.setValue("keep_metadata", self.radio_keep.isChecked())
        s.setValue("set_fields", self.check_set_fields.isChecked())
        s.setValue("author", self.edit_author.text())
        s.setValue("copyright", self.edit_copyright.text())
        s.setValue("output_dir", self.edit_output_dir.text())
        s.setValue("overwrite", self.check_overwrite.isChecked())
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
        for path in paths:
            path = path.resolve()
            if path in self._paths:
                continue
            self._paths.add(path)
            item = QTreeWidgetItem([str(path), STATUS_PENDING])
            item.setToolTip(0, str(path))
            item.setData(0, ROLE_PATH, str(path))
            item.setData(0, ROLE_ROOT, str(root.resolve()) if root else "")
            item.setData(0, ROLE_DONE, False)
            self.tree.addTopLevelItem(item)
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
            self._paths.discard(Path(item.data(0, ROLE_PATH)))
            self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(item))
        self._update_enabled()

    def _clear(self) -> None:
        self.tree.clear()
        self._paths.clear()
        self.progress.setValue(0)
        self._update_enabled()

    def _fill_copyright(self) -> None:
        author = self.edit_author.text().strip() or "Nome Cognome"
        self.edit_copyright.setText(f"© {date.today().year} {author}")

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
        self.progress.setRange(0, len(jobs))
        self.progress.setValue(0)
        self._thread = QThread(self)
        self._worker = ConvertWorker(
            jobs, self.spin_quality.value(), metadata, output_dir, self.check_overwrite.isChecked()
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._on_finished)
        self._update_enabled()
        self._thread.start()

    @Slot(int, bool, str)
    def _on_progress(self, row: int, ok: bool, message: str) -> None:
        item = self.tree.topLevelItem(row)
        style = self.style()
        if ok:
            item.setText(1, Path(message).name)
            item.setIcon(1, style.standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton))
            item.setData(0, ROLE_DONE, True)
        else:
            self._errors += 1
            item.setText(1, f"Errore: {message}")
            item.setIcon(1, style.standardIcon(QStyle.StandardPixmap.SP_MessageBoxCritical))
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

    def _update_enabled(self) -> None:
        busy = self._busy()
        has_items = self.tree.topLevelItemCount() > 0
        for widget in (
            self.button_add_files, self.button_add_folder, self.check_recursive,
            self.check_include_jpeg, self.slider_quality, self.spin_quality,
            self.radio_keep, self.radio_strip, self.check_set_fields,
            self.radio_origin, self.radio_custom, self.check_overwrite,
        ):
            widget.setEnabled(not busy)
        self.button_remove.setEnabled(not busy and has_items)
        self.button_clear.setEnabled(not busy and has_items)

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

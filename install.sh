#!/usr/bin/env bash
# Installa any2jpg per l'utente corrente, senza sudo.
#
#   ./install.sh               installa o aggiorna
#   ./install.sh --uninstall   rimuove l'app (le impostazioni restano)
#   ./install.sh --purge       rimuove l'app e le impostazioni
set -euo pipefail

APP=any2jpg
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
PREFIX="$DATA_HOME/$APP"
VENV="$PREFIX/venv"
LAUNCHER="$VENV/bin/$APP"
BIN_DIR="$HOME/.local/bin"
APPS_DIR="$DATA_HOME/applications"
DESKTOP_FILE="$APPS_DIR/$APP.desktop"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/$APP"
PYTHON="${PYTHON:-python3}"

info() { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mAttenzione:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[31mErrore:\033[0m %s\n' "$*" >&2; exit 1; }

refresh_desktop_database() {
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
    fi
}

remove_launcher_link() {
    # Rimuove ~/.local/bin/any2jpg solo se è il nostro collegamento.
    if [[ -L "$BIN_DIR/$APP" && "$(readlink "$BIN_DIR/$APP")" == "$LAUNCHER" ]]; then
        rm -f "$BIN_DIR/$APP"
    fi
}

uninstall() {
    info "Rimozione di $APP"
    remove_launcher_link
    rm -f "$DESKTOP_FILE"
    rm -rf "$PREFIX"
    refresh_desktop_database
    if [[ "${1:-}" == purge ]]; then
        rm -rf "$CONFIG_DIR"
        info "Impostazioni rimosse ($CONFIG_DIR)"
    fi
    info "Fatto."
}

check_requirements() {
    command -v "$PYTHON" >/dev/null 2>&1 || die "$PYTHON non trovato. Installa Python 3.10 o successivo."
    "$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 10))' \
        || die "serve Python 3.10 o successivo (trovato: $("$PYTHON" -V 2>&1))."
    "$PYTHON" -c 'import venv, ensurepip' 2>/dev/null \
        || die "manca il modulo venv. Su Debian/Ubuntu: sudo apt install python3-venv"
    [[ -f "$SRC_DIR/pyproject.toml" && -d "$SRC_DIR/$APP" ]] \
        || die "esegui lo script dalla cartella del progetto ($SRC_DIR non lo è)."
}

install() {
    check_requirements

    local venv_args=()
    if "$PYTHON" -c 'import PySide6.QtWidgets' 2>/dev/null; then
        info "Uso il PySide6 già installato nel sistema"
        venv_args+=(--system-site-packages)
    else
        info "PySide6 non trovato nel sistema: verrà scaricato (circa 200 MB)"
    fi

    info "Creazione dell'ambiente in $VENV"
    rm -rf "$VENV"
    mkdir -p "$PREFIX"
    "$PYTHON" -m venv "${venv_args[@]}" "$VENV"
    "$VENV/bin/python" -m pip install --quiet --upgrade pip

    info "Installazione di $APP e delle dipendenze"
    # --ignore-installed: il Pillow di alcune distribuzioni non legge gli AVIF,
    # quello pubblicato su PyPI sì.
    "$VENV/bin/python" -m pip install --quiet --ignore-installed "Pillow>=11.3"
    "$VENV/bin/python" -m pip install --quiet "$SRC_DIR"

    info "Verifica"
    "$VENV/bin/python" - <<'EOF' || die "verifica fallita: l'installazione è incompleta."
from PIL import features
import PySide6.QtWidgets  # noqa: F401
from any2jpg import DISPLAY_VERSION, converter
missing = [name for name, ok in (("AVIF", features.check("avif")), ("HEIC", converter.HEIF_SUPPORTED)) if not ok]
if missing:
    raise SystemExit("formati non supportati: " + ", ".join(missing))
print(f"    any2jpg {DISPLAY_VERSION}: {len(converter.supported_extensions())} estensioni supportate")
EOF

    info "Collegamento del comando in $BIN_DIR/$APP"
    mkdir -p "$BIN_DIR"
    if [[ -e "$BIN_DIR/$APP" && ! -L "$BIN_DIR/$APP" ]]; then
        die "$BIN_DIR/$APP esiste già e non è un collegamento: rimuovilo e riprova."
    fi
    ln -sfn "$LAUNCHER" "$BIN_DIR/$APP"

    info "Voce nel menu applicazioni"
    mkdir -p "$APPS_DIR"
    sed "s|@BIN@|\"$LAUNCHER\"|" "$SRC_DIR/$APP.desktop" > "$DESKTOP_FILE"
    refresh_desktop_database

    info "Installazione completata."
    case ":$PATH:" in
        *":$BIN_DIR:"*) echo "    Avvia con: $APP (o dal menu applicazioni)" ;;
        *) warn "$BIN_DIR non è nel PATH: avvia dal menu applicazioni o con $LAUNCHER" ;;
    esac
}

case "${1:-}" in
    "") install ;;
    --uninstall) uninstall ;;
    --purge) uninstall purge ;;
    -h|--help) sed -n '2,7s/^# \{0,1\}//p' "$0" ;;
    *) die "opzione sconosciuta: $1 (usa --help)" ;;
esac

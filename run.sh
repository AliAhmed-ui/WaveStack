#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_DIR/venv"
SETUP_MARKER="$PROJECT_DIR/.wavestack-setup-complete"
APP_LAUNCHER_DIR="$HOME/.local/share/applications"
APP_LAUNCHER="$APP_LAUNCHER_DIR/WaveStack.desktop"

cd "$PROJECT_DIR"

detect_linux_family() {
    if [[ "$(uname -s)" != "Linux" ]]; then
        echo "WaveStack currently supports Linux only."
        exit 1
    fi

    if [[ ! -r /etc/os-release ]]; then
        echo "Could not identify your Linux distribution."
        exit 1
    fi

    source /etc/os-release

    case "${ID:-}" in
        ubuntu|debian|linuxmint|pop|zorin|elementary)
            OS_FAMILY="debian"
            ;;
        fedora|rhel|centos|rocky|almalinux)
            OS_FAMILY="fedora"
            ;;
        arch|manjaro|endeavouros|garuda)
            OS_FAMILY="arch"
            ;;
        opensuse*|sles)
            OS_FAMILY="suse"
            ;;
        *)
            case "${ID_LIKE:-}" in
                *debian*|*ubuntu*) OS_FAMILY="debian" ;;
                *fedora*|*rhel*)   OS_FAMILY="fedora" ;;
                *arch*)            OS_FAMILY="arch" ;;
                *suse*)            OS_FAMILY="suse" ;;
                *)
                    echo "Unsupported Linux distribution: ${PRETTY_NAME:-Unknown}"
                    exit 1
                    ;;
            esac
            ;;
    esac

    echo "Detected: ${PRETTY_NAME:-$OS_FAMILY}"
}

install_system_dependencies() {
    echo "Installing WaveStack system dependencies..."

    case "$OS_FAMILY" in
        debian)
            sudo apt-get update
            sudo apt-get install -y python3 python3-venv python3-tk python3-dev build-essential vlc
            ;;
        fedora)
            sudo dnf install -y python3 python3-tkinter python3-devel gcc-c++ vlc
            ;;
        arch)
            sudo pacman -Syu --needed --noconfirm python tk gcc vlc
            ;;
        suse)
            sudo zypper install -y python3 python3-tk python3-devel gcc-c++ vlc
            ;;
    esac
}

install_python_dependencies() {
    echo "Creating WaveStack's Python environment..."
    python3 -m venv "$VENV_DIR"

    echo "Installing WaveStack's Python dependencies..."
    "$VENV_DIR/bin/python3" -m pip install --upgrade pip
    "$VENV_DIR/bin/python3" -m pip install -r "$PROJECT_DIR/requirements.txt"
}

# Builds the native miniaudio engine (wavestack_audio*.so). Runs on every
# launch but is a no-op unless the module is missing or its sources changed.
# Fails soft: without a compiler WaveStack falls back to libVLC.
build_native_module() {
    local py="$VENV_DIR/bin/python3"
    local built
    built="$(compgen -G "$PROJECT_DIR/wavestack_audio*.so" | head -n1 || true)"

    if [[ -n "$built" && "$built" -nt "$PROJECT_DIR/AudioEngine.cpp" \
          && "$built" -nt "$PROJECT_DIR/miniaudio.h" \
          && "$built" -nt "$PROJECT_DIR/setup.py" ]]; then
        return 0
    fi
    if [[ ! -f "$PROJECT_DIR/AudioEngine.cpp" || ! -f "$PROJECT_DIR/miniaudio.h" ]]; then
        return 0
    fi

    echo "Building the native audio engine..."
    local log
    log="$(mktemp)"
    if "$py" -m pip install --quiet pybind11 setuptools >"$log" 2>&1 \
       && "$py" setup.py build_ext --inplace >>"$log" 2>&1; then
        echo "Native audio engine ready."
        rm -f "$log"
    else
        tail -n 15 "$log"
        rm -f "$log"
        echo "Native audio engine build failed (needs g++ and python3-dev); using libVLC instead."
    fi
}

create_desktop_launcher() {
    local desktop_dir
    desktop_dir="$(xdg-user-dir DESKTOP 2>/dev/null || true)"
    desktop_dir="${desktop_dir:-$HOME/Desktop}"

    mkdir -p "$APP_LAUNCHER_DIR" "$desktop_dir"

    cat > "$APP_LAUNCHER" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=WaveStack
Comment=1990s Offline MP3 Player
Exec="$PROJECT_DIR/run.sh"
Path=$PROJECT_DIR
Icon=multimedia-audio-player
Terminal=false
Categories=AudioVideo;Audio;Player;
EOF

    chmod +x "$APP_LAUNCHER"
    cp "$APP_LAUNCHER" "$desktop_dir/WaveStack.desktop"
    chmod +x "$desktop_dir/WaveStack.desktop"

    # Trust the desktop shortcut in GNOME when supported.
    if command -v gio >/dev/null 2>&1; then
        gio set "$desktop_dir/WaveStack.desktop" metadata::trusted true 2>/dev/null || true
    fi

    update-desktop-database "$APP_LAUNCHER_DIR" 2>/dev/null || true
}

if [[ ! -f "$SETUP_MARKER" || ! -x "$VENV_DIR/bin/python3" ]]; then
    detect_linux_family
    install_system_dependencies
    install_python_dependencies
    create_desktop_launcher
    touch "$SETUP_MARKER"
else
    create_desktop_launcher
fi

build_native_module

echo "Starting WaveStack..."
exec "$VENV_DIR/bin/python3" "$PROJECT_DIR/wavestack.py" "$@"

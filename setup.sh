#!/data/data/com.termux/files/usr/bin/bash
# =============================================================================
# Retail AI Core — Termux bootstrap for POCO F3 (Snapdragon 870, ARM64)
#
# Idempotent: safe to re-run. Sets up:
#   1. 2GB Linux swap file          (prevents Android OOM kills)
#   2. Toolchain + Python + OpenCV
#   3. llama.cpp built for the SD870 (native armv8.2a+dotprod optimizations)
#   4. MiniCPM-V GGUF model download
#   5. termux-services unit + wake lock
#
# Usage:  chmod +x setup.sh && ./setup.sh
# =============================================================================
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

# --- configurable knobs ------------------------------------------------------
# Verified (2026-10): openbmb/MiniCPM-V-4.7-1B-gguf exists and is open.
# MiniCPM-V needs BOTH the LLM GGUF and the mmproj vision projector.
MODEL_REPO_URL="${MODEL_REPO_URL:-https://huggingface.co/openbmb/MiniCPM-V-4.7-1B-gguf/resolve/main}"
MODEL_FILE="${MODEL_FILE:-MiniCPM-V-4_7-1B-Q4_K_M.gguf}"
MMPROJ_FILE="${MMPROJ_FILE:-mmproj-model-f16.gguf}"
MODEL_DIR="${MODEL_DIR:-models}"
LLAMA_CPP_URL="${LLAMA_CPP_URL:-https://github.com/ggerganov/llama.cpp.git}"
SWAP_MB="${SWAP_MB:-2048}"

log()  { printf '\033[1;34m[setup]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[setup]\033[0m %s\n' "$*" >&2; exit 1; }

# --- 0. sanity -----------------------------------------------------------------
command -v pkg >/dev/null 2>&1 || fail "This script must run inside Termux."
log "Detected Termux environment: $(uname -m)"

# --- 1. base packages ----------------------------------------------------------
log "Updating package index and installing base packages..."
pkg update -y
pkg install -y git cmake clang make python python-pip \
    opencv python-numpy ndk-multilib termux-api termux-services jq wget curl || \
    fail "pkg install failed"
# Python bindings for cv2 (separate package; tolerate absence on old mirrors)
pkg install -y opencv-python >/dev/null 2>&1 || \
    log "WARNING: opencv-python package not found — cv2 may be missing"

# --- 2. 2GB swap (Android OOM protection; needs root on most devices) ----------
SWAP_FILE="$HOME/.retail-ai-swap.img"
if ! command -v mkswap >/dev/null 2>&1 || ! command -v swapon >/dev/null 2>&1; then
    log "swap tools missing — trying root-repo util-linux (requires a rooted device)..."
    pkg install -y root-repo util-linux >/dev/null 2>&1 || true
fi
if command -v mkswap >/dev/null 2>&1 && command -v swapon >/dev/null 2>&1; then
    if swapon --show 2>/dev/null | grep -q "$SWAP_FILE"; then
        log "Swap file already active: $SWAP_FILE"
    else
        if [ ! -f "$SWAP_FILE" ]; then
            log "Creating ${SWAP_MB}MB swap file (this takes a minute)..."
            dd if=/dev/zero of="$SWAP_FILE" bs=1M count="$SWAP_MB" status=progress
            chmod 600 "$SWAP_FILE"
            mkswap "$SWAP_FILE"
        fi
        log "Enabling swap..."
        if swapon "$SWAP_FILE" 2>/dev/null; then
            # Persist across reboots (Termux boots run ~/.bashrc on session start)
            grep -q "retail-ai-swap" "$HOME/.bashrc" 2>/dev/null || \
                echo "swapon $SWAP_FILE 2>/dev/null || true  # retail-ai swap" >> "$HOME/.bashrc"
            log "Swap active. Verify with: free -h"
        else
            log "WARNING: swapon failed (kernel blocks swap without root?). Continuing without swap."
        fi
    fi
else
    log "WARNING: skipping swap (non-rooted device). Android zram still helps;"
    log "         for heavy inference consider rooting, or use a smaller GGUF quant."
fi

# --- 3. python virtualenv + dependencies ---------------------------------------
# NOTE: cv2 comes from the system package `opencv-python` (Termux currently has
# no PyPI opencv wheels for aarch64 + Python 3.14), so the venv reuses the
# system site-packages via --system-site-packages.
log "Creating Python virtualenv (with system site-packages)..."
python -m venv --system-site-packages .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --upgrade pip >/dev/null
pip install requests python-dotenv || \
    log "WARNING: pip install failed (network?) — daemon needs requests"
python -c "import cv2" 2>/dev/null && log "cv2 importable" || \
    log "WARNING: cv2 not importable — run: pkg install opencv-python python-numpy"
log "Python deps ready."

# --- 4. llama.cpp native build --------------------------------------------------
if [ -x "llama.cpp/build/bin/llama-minicpmv-cli" ]; then
    log "llama-minicpmv-cli already built; skipping."
else
    log "Cloning and building llama.cpp for Snapdragon 870 (armv8.2a+dotprod)..."
    if [ ! -d llama.cpp ]; then
        git clone --depth 1 "$LLAMA_CPP_URL" llama.cpp
    fi
    cmake -S llama.cpp -B llama.cpp/build \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_C_FLAGS="-O3 -march=armv8.2a+dotprod" \
        -DCMAKE_CXX_FLAGS="-O3 -march=armv8.2a+dotprod" \
        -DGGML_NATIVE=OFF \
        -DGGML_OPENMP=OFF \
        -DLLAMA_CURL=OFF \
        -DLLAMA_BUILD_EXAMPLES=OFF \
        -DLLAMA_BUILD_TESTS=OFF \
        -DLLAMA_BUILD_SERVER=OFF
    cmake --build llama.cpp/build --config Release \
        --target llama-minicpmv-cli -j"$(nproc)"
    log "llama.cpp build finished."
fi

# --- 5. MiniCPM-V model download ------------------------------------------------
mkdir -p "$MODEL_DIR"
for pair in "MODEL_FILE:$MODEL_FILE" "MMPROJ_FILE:$MMPROJ_FILE"; do
    key="${pair%%:*}"; file="${pair#*:}"
    if [ -f "$MODEL_DIR/$file" ]; then
        log "$key already present: $MODEL_DIR/$file"
        continue
    fi
    log "Downloading $key ($file, may take a while)..."
    curl -fL --retry 3 -C - \
        "$MODEL_REPO_URL/$file" \
        -o "$MODEL_DIR/$file" || \
        fail "Download failed for $file. Verify against the file listing:
        https://huggingface.co/openbmb/MiniCPM-V-4.7-1B-gguf/tree/main"
done

# --- 6. config + directories -----------------------------------------------------
[ -f config.env ] || { cp config.env.example config.env; log "Created config.env — EDIT IT with your BOT_TOKEN etc."; }
mkdir -p data snapshots logs

# --- 7. termux-services unit ------------------------------------------------------
# termux-services convention: services live in ~/.termux/service/<name>/run;
# `sv-enable <name>` symlinks them into $PREFIX/var/service for runit.
SVDIR="$HOME/.termux/service/retail-ai"
mkdir -p "$SVDIR"
cat > "$SVDIR/run" <<EOF
#!/data/data/com.termux/files/usr/bin/bash
# termux-services (runit) entrypoint for the Retail AI daemon
cd "$REPO_DIR" || exit 1
exec "$REPO_DIR/.venv/bin/python" "$REPO_DIR/core/main.py" >> "$REPO_DIR/logs/service.log" 2>&1
EOF
chmod +x "$SVDIR/run"
# Remove any older copy placed in the wrong (system) location
rm -rf "$PREFIX/var/service/retail-ai"
log "Service installed at $SVDIR — enable with: sv-enable retail-ai && sv up retail-ai"

# --- 8. wake lock ----------------------------------------------------------------
termux-wake-lock 2>/dev/null && log "Wake lock acquired (keeps CPU alive in deep sleep)." || \
    log "termux-wake-lock failed — run it manually after granting Termux:API permissions."

# --- done ------------------------------------------------------------------------
cat <<DONE

================================================================================
 Setup complete.
 Next steps:
   1. Edit config.env  (BOT_TOKEN, STATE_CHANNEL_ID, cameras, capacity)
   2. Quick test:      .venv/bin/python -c "from core.config import Config; print(Config.load())"
   3. Start daemon:    sv-enable retail-ai && sv up retail-ai
   4. Check logs:      tail -f logs/service.log
================================================================================
DONE

#!/usr/bin/env bash
# smart-ai-router + claudish-smart installer
#
# Single-line installation:
#   curl -fsSL https://raw.githubusercontent.com/gaaschk/smart-ai-router/main/install.sh | bash
#
# Or with options:
#   curl -fsSL https://... | bash -s -- [--no-setup] [--install-dir /custom/path]

set -euo pipefail

# ══════════════════════════════════════════════════════════════════════════════
# Parse arguments
# ══════════════════════════════════════════════════════════════════════════════

REPO_URL="${REPO_URL:-https://github.com/gaaschk/smart-ai-router.git}"
INSTALL_DIR="${INSTALL_DIR:-}"
SKIP_SETUP="${SKIP_SETUP:-0}"
REPO_BRANCH="${REPO_BRANCH:-main}"

while [[ $# -gt 0 ]]; do
  case $1 in
    --no-setup)
      SKIP_SETUP=1
      shift
      ;;
    --install-dir)
      INSTALL_DIR="$2"
      shift 2
      ;;
    --help)
      cat <<EOF
Usage: $0 [OPTIONS]

Options:
  --no-setup              Skip the interactive setup wizard
  --install-dir PATH      Install to PATH (default: $HOME/smart-ai-router)
  --help                  Show this help message

Environment variables:
  REPO_URL                Repository URL (default: https://github.com/gaaschk/smart-ai-router.git)
  REPO_BRANCH             Git branch to checkout (default: main)

Examples:
  # One-line install with interactive setup
  curl -fsSL https://raw.githubusercontent.com/gaaschk/smart-ai-router/main/install.sh | bash

  # Install without running setup
  curl -fsSL https://... | bash -s -- --no-setup

  # Install to custom directory
  curl -fsSL https://... | bash -s -- --install-dir /opt/smart-ai-router
EOF
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 1
      ;;
  esac
done

# Set default install dir if not specified
if [ -z "$INSTALL_DIR" ]; then
  INSTALL_DIR="$HOME/smart-ai-router"
fi

# Colors for output
_RED='\033[0;31m'
_GREEN='\033[0;32m'
_YELLOW='\033[1;33m'
_BOLD='\033[1m'
_NC='\033[0m' # No Color

# ══════════════════════════════════════════════════════════════════════════════
# Functions
# ══════════════════════════════════════════════════════════════════════════════

log_info() {
  echo -e "${_GREEN}✓${_NC} $*"
}

log_bold() {
  echo -e "${_BOLD}$*${_NC}"
}

log_warn() {
  echo -e "${_YELLOW}⚠${_NC} $*" >&2
}

log_error() {
  echo -e "${_RED}✗${_NC} $*" >&2
}

die() {
  log_error "$*"
  exit 1
}

check_command() {
  if ! command -v "$1" &> /dev/null; then
    die "Required command not found: $1"
  fi
}

# ══════════════════════════════════════════════════════════════════════════════
# Pre-flight checks
# ══════════════════════════════════════════════════════════════════════════════

echo ""
log_bold "smart-ai-router + claudish-smart installer"
echo "=============================================="
echo ""

# Check OS
if [[ "$OSTYPE" != "darwin"* ]]; then
  log_warn "This installer is designed for macOS. Continuing anyway..."
fi

# Check dependencies
log_bold "Checking dependencies..."
check_command git
log_info "git"
check_command python3
log_info "python3"
check_command curl
log_info "curl"

# Check if claudish is installed
if ! command -v claudish &> /dev/null; then
  log_warn "claudish not found. Install it with: pip install claudish"
  log_warn "After installing claudish, you can run claudish-smart"
fi

echo ""

# ══════════════════════════════════════════════════════════════════════════════
# Clone or update repository
# ══════════════════════════════════════════════════════════════════════════════

log_bold "Setting up smart-ai-router..."

if [ -d "$INSTALL_DIR/.git" ]; then
  log_info "Updating existing installation at $INSTALL_DIR"
  cd "$INSTALL_DIR"
  git fetch origin
  git checkout "$REPO_BRANCH"
  git pull origin "$REPO_BRANCH"
else
  log_info "Cloning from $REPO_URL"
  git clone --branch "$REPO_BRANCH" "$REPO_URL" "$INSTALL_DIR"
  cd "$INSTALL_DIR"
fi

log_info "Repository ready at $INSTALL_DIR"
echo ""

# ══════════════════════════════════════════════════════════════════════════════
# Python virtual environment
# ══════════════════════════════════════════════════════════════════════════════

log_bold "Setting up Python environment..."

if [ ! -d "$INSTALL_DIR/.venv" ]; then
  log_info "Creating virtual environment..."
  python3 -m venv "$INSTALL_DIR/.venv"
else
  log_info "Virtual environment exists"
fi

source "$INSTALL_DIR/.venv/bin/activate"
log_info "Virtual environment activated"

# Upgrade pip and install the package
log_info "Installing smart-ai-router..."
pip install -q --upgrade pip setuptools wheel
pip install -q -e "$INSTALL_DIR"

log_info "Installation complete"
echo ""

# ══════════════════════════════════════════════════════════════════════════════
# Run setup wizard
# ══════════════════════════════════════════════════════════════════════════════

if [ "$SKIP_SETUP" = "0" ]; then
  log_bold "Running setup wizard..."
  echo "(You can skip this and configure later with: smart-ai-router setup)"
  echo ""
  smart-ai-router setup
else
  log_warn "Setup wizard skipped (SKIP_SETUP=1)"
  echo ""
  echo "To configure providers and install the service, run:"
  echo "  source $INSTALL_DIR/.venv/bin/activate"
  echo "  smart-ai-router setup"
fi

echo ""
echo "=============================================="
log_bold "Installation complete!"
echo ""
echo "To launch Claude Code with routing:"
echo "  claudish-smart"
echo ""
echo "To manage the router service:"
echo "  launchctl list | grep smart-ai-router     # check status"
echo "  launchctl kickstart -k gui/\$(id -u)/com.smart-ai-router  # restart"
echo ""

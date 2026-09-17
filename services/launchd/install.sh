#!/bin/bash
#
# Smart-AI-Router Service Installation Script
#
# This script installs and loads the smart-ai-router service as a macOS background process.
# (The unified dashboard has been retired in favor of the built-in Python web UI at :8001.)
#
# Usage:
#   ./install.sh
#   ./install.sh --uninstall  (to remove the service)
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCHD_DIR="$HOME/Library/LaunchAgents"

# Service files
SERVICES=(
    "com.smart-ai-router.plist"
)

SERVICE_LABELS=(
    "com.smart-ai-router"
)

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

function print_header() {
    echo -e "\n${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}\n"
}

function print_success() {
    echo -e "${GREEN}✅ $1${NC}"
}

function print_error() {
    echo -e "${RED}❌ $1${NC}"
}

function print_warning() {
    echo -e "${YELLOW}⚠️  $1${NC}"
}

function print_info() {
    echo -e "${BLUE}ℹ️  $1${NC}"
}

function uninstall() {
    print_header "Uninstalling Services"

    for label in "${SERVICE_LABELS[@]}"; do
        if launchctl list "$label" &>/dev/null; then
            print_info "Unloading $label..."
            launchctl unload "$LAUNCHD_DIR/$label.plist" 2>/dev/null || true
        fi

        plist_file="$LAUNCHD_DIR/$label.plist"
        if [ -f "$plist_file" ]; then
            rm "$plist_file"
            print_success "Removed $label"
        fi
    done

    print_success "All services uninstalled!"
}

function install() {
    print_header "Installing Smart-AI-Router Service"
    
    # Check if launchd directory exists
    if [ ! -d "$LAUNCHD_DIR" ]; then
        mkdir -p "$LAUNCHD_DIR"
        print_success "Created LaunchAgents directory"
    fi

    # Copy service files
    print_info "Copying service files..."
    for service in "${SERVICES[@]}"; do
        if [ ! -f "$SCRIPT_DIR/$service" ]; then
            print_error "Service file not found: $service"
            return 1
        fi

        cp "$SCRIPT_DIR/$service" "$LAUNCHD_DIR/"
        chmod 644 "$LAUNCHD_DIR/$service"
        print_success "Installed $service"
    done

    # Load services
    print_info "\nLoading services..."
    for label in "${SERVICE_LABELS[@]}"; do
        plist_file="$LAUNCHD_DIR/$label.plist"

        # Unload if already loaded
        if launchctl list "$label" &>/dev/null; then
            launchctl unload "$plist_file" 2>/dev/null || true
        fi

        # Load service
        launchctl load "$plist_file" 2>/dev/null || {
            print_error "Failed to load $label"
            return 1
        }
        print_success "Loaded $label"
    done

    # Wait for services to start
    sleep 3

    # Check status
    print_header "Service Status"
    for label in "${SERVICE_LABELS[@]}"; do
        if launchctl list "$label" &>/dev/null; then
            print_success "$label is running"
        else
            print_warning "$label may not be running yet (give it 5-10 seconds)"
        fi
    done

    # Test connectivity
    print_header "Testing Connectivity"

    # Test smart-ai-router
    if curl -s http://localhost:8001/health &>/dev/null; then
        print_success "Smart-AI-Router (port 8001) is responding"
    else
        print_warning "Smart-AI-Router (port 8001) is not responding yet - it may still be starting"
    fi

    print_header "Installation Complete!"
    echo -e "Service has been installed and loaded. It will start automatically on boot.\n"
    echo -e "Next steps:"
    echo -e "  1. Wait 10-15 seconds for the service to fully start"
    echo -e "  2. Open your browser to: ${GREEN}http://localhost:8001${NC}"
    echo -e "  3. Use your admin API key to authenticate\n"
    echo -e "Useful commands:"
    echo -e "  Check status:    ${BLUE}launchctl list com.smart-ai-router${NC}"
    echo -e "  View logs:       ${BLUE}tail -f ~/Library/Logs/dashboard/smartrouter.log${NC}"
    echo -e "  Stop service:    ${BLUE}launchctl stop com.smart-ai-router${NC}"
    echo -e "  Uninstall:       ${BLUE}$0 --uninstall${NC}\n"
}

# Main
case "${1:-}" in
    --uninstall)
        uninstall
        ;;
    *)
        install
        ;;
esac

#!/bin/bash
#
# Unified Dashboard Services Installation Script
#
# This script installs and loads all dashboard services as macOS background processes.
#
# Usage:
#   ./install.sh
#   ./install.sh --uninstall  (to remove all services)
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCHD_DIR="$HOME/Library/LaunchAgents"

# Service files
SERVICES=(
    "com.dashboard.backend.plist"
    "com.dashboard.frontend.plist"
    "com.smartrouter.api.plist"
)

SERVICE_LABELS=(
    "com.dashboard.backend"
    "com.dashboard.frontend"
    "com.smartrouter.api"
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
    print_header "Installing Dashboard Services"

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

    # Test backend
    if curl -s http://localhost:5050/health &>/dev/null; then
        print_success "Dashboard Backend (port 5050) is responding"
    else
        print_warning "Dashboard Backend (port 5050) is not responding yet - it may still be starting"
    fi

    # Test frontend
    if curl -s http://localhost:5173 &>/dev/null; then
        print_success "Dashboard Frontend (port 5173) is responding"
    else
        print_warning "Dashboard Frontend (port 5173) is not responding yet - it may still be starting"
    fi

    print_header "Installation Complete!"
    echo -e "Services have been installed and loaded. They will start automatically on boot.\n"
    echo -e "Next steps:"
    echo -e "  1. Wait 10-15 seconds for services to fully start"
    echo -e "  2. Open your browser to: ${GREEN}http://localhost:5173${NC}"
    echo -e "  3. Sign in with your admin credentials\n"
    echo -e "Useful commands:"
    echo -e "  Check status:    ${BLUE}launchctl list | grep -E 'dashboard|smartrouter'${NC}"
    echo -e "  View logs:       ${BLUE}tail -f /var/log/dashboard-backend.log${NC}"
    echo -e "  Stop service:    ${BLUE}launchctl stop com.dashboard.backend${NC}"
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

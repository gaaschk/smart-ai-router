#!/usr/bin/env bash
# sudoers-setup.sh — Grant the service user passwordless sudo for exactly one
# launchctl command: restarting the smart-ai-router daemon.
#
# Run once on the Mac mini as root (or via sudo):
#   sudo ./scripts/sudoers-setup.sh
#
set -euo pipefail

USER="${1:-kevingaasch}"
RULE_FILE="/etc/sudoers.d/smart-ai-router"

cat > "$RULE_FILE" <<EOF
# Allow $USER to restart the smart-ai-router launchd daemon without a password.
# Scoped to exactly this one command — no broader sudo access is granted.
$USER ALL=(root) NOPASSWD: /bin/launchctl kickstart -k system/com.kevingaasch.smart-ai-router
EOF

chmod 440 "$RULE_FILE"
visudo -c -f "$RULE_FILE"

echo "✓ Sudoers rule installed at $RULE_FILE"

#!/bin/sh
# The Tailscale CLI for Jarvis's own userspace tailscaled (LaunchAgent com.sanjar.jarvis-tailscale).
#   scripts/ts.sh status        scripts/ts.sh ip -4        scripts/ts.sh up --hostname=jarvis-mac
exec /opt/homebrew/bin/tailscale --socket="$HOME/.local/share/jarvis-tailscale/tailscaled.sock" "$@"

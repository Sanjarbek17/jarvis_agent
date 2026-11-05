#!/bin/sh
# Public HTTPS for the Jarvis phone app (ticket 31ed52a7): Cloudflare tunnel "jarvis-app",
# https://jarvis.sanjarbek17.uz (app API and website) -> the app server on :8790. Run by launchd
# (~/Library/LaunchAgents/com.sanjar.jarvis-app-tunnel.plist, KeepAlive).
#
# Safety: the tunnel only opens once the running Jarvis has the login code (/api/health says
# "login": true). Older Jarvis code accepted the APK's built-in token from anywhere; the new code
# refuses it on tunnel requests, so only a Telegram-code login works from outside.
CONFIG="$HOME/.cloudflared/jarvis-app.yml"
until curl -fsS --max-time 5 http://127.0.0.1:8790/api/health 2>/dev/null | grep -q '"login":true'; do
    sleep 30
done
exec /opt/homebrew/bin/cloudflared tunnel --no-autoupdate --config "$CONFIG" run jarvis-app

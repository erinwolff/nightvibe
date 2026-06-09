#!/usr/bin/env bash
# Install the host-side playback pieces:
#   - scripts  -> /usr/local/bin       (symlinked back to this repo)
#   - units    -> ~/.config/systemd/user
# Then reload systemd and enable the timers. Idempotent; re-run after edits.
#
# This installs USER units (chosen path: linger is enabled, socket/uid align
# cleanly with the container). It does NOT use sudo for the units; it does for
# the /usr/local/bin symlinks.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

SCRIPTS=(start-music ensure-sink play-now start-shuffle bt-assert resolve-bt-sink bt-disconnect ensure-easyeffects)
UNITS=(music-night.service music-night.timer music-stop.service music-stop.timer \
       music-playnow.service music-playnow.path music-shuffle.service music-shuffle.path)

echo "==> Symlinking scripts into /usr/local/bin (needs sudo)"
for s in "${SCRIPTS[@]}"; do
  chmod +x "$HERE/$s"
  sudo ln -sfn "$HERE/$s" "/usr/local/bin/$s"
  echo "    /usr/local/bin/$s -> $HERE/$s"
done

echo "==> Installing user units into $USER_UNIT_DIR"
mkdir -p "$USER_UNIT_DIR"
for u in "${UNITS[@]}"; do
  cp -f "$HERE/$u" "$USER_UNIT_DIR/$u"
  echo "    $USER_UNIT_DIR/$u"
done

echo "==> Reloading user systemd and enabling timers + on-demand watchers"
systemctl --user daemon-reload
systemctl --user enable --now music-night.timer music-stop.timer \
  music-playnow.path music-shuffle.path

echo "==> Ensuring linger is enabled (units run without an active login)"
if ! loginctl show-user "$USER" 2>/dev/null | grep -q "Linger=yes"; then
  sudo loginctl enable-linger "$USER"
  echo "    enabled linger for $USER"
else
  echo "    linger already enabled"
fi

echo
echo "Done. Check with:"
echo "  systemctl --user list-timers 'music-*'"
echo "  systemctl --user status music-night.service"

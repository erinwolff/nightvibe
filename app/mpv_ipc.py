"""Minimal JSON-RPC client for mpv's --input-ipc-server unix socket.

The mpv IPC protocol is line-delimited JSON: send `{"command": [...], "request_id": N}\n`,
read back `{"error": "success", "data": ..., "request_id": N}`. Async `{"event": ...}`
messages are interleaved and ignored here.

mpv only runs 23:00-08:00, so the socket is absent most of the day. That is a NORMAL
state, not an error: status() returns {"playing": False} and control methods return
False rather than raising. No third-party dependencies.
"""
from __future__ import annotations

import json
import os
import socket
from typing import Any, Optional

# Container path by default; bind-mounted from the host's
# /run/user/<uid>/music-night/mpv.sock. Override with MPV_SOCKET.
SOCKET_PATH = os.environ.get("MPV_SOCKET", "/run/music-night/mpv.sock")


class MpvNotRunning(Exception):
    """The socket is absent or mpv is not accepting connections (the daytime norm)."""


class MpvError(Exception):
    """mpv accepted the command but returned a non-success error."""


class MpvIPC:
    def __init__(self, path: str = SOCKET_PATH, timeout: float = 1.0):
        self.path = path
        self.timeout = timeout

    # --- transport -------------------------------------------------------
    def _command(self, *args: Any) -> Any:
        """Send one command, return its `data`. Raise MpvNotRunning if mpv is down."""
        if not os.path.exists(self.path):
            raise MpvNotRunning(self.path)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(self.timeout)
                s.connect(self.path)
                req = json.dumps({"command": list(args), "request_id": 1}) + "\n"
                s.sendall(req.encode("utf-8"))
                buf = b""
                while True:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            msg = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        # Skip async events; wait for our reply.
                        if msg.get("request_id") != 1 or "error" not in msg:
                            continue
                        if msg["error"] != "success":
                            raise MpvError(msg["error"])
                        return msg.get("data")
        except (ConnectionRefusedError, FileNotFoundError, BrokenPipeError, OSError) as e:
            # Socket file lingered but mpv is gone / not listening.
            raise MpvNotRunning(str(e))
        raise MpvNotRunning("no response from mpv")

    def _get(self, prop: str) -> Optional[Any]:
        """get_property tolerant of per-property errors (e.g. unavailable while idle)."""
        try:
            return self._command("get_property", prop)
        except MpvError:
            return None

    # --- queries ---------------------------------------------------------
    def is_running(self) -> bool:
        try:
            self._command("get_property", "idle-active")
            return True
        except MpvNotRunning:
            return False

    def status(self) -> dict:
        """Now-playing snapshot. {'playing': False} when mpv isn't running."""
        try:
            meta = self._get("metadata") or {}
        except MpvNotRunning:
            return {"playing": False}

        def m(*keys: str) -> Optional[str]:
            for k in keys:
                for variant in (k, k.lower(), k.upper(), k.title()):
                    if variant in meta:
                        return meta[variant]
            return None

        return {
            "playing": True,
            "paused": bool(self._get("pause")),
            "title": self._get("media-title"),
            "artist": m("artist", "ARTIST", "album_artist"),
            "album": m("album", "ALBUM"),
            "path": self._get("path"),
            "position": self._get("time-pos"),
            "duration": self._get("duration"),
            "volume": self._get("volume"),
            "playlist_pos": self._get("playlist-pos-1"),
            "playlist_count": self._get("playlist-count"),
        }

    # --- controls (return False when mpv isn't running) ------------------
    def _try(self, *args: Any) -> bool:
        try:
            self._command(*args)
            return True
        except MpvNotRunning:
            return False

    def skip(self) -> bool:
        return self._try("playlist-next", "force")

    def previous(self) -> bool:
        return self._try("playlist-prev", "force")

    def toggle_pause(self) -> bool:
        return self._try("cycle", "pause")

    def set_volume(self, volume: float) -> bool:
        volume = max(0.0, min(130.0, float(volume)))
        return self._try("set_property", "volume", volume)

    def stop(self) -> bool:
        """Quit mpv -> the systemd service goes inactive and ExecStopPost disconnects
        the speaker. Playback resumes at the next 23:00 timer."""
        return self._try("quit")

    def play_next(self, path: str) -> bool:
        """Jump `path` to the front of the LIVE player and play it now, leaving
        loop/shuffle untouched so the pool resumes once it finishes.

        Appends the track, then seeks to it — mpv was started with
        --loop-playlist=inf, so after this song it wraps back into the shuffled
        pool. Returns False if mpv isn't running, so the caller cold-starts a
        one-off player instead (see app/main.play_now)."""
        try:
            count = self._command("get_property", "playlist-count")
            self._command("loadfile", path, "append")
            self._command("set_property", "pause", False)
            if isinstance(count, int) and count >= 1:
                # Appended entry sits at index == old count; seek to it now.
                self._command("set_property", "playlist-pos", count)
            else:
                # Idle/empty player: the appended entry is the only one (index 0).
                self._command("set_property", "playlist-pos", 0)
            return True
        except MpvNotRunning:
            return False


if __name__ == "__main__":
    import sys

    path = os.environ.get("MPV_SOCKET", f"/run/user/{os.getuid()}/music-night/mpv.sock")
    mpv = MpvIPC(path)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "status":
        print(json.dumps(mpv.status(), indent=2, default=str))
    elif cmd == "skip":
        print("skipped" if mpv.skip() else "not running")
    elif cmd == "prev":
        print("previous" if mpv.previous() else "not running")
    elif cmd == "pause":
        print("toggled" if mpv.toggle_pause() else "not running")
    elif cmd == "vol":
        print("ok" if mpv.set_volume(sys.argv[2]) else "not running")
    elif cmd == "stop":
        print("stopped" if mpv.stop() else "not running")
    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        sys.exit(2)

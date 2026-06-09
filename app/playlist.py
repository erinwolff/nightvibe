"""Shuffle-pool curation: scan the music library and build/edit the .m3u.

The playlist is consumed by mpv on the HOST, so entries are absolute host paths.
For the container to write host-valid paths, the library is bind-mounted at the
SAME path inside the container as on the host (see compose.yaml). No third-party
dependencies; track display uses filenames/relative paths rather than tag reads.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

MUSIC_DIR = Path(os.environ.get("MUSIC_DIR", "/home/littlemiss/Music"))
PLAYLIST_PATH = Path(os.environ.get("PLAYLIST_PATH", "/data/playlist.m3u"))

# On-demand request files in the shared run/ dir. The host watches these with
# systemd .path units; the app writes them when mpv isn't already running.
PLAYNOW_REQUEST = Path(os.environ.get("PLAYNOW_REQUEST", "/run/music-night/playnow.request"))
SHUFFLE_REQUEST = Path(os.environ.get("SHUFFLE_REQUEST", "/run/music-night/shuffle.request"))

AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".opus", ".ogg", ".oga", ".wav", ".aac", ".wma"}


def _is_audio(p: Path) -> bool:
    return p.suffix.lower() in AUDIO_EXTS


# --- library ------------------------------------------------------------
def scan_library(root: Path | None = None) -> list[str]:
    """All audio files under the library, as sorted absolute paths."""
    root = root or MUSIC_DIR
    out: list[str] = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".")]  # prune hidden dirs
        for f in files:
            p = Path(dirpath) / f
            if _is_audio(p):
                out.append(str(p))
    out.sort()
    return out


def browse(subpath: str = "") -> dict:
    """One directory level, for a folder-browser UI. Paths are absolute.

    Returns {"path", "parent", "dirs": [{name, path}], "tracks": [{name, path}]}.
    `subpath` is interpreted relative to MUSIC_DIR; traversal outside is rejected.
    """
    base = (MUSIC_DIR / subpath).resolve()
    root = MUSIC_DIR.resolve()
    if root != base and root not in base.parents:
        raise ValueError("path escapes music library")
    if not base.is_dir():
        raise NotADirectoryError(str(base))

    dirs, tracks = [], []
    for entry in sorted(base.iterdir(), key=lambda e: e.name.lower()):
        if entry.name.startswith("."):  # skip .stfolder, .thumbnails, etc.
            continue
        if entry.is_dir():
            dirs.append({"name": entry.name, "path": str(entry)})
        elif _is_audio(entry):
            tracks.append({"name": entry.name, "path": str(entry)})

    parent = "" if base == root else str(base.parent.relative_to(root))
    if parent == ".":  # top-level dir -> parent is the library root
        parent = ""
    return {
        "path": "" if base == root else str(base.relative_to(root)),
        "parent": parent,
        "dirs": dirs,
        "tracks": tracks,
    }


def search(query: str, limit: int = 200) -> dict:
    """Case-insensitive search across the library for folders and tracks.

    Matches folder names and track relative-paths. Returns
    {"query", "dirs": [{name, path, rel}], "tracks": [{name, path}], "truncated"}.
    `dirs` let you add a whole matching album; `tracks` are individual matches.
    """
    q = query.strip().lower()
    if not q:
        return {"query": query, "dirs": [], "tracks": [], "truncated": False}

    root = MUSIC_DIR.resolve()
    dirs: list[dict] = []
    tracks: list[dict] = []
    truncated = False
    for dirpath, dnames, files in os.walk(root):
        dnames[:] = [d for d in dnames if not d.startswith(".")]
        for d in dnames:
            if q in d.lower():
                p = Path(dirpath) / d
                dirs.append({"name": d, "path": str(p), "rel": str(p.relative_to(root))})
        for f in files:
            p = Path(dirpath) / f
            if not _is_audio(p):
                continue
            rel = str(p.relative_to(root))
            if q in rel.lower():
                tracks.append({"name": rel, "path": str(p)})
        if len(dirs) + len(tracks) > limit * 2:  # bail early on very broad queries
            truncated = True
            break

    dirs.sort(key=lambda x: x["rel"].lower())
    tracks.sort(key=lambda x: x["name"].lower())
    if len(dirs) > limit:
        dirs, truncated = dirs[:limit], True
    if len(tracks) > limit:
        tracks, truncated = tracks[:limit], True
    return {"query": query, "dirs": dirs, "tracks": tracks, "truncated": truncated}


def list_under(subpath: str = "") -> list[str]:
    """All audio files under a subdirectory of the library (recursive), absolute."""
    base = (MUSIC_DIR / subpath).resolve()
    root = MUSIC_DIR.resolve()
    if root != base and root not in base.parents:
        raise ValueError("path escapes music library")
    return scan_library(base)


# --- playlist -----------------------------------------------------------
def read_playlist() -> list[str]:
    """Current pool entries (absolute paths), in order. Empty if no file yet."""
    if not PLAYLIST_PATH.exists():
        return []
    entries = []
    for line in PLAYLIST_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            entries.append(line)
    return entries


def write_playlist(paths: Iterable[str]) -> int:
    """Replace the pool with `paths` (order preserved, blanks dropped). Atomic."""
    cleaned = [p.strip() for p in paths if p and p.strip()]
    PLAYLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PLAYLIST_PATH.with_suffix(PLAYLIST_PATH.suffix + ".tmp")
    tmp.write_text("\n".join(cleaned) + ("\n" if cleaned else ""), encoding="utf-8")
    os.replace(tmp, PLAYLIST_PATH)  # atomic; mpv re-reads fresh each 23:00
    return len(cleaned)


def add_tracks(paths: Iterable[str]) -> int:
    """Append paths not already present. Returns count added."""
    current = read_playlist()
    have = set(current)
    added = [p for p in paths if p not in have and p.strip()]
    if added:
        write_playlist(current + added)
    return len(added)


def remove_tracks(paths: Iterable[str]) -> int:
    """Remove the given paths from the pool. Returns count removed."""
    remove = set(paths)
    current = read_playlist()
    kept = [p for p in current if p not in remove]
    removed = len(current) - len(kept)
    if removed:
        write_playlist(kept)
    return removed


def reorder(paths: list[str]) -> int:
    """Set the exact order/content of the pool to `paths`."""
    return write_playlist(paths)


# --- on-demand playback requests ---------------------------------------
def is_in_library(path: str) -> bool:
    """True if `path` is an existing audio file inside the music library.

    Guards the play-now endpoint: the path becomes an mpv argument on the host,
    so only real files under MUSIC_DIR are allowed."""
    try:
        p = Path(path).resolve()
        root = MUSIC_DIR.resolve()
    except OSError:
        return False
    if not (p == root or root in p.parents):
        return False
    return p.is_file() and _is_audio(p)


def request_play_now(path: str) -> None:
    """Ask the host to cold-start a one-off player for `path`.

    Written as a plain (non-atomic) write so systemd's inotify CLOSE_WRITE fires
    and music-playnow.path triggers. The path is host-valid because the library
    is bind-mounted at the same path inside the container."""
    PLAYNOW_REQUEST.parent.mkdir(parents=True, exist_ok=True)
    PLAYNOW_REQUEST.write_text(path + "\n", encoding="utf-8")


def request_shuffle(token: str = "go") -> None:
    """Ask the host to start the shuffle pool now (via music-shuffle.path).

    Content is ignored by the watcher; we write a short token so each request is
    a fresh modification event."""
    SHUFFLE_REQUEST.parent.mkdir(parents=True, exist_ok=True)
    SHUFFLE_REQUEST.write_text(token + "\n", encoding="utf-8")


if __name__ == "__main__":
    import json
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else "show"
    if cmd == "show":
        pl = read_playlist()
        print(f"{len(pl)} tracks in {PLAYLIST_PATH}")
        for p in pl[:10]:
            print(" ", p)
        if len(pl) > 10:
            print(f"  ... and {len(pl) - 10} more")
    elif cmd == "scan":
        print(len(scan_library()), "audio files under", MUSIC_DIR)
    elif cmd == "browse":
        print(json.dumps(browse(sys.argv[2] if len(sys.argv) > 2 else ""), indent=2))
    elif cmd == "search":
        r = search(sys.argv[2] if len(sys.argv) > 2 else "")
        print(f"dirs: {len(r['dirs'])}  tracks: {len(r['tracks'])}  truncated: {r['truncated']}")
        for d in r["dirs"][:5]:
            print("  📁", d["rel"])
        for t in r["tracks"][:5]:
            print("  🎵", t["name"])
    elif cmd == "add-dir":
        print("added", add_tracks(list_under(sys.argv[2])), "tracks")
    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        sys.exit(2)

"""nightvibe management app: live mpv control + shuffle-pool curation.

Thin FastAPI layer over mpv_ipc (control) and playlist (curation). Mirrors the
bptracker/fitness-tracker app shape. mpv only runs 23:00-08:00, so control
endpoints return {"playing": false}/{"ok": false} when it's down — never errors.
"""
import os
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import mpv_ipc
import playlist

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="nightvibe")
mpv = mpv_ipc.MpvIPC()


def _display_name(path: str) -> str:
    """Human label for a track: path relative to the library, else basename."""
    try:
        return str(Path(path).relative_to(playlist.MUSIC_DIR))
    except ValueError:
        return Path(path).name


# --- live control -------------------------------------------------------
@app.get("/api/status")
def status():
    return mpv.status()


@app.post("/api/skip")
def skip():
    return {"ok": mpv.skip()}


@app.post("/api/prev")
def prev():
    return {"ok": mpv.previous()}


@app.post("/api/pause")
def pause():
    return {"ok": mpv.toggle_pause()}


@app.post("/api/stop")
def stop():
    return {"ok": mpv.stop()}


class VolumeReq(BaseModel):
    volume: float


@app.post("/api/volume")
def volume(req: VolumeReq):
    return {"ok": mpv.set_volume(req.volume)}


class PlayReq(BaseModel):
    path: str


@app.post("/api/play-now")
def play_now(req: PlayReq):
    """Play one specific track immediately, any time of day.

    If a player is already live (the shuffle pool), we jump this track to the
    front and let the pool resume afterward. If nothing is playing, we ask the
    host to cold-start a one-off player (it connects the speaker first), which
    plays just this track and then frees the speaker."""
    if not playlist.is_in_library(req.path):
        raise HTTPException(status_code=400, detail="path is not a library track")
    if mpv.play_next(req.path):
        return {"ok": True, "mode": "live"}
    playlist.request_play_now(req.path)
    return {"ok": True, "mode": "starting"}


@app.post("/api/shuffle-now")
def shuffle_now():
    """Start the shuffle pool on demand. Routes through the host's music-night
    service (the single owner of shuffle), so the 23:00/08:00 timers are
    unaffected. No-op-safe if shuffle is already running."""
    tracks = playlist.read_playlist()
    if not tracks:
        raise HTTPException(status_code=400, detail="shuffle pool is empty")
    playlist.request_shuffle()
    return {"ok": True, "count": len(tracks)}


# --- curation -----------------------------------------------------------
@app.get("/api/playlist")
def get_playlist():
    tracks = playlist.read_playlist()
    return {
        "count": len(tracks),
        "tracks": [{"path": p, "name": _display_name(p)} for p in tracks],
    }


@app.get("/api/browse")
def browse(path: str = ""):
    try:
        return playlist.browse(path)
    except (ValueError, NotADirectoryError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/search")
def search(q: str = ""):
    return playlist.search(q)


class PathsReq(BaseModel):
    paths: list[str]


@app.post("/api/playlist/add")
def add(req: PathsReq):
    return {"added": playlist.add_tracks(req.paths), "count": len(playlist.read_playlist())}


@app.post("/api/playlist/remove")
def remove(req: PathsReq):
    return {"removed": playlist.remove_tracks(req.paths), "count": len(playlist.read_playlist())}


@app.post("/api/playlist/reorder")
def reorder(req: PathsReq):
    return {"count": playlist.reorder(req.paths)}


class DirReq(BaseModel):
    path: str = ""


@app.post("/api/playlist/add-dir")
def add_dir(req: DirReq):
    try:
        tracks = playlist.list_under(req.path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"added": playlist.add_tracks(tracks), "count": len(playlist.read_playlist())}


# --- static UI ----------------------------------------------------------
@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=os.environ.get("NV_HOST", "0.0.0.0"),
        port=int(os.environ.get("NV_PORT", "8323")),
    )

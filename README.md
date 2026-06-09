# nightvibe

Plays a shuffle of `~/Music` from a Bluetooth speaker every night **23:00–08:00**,
plus a small web app to curate the shuffle pool and control playback live.

See [PLAN.md](PLAN.md) for the full design and build log.

## Two halves

- **Host (native, systemd user units):** connects the speaker, verifies the A2DP
  sink, and runs mpv. Touches hardware, so it is *not* in Docker.
- **Docker (this compose app):** a FastAPI control panel — now-playing/skip/
  volume/stop over mpv's IPC socket, and shuffle-pool curation. Reachable from the
  phone at `http://<host>:8323`.

The two are decoupled by the mpv IPC socket (`run/mpv.sock`), bind-mounted into the
container. The app can go up/down/rebuild without ever interrupting playback.

## Install

First, point the host scripts at your Bluetooth speaker (this file is gitignored):

```sh
cp host/speaker.env.example host/speaker.env
# edit host/speaker.env and set SPEAKER_MAC to your speaker's MAC
# (find it with: bluetoothctl devices)
```

Host side (systemd user units + scripts):

```sh
host/install.sh        # symlinks scripts to /usr/local/bin, installs user units,
                       # enables the 23:00 start + 08:00 stop timers (needs sudo)
```

Docker side (already added to `docker_manage`'s PROJECTS):

```sh
docker compose up -d   # or: docker_manage up -d
```

## Requirements that bite

- The **speaker must be powered on** at 23:00 (it can be disconnected; a fully-off
  speaker can't be woken over Bluetooth).
- mpv runs only 23:00–08:00; the app shows "Not playing" the rest of the day.
- If a night ever starts silent with the BT transport stuck, `start-music`
  auto-recovers by restarting the PipeWire stack and relaunching EasyEffects with
  its preset. See PLAN.md → "Known risks / gotchas".

## Layout

```
app/        FastAPI control app (main.py, mpv_ipc.py, playlist.py, static/)
host/       native host pieces (start-music, bt-assert, units, install.sh, ...)
data/       playlist.m3u (the shuffle pool)
run/        mpv IPC socket lives here at runtime (bind-mounted into the container)
```

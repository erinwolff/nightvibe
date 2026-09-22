# Nighttime Music Automation — Plan

A system that plays a shuffle of my music library from a Bluetooth speaker every
night from **23:00 to 08:00**, plus a small management app to curate the shuffle
pool and control playback live.

This file is the working spec. Build it in the phase order below. Each phase has a
"Done when" so progress is checkable.

---

## Environment / hard constraints

- **OS:** Pop!_OS (Ubuntu-based). Audio stack is **PipeWire** (not bare PulseAudio).
- **Bluetooth:** BlueZ via D-Bus. Output is an A2DP sink.
- **Machine stays on overnight** — no suspend/inhibit handling needed.
- **Language:** Python for the app. Glue scripts stay as bash.
- **FOSS only.** Prefer tools already native to Pop!_OS.
- The management app runs in **Docker** and is managed by my existing
  `docker_manage` script (loops a `PROJECTS` array of compose dirs).

---

## Architecture

Two halves with a clear seam. **Do not blur them.**

### Host half — playback (native, NOT Docker)
Touches hardware and the schedule. Lives on the host because it needs PipeWire's
socket, the BlueZ/A2DP sink over D-Bus, and host systemd timers. Containerizing
this buys nothing and breaks everything.

- `music-night.timer` → fires 23:00, `Persistent=true`.
- `music-stop.timer` → fires 08:00, runs `systemctl stop music-night.service`.
  (Explicit stop timer over `RuntimeMaxSec` so start/stop times stay independent.)
- `music-night.service` → `ExecStart=start-music`, `Restart=on-failure`.
- `start-music` (bash): `bt-assert` → resolve BT sink node → `exec mpv ...`.
  Using `exec` makes the script *become* mpv, so the 08:00 stop signal hits mpv
  directly with no wrapper in between.
- `bt-assert` (the one genuinely failure-prone piece): connect the speaker over
  D-Bus with retry/backoff, then **verify the A2DP sink actually appears in
  PipeWire** before returning. Connecting is not the same as the sink existing.

mpv invocation (shuffle reshuffles for free each night because the service starts
fresh):

```
exec mpv --ao=pulse \
         --shuffle --loop-playlist=inf \
         --audio-device="pulse/$SINK" \
         --input-ipc-server=/run/music-night/mpv.sock \
         --volume=70 \
         /data/playlist.m3u
```

Explicit `--audio-device` beats setting the PipeWire default sink — it won't fight
anything else that changes the default, and it can only resolve *after* `bt-assert`
confirmed the sink exists, which enforces correct ordering.

> **Deviation (verified Phase 1):** mpv 0.34.1 (Pop!_OS) has **no native `pipewire`
> AO** — only `pulse` and `alsa`. Route through PipeWire's PulseAudio-compat layer:
> `--ao=pulse --audio-device="pulse/$SINK"` where `$SINK` is the bluez node name
> (e.g. `bluez_output.F4_4E_FC_95_3E_52.1`). NOT `pipewire/...`.

### Docker half — management app
Same shape as my other self-hosted services (navidrome/immich): a long-running
app with a UI. Controls mpv over the IPC socket, which is on the host — so the
container bind-mounts the socket and the playlist/library dirs.

Responsibilities:
1. **Curation:** build/edit the `.m3u` shuffle pool from the library.
2. **Live control:** JSON-RPC to mpv's IPC socket (now playing, skip, volume, stop).

### The seam
The host-side mpv IPC socket (`/run/music-night/mpv.sock`), bind-mounted into the
container. This decoupling is the whole point: the Docker app can go up / down /
rebuild via `docker_manage` without ever interrupting playback.

---

## Tech stack

- **Scheduler/supervisor:** systemd (host).
- **Player:** mpv, controlled via `--input-ipc-server` (raw JSON over the unix
  socket — likely no `python-mpv` dependency needed; the protocol is trivial).
- **Bluetooth:** BlueZ over D-Bus via `dbus-next` (Python) for `bt-assert`.
  Avoid scraping `bluetoothctl` output — it's a TUI, not a scripting interface.
- **Sink query/route:** `wpctl` / `pw-cli`, or `pw-dump` parsed as JSON.
- **App:** Python. Web UI so it's reachable from the phone (GrapheneOS, browser).
  Keep the frontend minimal — this is a control panel, not a product.
- **Container:** Docker Compose, added to `docker_manage`'s `PROJECTS` array.

---

## Proposed repo layout

```
music-night/
├── PLAN.md                  # this file
├── compose.yaml             # the Docker management app
├── Dockerfile
├── app/                     # Python management app
│   ├── main.py              # web server + routes
│   ├── mpv_ipc.py           # JSON-RPC client for the mpv socket
│   ├── playlist.py          # scan library, build/edit .m3u
│   └── static/              # minimal UI
├── host/                    # native host-side pieces (installed outside Docker)
│   ├── start-music          # bt-assert → resolve sink → exec mpv
│   ├── bt-assert            # connect + verify A2DP sink, with backoff
│   ├── resolve-bt-sink      # print the PipeWire node name for the BT device
│   ├── bt-disconnect        # optional, for ExecStopPost
│   ├── music-night.service
│   ├── music-night.timer
│   └── music-stop.timer
└── data/
    └── playlist.m3u         # the shuffle pool (bind-mounted into the container)
```

`host/` is version-controlled here but installed to the real system paths
(`/usr/local/bin/`, `~/.config/systemd/user/` or `/etc/systemd/system/`). An
`install.sh` that symlinks/copies them is worth adding.

---

## Build phases

### Phase 1 — Playback by hand
Prove the audio path works before automating anything.
- Pair the speaker manually (`bluetoothctl`).
- Find the PipeWire sink node name (`wpctl status` / `pw-dump`).
- Run mpv manually with `--audio-device` and a hand-made `.m3u`.
- **Done when:** mpv plays the shuffle out of the speaker with explicit device
  targeting (not relying on the default sink).

### Phase 2 — `bt-assert` + `resolve-bt-sink`
The failure-prone core. Build and hammer it.
- `bt-assert`: D-Bus connect with retry/backoff, then poll until the A2DP sink
  exists in PipeWire (with a timeout). Non-zero exit if it never appears.
- `resolve-bt-sink`: print the node name for the device.
- **Done when:** powering the speaker off, then running `bt-assert`, reliably
  connects and only returns once audio would actually route. Test the speaker-was-
  off and speaker-already-on cases.

### Phase 3 — systemd wiring
- `start-music` chains Phase 2 scripts then `exec mpv`.
- Service + both timers installed. Test with near-future `OnCalendar` values.
- **Done when:** a test timer starts playback and the stop timer kills it cleanly,
  and it survives a reboot (`Persistent=true` catches a missed start).

### Phase 4 — mpv IPC client
- `mpv_ipc.py`: connect to the socket, send JSON commands, read events.
- Handle **socket-absent as normal** (mpv only runs 23:00–08:00) — return a
  "not playing" state, never crash.
- **Done when:** can query now-playing, skip, set volume, and stop while mpv runs;
  and degrades gracefully when it isn't running.

### Phase 5 — Playlist curation
- `playlist.py`: scan `/music`, write `/data/playlist.m3u`. Add/remove/reorder.
- **Done when:** editing the pool through the app changes what next night shuffles.

### Phase 6 — Web UI + Docker
- Minimal web UI over Phases 4–5. Reachable from the phone.
- `compose.yaml` with the bind mounts; add dir to `docker_manage` PROJECTS.
- **Done when:** `docker_manage up -d` brings up the app, it controls playback at
  night and curates the pool any time, and `docker_manage down` doesn't touch mpv.

---

## Known risks / gotchas

- **bt-assert is the whole project's reliability.** "Connected" ≠ "sink exists" ≠
  "sink is targetable." Verify the sink, don't assume. Budget most debugging here.
- **Socket lifecycle:** `/run/music-night/mpv.sock` only exists 23:00–08:00. The
  container is up 24/7. App must treat a missing socket as "not playing," not error.
- **Socket ownership / uid:** the socket is owned by whoever runs the service.
  - User unit (`systemctl --user`): socket under `/run/user/1000/`, owned by my uid
    → set container `user: "1000:1000"`.
  - System unit: control path + ownership via `User=` + `RuntimeDirectory=`.
  - Misaligned uid → socket is bind-mounted but unwriteable from the container.
- **Sink name stability:** confirm the PipeWire node name is stable across
  reconnects; if not, match on a stable property (MAC/description) in
  `resolve-bt-sink` rather than a volatile index.
  - *Verified:* node name is `bluez_output.<MAC_with_underscores>.1`, MAC-derived
    and stable. `resolve-bt-sink` queries `pw-dump` and matches on
    `api.bluez5.address` (the MAC) to avoid guessing the trailing profile index.
- **Stuck A2DP transport (HIT during Phase 1):** rapid connect/disconnect +
  `wireplumber` restarts left the bluez sink present but **non-functional** —
  streams to it failed with `Stream error: Timeout`, the sink stayed `SUSPENDED`,
  and `MediaTransport1 State` stayed `idle` (never acquired). A `wireplumber`-only
  restart did **not** clear it. Fix that worked: restart the **full** user stack
  `systemctl --user restart pipewire pipewire-pulse wireplumber`, then reconnect.
  After that, `paplay`/mpv to the sink played correctly (transport → `active`).
  This was almost certainly induced by heavy debug cycling, not normal operation —
  but if a night ever starts silent with transport stuck `idle`, this is the cure.
  Health-check idea for `bt-assert` or `start-music`: after the sink appears,
  confirm a short test play actually drives `MediaTransport1 State` to `active`.

---

## Open decisions (resolve before/while building)

- **User systemd unit vs system unit** for `music-night.service`. User unit is
  simpler for socket/uid alignment with the container; needs `loginctl
  enable-linger` so it runs without me logged in. **Leaning user unit.**
- **Disconnect speaker at 08:00?** `ExecStopPost=bt-disconnect` — yes if the
  speaker behaves better fully disconnected, otherwise leave connected.
- **Volume:** fixed `--volume`, or fade-in at 23:00? Fade-in is a nice-to-have,
  not Phase 1.
- **Web framework:** FastAPI vs Flask. FastAPI if I want the async IPC handling to
  be clean; Flask if I want the smallest possible thing. Decide at Phase 6.
  - *Resolved:* FastAPI + uvicorn, to match the bptracker/fitness-tracker apps.

---

## Decisions made / build log

- **systemd unit type:** user unit (linger already enabled). Socket under
  `/run/user/1000/music-night` via `RuntimeDirectory=`, owned by uid 1000 →
  container runs `user: "1000:1000"`.
- **08:00 behavior:** disconnect the speaker (`ExecStopPost=bt-disconnect`).
  Note: while playing, the speaker being connected never blocks other PC audio —
  PipeWire sinks are independent and mpv targets the speaker explicitly. Disconnect
  is just to free the BT radio / let the speaker idle off.
- **Shuffle pool seed:** `Albums/` + `Singles/` from `~/Music` (curatable in-app).
- **bt-assert implementation:** bash + `dbus-send` (BlueZ Device1.Connect over
  D-Bus, no `bluetoothctl` TUI scraping, no host Python deps). `resolve-bt-sink`
  parses `pw-dump` JSON via `python3`. Chosen over Python+`dbus-next` for zero host
  dependencies and simplicity.
- **Added `music-stop.service`** (oneshot `systemctl --user stop music-night`),
  triggered by `music-stop.timer` — a timer needs a unit to activate.
- **Speaker must be powered ON at 23:00.** A fully powered-off OontZ cannot be
  woken over the air (`bt-assert` fails after retries, ~78s). Powered-on-but-
  disconnected (the real nightly case) connects first try in ~9.5s. `Restart=on-
  failure` keeps retrying so playback starts whenever the speaker comes online.
- **Codec:** plays correctly on plain **SBC** (stock WirePlumber config; no custom
  bluez codec config needed — an earlier SBC-XQ-disable experiment was reverted
  as it wasn't the cause).
- **Health-check + auto-recovery (in `start-music`):** after `bt-assert`, push a
  short SILENT wav (`host/silence.wav`) to the sink. If it fails (stuck transport),
  restart the full PipeWire stack, run `host/ensure-easyeffects` (relaunches
  EasyEffects in service mode), reconnect, and retry once. EasyEffects recovery is
  mandatory because a PipeWire restart drops it. See [[reference-easyeffects]].
  - *Verified 2026-09-21* by restarting the stack live: it does not just disconnect
    EasyEffects, it **kills** it — process dead, `easyeffects_sink`/`_source` gone,
    and `app-com.github.wwmm.easyeffects@autostart.service` left in `failed`. So the
    relaunch is genuinely load-bearing; without it the mic chain stays dead until
    the next login. `ensure-easyeffects` now restarts that unit (which also clears
    the failed state) rather than detaching its own `flatpak run`.
  - *Also verified:* it must **not** load a preset. The mic's autoload rule
    (`autoload/input/<samson>:Microphone.json` → `voicechat-tuned`) reapplies it on
    its own — relaunching with no `-l` came back on `voicechat-tuned`. The old
    explicit `-l` was redundant *and* unverifiable, since EasyEffects exits 0 for a
    preset that does not exist; that is how a wrong preset name (`voicechat`, vs the
    real `voicechat-tuned`) sat here for months while the script logged success.
  - Note this recovery path has never actually fired in production (0 occurrences
    Aug 19 2026 → now; every night connects on `attempt 1/6`), and its blast radius
    is every audio client on the box — it also silently drops `cava`'s audio.
- **Timer accuracy gotcha (testing only):** systemd timers default to
  `AccuracySec=1min` and coalesce firings within that window. For near-future test
  timers placed <1min apart, add `AccuracySec=1s` or they fire together. The real
  23:00/08:00 timers are an hour apart, so default accuracy is fine (start may land
  anywhere in 23:00–23:01).

### Phase status (2026-06-06)
- Phase 1 ✅  Phase 2 ✅  Phase 3 ✅  Phase 4 ✅  Phase 5 ✅  Phase 6 ✅
- Phase 4: `app/mpv_ipc.py` — status/skip/prev/pause/volume/stop, all verified;
  degrades to {"playing": false} when mpv is down.
- Phase 5: `app/playlist.py` — scan/browse/search/add/remove/reorder, hidden dirs
  pruned, path-traversal guarded. `search(query)` matches folder names + track
  relpaths library-wide (`GET /api/search?q=`), surfaced as a search box in the UI.
- Phase 6: FastAPI app (`app/main.py` + `static/index.html`), `compose.yaml`,
  `Dockerfile`. Container up on :8323, added to `docker_manage` PROJECTS.
  End-to-end seam verified: the container controls a host mpv (skip/volume/stop)
  through the bind-mounted `run/mpv.sock`.
- **Socket relocated** from `RuntimeDirectory` (`/run/user/1000/music-night`) to the
  repo's `run/` dir: systemd deletes a RuntimeDirectory on stop, so the Docker
  bind-mount source would vanish during the day / after reboot. `run/` always
  exists; container mounts `./run:/run/music-night`, runs as `user: "1000:1000"`.
- Remaining: confirm a real 23:00 start (or a manual `systemctl --user start
  music-night.service`) with the new socket path; reboot-survival still untested.

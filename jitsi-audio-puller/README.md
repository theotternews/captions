# jitsi-audio-puller

A minimalist headless bot that joins an open Jitsi meeting and streams the
mixed participant audio to a named pipe (FIFO) as raw PCM.

## Requirements

- Node.js 20 or 22 (64-bit Linux, macOS, or Windows)
- `mkfifo` (standard on Linux/macOS)
- The target Jitsi room must be open (no password, no lobby, no members-only)

## Install

```bash
npm install
```

`lib-jitsi-meet` is fetched from a GitHub release tarball; the install step
may take a moment while npm downloads ~2 MB.

## Usage

```
node index.js <jitsi-meeting-url> [options]
```

| Option | Description |
|---|---|
| `--per-speaker` | One FIFO per remote audio track (**default**; used by captions-relay) |
| `--pipe-dir <dir>` | Directory for per-track FIFOs (required with `--per-speaker`) |
| `--mixed [<pipe-path>]` | Sum all tracks into one FIFO (legacy mode) |

Positional: `jitsi-meeting-url` — full meeting URL (e.g. `https://meet.jit.si/MyRoom`).

With `--mixed`, an optional `pipe-path` names an existing FIFO; if omitted, a FIFO is created under `/tmp` and its path is printed.

### Examples

```bash
# Per-speaker FIFOs (captions-relay default)
mkdir -p /tmp/jitsi-speakers
node index.js https://meet.jit.si/MyRoom --per-speaker --pipe-dir /tmp/jitsi-speakers

# Legacy mixed mono stream
node index.js https://meet.jit.si/MyRoom --mixed

# Mixed stream into your own pipe
mkfifo /tmp/meeting.pcm
node index.js https://meet.jit.si/MyRoom --mixed /tmp/meeting.pcm
```

## Audio output format

| Property | Value |
|---|---|
| Encoding | Signed 16-bit PCM, little-endian |
| Sample rate | 48 000 Hz |
| Channels | 1 (mono, all participants mixed) |

### Consuming the audio stream

**Play with ffplay:**
```bash
ffplay -f s16le -ar 48000 -ac 1 /tmp/jitsi-audio-<timestamp>.pcm
```

**Pipe to ffmpeg for encoding:**
```bash
ffmpeg -f s16le -ar 48000 -ac 1 -i /tmp/jitsi-audio-<timestamp>.pcm \
       -c:a libopus output.ogg
```

**Live transcription with whisper.cpp:**
```bash
./stream -f s16le -ar 48000 -ac 1 < /tmp/jitsi-audio-<timestamp>.pcm
```

## Bot behaviour

- Joins the room with the display name **`captions-bot`**
- Receives audio from all participants; sends nothing
- Requests zero video streams to avoid unnecessary bandwidth
- Logs unmuted participants to stdout whenever the mute state changes
- Disconnects and exits when:
  - `Ctrl-C` / `SIGINT` / `SIGTERM` is received
  - The last non-bot participant leaves the room

## How it works

```
lib-jitsi-meet (XMPP/Jingle signalling + WebRTC)
    uses @roamhq/wrtc for RTCPeerConnection in Node.js
    ↓
RTCAudioSink  ← one per remote audio track
    ↓ ondata (Int16 PCM, 48 kHz, ~480 samples / 10 ms)
10 ms mix timer  ← sums all active tracks, clamps to Int16
    ↓
Named FIFO  → consumer (ffplay / ffmpeg / whisper / …)
```

Browser APIs required by `lib-jitsi-meet` (`XMLHttpRequest`, `Event`,
`navigator`, etc.) are polyfilled via `jsdom` before the library is loaded.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `Error: ENOENT: mkfifo` | `mkfifo` not in `PATH` (unusual on Linux) |
| Silence on the pipe | No reader attached yet — connect a consumer first |
| `CONNECTION_FAILED` | Wrong server domain, WebSocket not available — try adding `bosh:` option |
| `CONFERENCE_FAILED` with `not-authorized` | Room has a password or lobby enabled |

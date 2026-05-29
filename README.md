# Live meeting captions

Share **live captions** with people in your meeting on a phone or second screen—alongside Zoom, Teams, Jitsi, or anything else. Speech is turned into text on **your** computer; viewers only get the caption stream, not your audio.

You need an [Ably](https://ably.com) account (free tier works). The person running captions keeps the main API key private; everyone else gets a short-lived link and token.

---

## Before your first meeting

1. **Set up the facilitator laptop** (see [For developers](#for-developers) — install once).
2. **Create an Ably API key** in the Ably dashboard and save it on the facilitator machine only:
   ```bash
   export CAPTIONS_ABLY_API_KEY='your_app_id.key_id:secret'
   ```
3. **Install speech-to-text** if you will caption from this machine: [whisper.cpp](https://github.com/ggerganov/whisper.cpp) built on the laptop, plus the path in `WHISPER_CPP_HOME`.
4. **For Jitsi rooms only:** install [Node.js](https://nodejs.org/) 20+ and run `npm install` once inside the `jitsi-audio-puller` folder in this project.

---

## Run a caption session

On the facilitator laptop:

```bash
uv run captions session new
```

You will get:

- A **subscriber link** — send this to Deaf participants, caption readers, or anyone who needs the text on another device.
- A **subscriber token** — they paste this on the subscriber page (or it may be in the link flow).
- A **publisher token** — **keep secret**; only used on the machine that generates captions.
- A **channel name** — must match on the subscriber link; do not change spelling or capitalization.

Tokens last about **four hours** by default. Start a new session for a new meeting if you want a clean slate.

**Start captioning in the same step** (Linux, audio from this computer):

```bash
uv run captions session new --pulse -v
```

**Start captioning from an open Jitsi room** (no password or waiting room):

```bash
uv run captions session new --jitsi 'https://meet.jit.si/YourRoomName' -v
```

The bot joins as **captions-bot**. The room must allow guests in without a lobby.

---

## Subscribers (phones and second screens)

1. Open the **subscriber link** from `session new` (hosted online by default, or a link your facilitator gives you).
2. Confirm the **channel** in the URL matches what the facilitator printed.
3. Paste the **subscriber token** and connect.
4. Captions should appear as the facilitator’s machine publishes them.

If you tested locally, the facilitator may have given you a `http://localhost:…` or LAN address instead—use that on the same Wi‑Fi.

**Stuck on an old session?** Use **Clear saved token** on the subscriber page, then open the new link and token.

---

## Facilitator: where captions come from

| Source | When to use |
|--------|-------------|
| **Pulse / system audio** (`--pulse`) | Linux laptop capturing meeting audio (headphones loopback, virtual cable, etc.). |
| **Jitsi** (`--jitsi <url>`) | Meeting is on Jitsi; this app joins the room and listens. |
| **Manual test** | Publisher web page to type or paste lines (good for rehearsal). |

For Jitsi, install Node dependencies once:

```bash
cd jitsi-audio-puller && npm install && cd ..
```

To resume the same Jitsi room without minting new tokens, add `--reconnect` to `session new --jitsi …` (only if you already ran a session for that room on this machine).

---

## End a session

```bash
uv run captions session delete <channel>
```

Subscribers see that the session ended. Old tokens still expire on their own schedule.

**New tokens** for the same channel (e.g. someone lost their link):

```bash
uv run captions tokens subscriber <channel>
uv run captions tokens publisher <channel>
```

---

## Rehearsal checklist (two phones)

1. Facilitator: `export CAPTIONS_ABLY_API_KEY=…` and `uv run captions session new`.
2. Phone A: open subscriber link, paste **subscriber** token, wait until it says it is listening.
3. Facilitator: open the publisher test page (local or hosted), paste **publisher** token, send a test line.
4. Phone A should update. If not, see [Troubleshooting](#troubleshooting).

---

## Troubleshooting

**Both sides say “connected” but no text**

- **Channel mismatch** — The name in the subscriber URL must match the facilitator’s channel exactly (including capitals).
- **Wrong token** — Publisher page needs the **publisher** token; subscribers need the **subscriber** token. They are not interchangeable.
- **Old token cached** — Subscriber: **Clear saved token**, then reconnect with the new link.

**“Channel denied access” or publish errors**

- Often a **subscriber** token was used on the publisher side, or the Ably key is too restricted. Use a normal root API key on the facilitator machine until you have a dedicated setup.

**Jitsi: no audio or bot cannot join**

- Room must be **open** (no password, no lobby, not members-only).
- Run `npm install` in `jitsi-audio-puller` if you have not already.

**Whisper / no captions from audio**

- Confirm `WHISPER_CPP_HOME` points at a built whisper.cpp tree and a model file exists.
- On Linux pulse mode, use `-v` to see whether transcription is running on the facilitator machine.

---

## For developers

### Install and environment

```bash
cd /path/to/captions
uv sync
export CAPTIONS_ABLY_API_KEY='your_app_id.key_id:secret'
```

Optional environment variables:

| Variable | Purpose |
|----------|---------|
| `CAPTIONS_SUBSCRIBER_PAGES_BASE` | Base URL for subscriber links (default: project GitHub Pages `docs/`). |
| `CAPTIONS_TOKEN_TTL` | Token lifetime in seconds (default `14400`). |
| `WHISPER_CPP_HOME` | whisper.cpp root; binary `build/bin/whisper-stream-pcm`, model under `models/`. |
| `CAPTIONS_WHISPER_STREAM_PCM` / `CAPTIONS_WHISPER_MODEL` | Override whisper binary or model path. |
| `CAPTIONS_JITSI_PULLER_SCRIPT` | Path to `jitsi-audio-puller/index.js` (default: `<repo>/jitsi-audio-puller/index.js`). |
| `CAPTIONS_NODE_BIN` | Node executable for Jitsi capture (default `node`). |

### CLI reference

```bash
uv run captions session new [--json] [--channel NAME] [--ttl SECONDS]
uv run captions session new --pulse [-v]          # Linux: PulseAudio → whisper → Ably
uv run captions session new --jitsi URL [-v] [--mixed] [--reconnect] [--max-speakers N]
uv run captions session list [--prefix captions:] [--json] [--all-pages]
uv run captions session delete <channel> [--dry-run]
uv run captions tokens publisher <channel>
uv run captions tokens subscriber <channel>
uv run captions whisper pulse --channel CH [--publisher-token T] [-v] [--dry-run] ...
uv run captions whisper jitsi --channel CH --jitsi-url URL [-v] [--mixed] ...
```

`session new --json` prints channel, `subscriber_url`, and tokens for scripting. `--pulse`, `--jitsi`, and `--json` are mutually exclusive where noted in `--help`.

Jitsi channel default: `captions:<room>` derived from the meeting URL. Explicit `--channel` overrides.

### Serve the web UI locally

Ably allows `http://localhost`. From `web/`:

```bash
python -m http.server 8765
```

- Subscriber: `http://localhost:8765/subscriber/index.html?channel=YOUR_CHANNEL`
- Publisher (test): `http://localhost:8765/publisher/index.html`

Use the machine’s LAN IP for phones on the same network.

### Browser publisher (`glue.js`)

Load after your whisper bundle:

```html
<script src="https://cdn.ably.com/lib/ably.min-2.js"></script>
<script src="http://localhost:8765/publisher/glue.js"></script>
```

```javascript
await connectCaptionsPublisher({
  channel: "<channel from session new>",
  token: "<publisher_token>",
});

publishCaption({
  text: transcriptString,
  kind: isFinal ? "final" : "partial",
});
```

`CaptionsAblyPublisher` supports `debounceMs` / `minIntervalMs`. Raw whisper terminal output (ANSI, `\r` rewrites) is normalized on subscribers the same way as the Python relay.

### whisper pulse / jitsi (implementation notes)

- **pulse:** `ffmpeg` → `whisper-stream-pcm`; stdout on a pseudo-TTY for `\r`/ANSI behavior; `--line-kind auto|partial|final`; `--debounce-ms` / `--min-interval-ms` on partials.
- **jitsi:** `node jitsi-audio-puller/index.js`; default **per-speaker** FIFOs; `--mixed` for single summed stream. See [jitsi-audio-puller/README.md](jitsi-audio-puller/README.md) for FIFO layout and standalone `node` usage.

### Project layout

- `src/captions_relay/` — CLI, Ably tokens, pulse/Jitsi pipelines
- `web/` — static subscriber and publisher pages
- `docs/` — GitHub Pages subscriber host
- `jitsi-audio-puller/` — headless Jitsi audio bot (Node)

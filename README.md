# captions-relay

Python CLI plus static web pages that relay **live caption text** to a small audience over **[Ably](https://ably.com)** pub/sub—a parallel “companion captions” lane alongside **any video meeting**. Whisper / STT stays on your machine; subscribers only receive **short JSON caption events**.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) and Python **3.13+**
- An Ably account and **root API key** (Dashboard → API keys)

## Install (dev)

```bash
cd /path/to/captions-relay
uv sync
export CAPTIONS_ABLY_API_KEY='your_app_id.key_id:secret'
```

Keep the root key **only** on the facilitator’s machine (or a future token broker). Browsers get **short-lived tokens** from the CLI.

Optional: `CAPTIONS_SUBSCRIBER_PAGES_BASE` — root URL printed for subscribers (default: hosted GitHub Pages site from this project’s `docs/`). Set this if your fork uses a different static host.

## Create a session

```bash
uv run captions session new
```

Or JSON for scripting:

```bash
uv run captions session new --json
```

This prints:

- **`channel`** — Ably channel name (default new sessions use `captions:<random uuid hex>`)
- **Subscriber URL** — ready-to-open link to the static subscriber page (GitHub Pages by default; override with `CAPTIONS_SUBSCRIBER_PAGES_BASE`)
- With **`--json`**: same link as **`subscriber_url`**
- **`publisher_token`** — **secret**; used in the tab that runs captioning / whisper
- **`subscriber_token`** — share with Deaf / processing-access participants
- Default TTL **4 hours** (override with `--ttl` or env `CAPTIONS_TOKEN_TTL`)

Refresh tokens only:

```bash
uv run captions tokens publisher <channel>
uv run captions tokens subscriber <channel>
```

End a session (subscribers see **Session ended by host** on the static subscriber page):

```bash
uv run captions session delete <channel>
```

Uses your **root** API key to publish a final `caption` message with `ended: true`. Ably does not erase the channel; tokens already minted still expire on schedule. Use **`--dry-run`** to inspect the payload without publishing.

List channels that Ably currently considers **active** (recently used):

```bash
uv run captions session list
uv run captions session list --prefix 'captions:'
uv run captions session list --json
```

Needs **channel-metadata** on `*` for your API key (root keys usually do). This is rate-limited; one page is returned by default — use **`--all-pages`** to follow Ably’s `next` links (still capped).

## Serve the static web UI

Subscribers can use the **subscriber URL** from `session new` (GitHub Pages by default).

For local development, Ably accepts `http://localhost`. Serve `web/` with a static server (not `file://`):

```bash
cd web
python -m http.server 8765
```

- **Subscribers (phones / second screen):**  
  `http://localhost:8765/subscriber/index.html?channel=YOUR_CHANNEL`  
  Use the exact **`channel`** string from `session new` in the query (URL-encode if needed). Paste the **subscriber** token, tap **Save & connect**.

- **Facilitator test publisher (no whisper):**  
  `http://localhost:8765/publisher/index.html`  
  Paste **channel** + **publisher** token, **Connect**, then use partial/final buttons.

For phones on the same LAN, use your machine’s LAN IP instead of `localhost`.

## Wire whisper.cpp (browser)

1. Load Ably and the helper after your whisper bundle:

```html
<script src="https://cdn.ably.com/lib/ably.min-2.js"></script>
<script src="http://localhost:8765/publisher/glue.js"></script>
```

2. Once per session (after you have `channel` + `publisher_token` from the CLI):

```javascript
await connectCaptionsPublisher({
  channel: "<channel from session new>",
  token: "<publisher_token>",
});
```

3. On each whisper update (interim or final):

```javascript
publishCaption({
  text: transcriptString,
  kind: isFinal ? "final" : "partial",
});
```

You can pass **raw whisper-style stdout** in `text` (ANSI CSI/OSC, carriage-return rewrites). Subscribers strip escapes and apply the same “last `\r` segment + duplicate-prefix collapse” rules as the Python helper `normalize_whisper_stdout_line`.

Or use the class directly for more control:

```javascript
const pub = new CaptionsAblyPublisher({ debounceMs: 450, minIntervalMs: 450 });
await pub.connect({ channel, token });
pub.publishCaption({ text: "…", kind: "partial" });
```

Coalescing keeps partial traffic roughly under **~2 messages/second**; finals flush immediately.

## Publish from whisper.cpp (PulseAudio, CLI)

Linux only: capture from PulseAudio, run **`whisper-stream-pcm`**, and publish transcript lines to Ably.

```bash
export CAPTIONS_PUBLISHER_TOKEN='<publisher_token from session new>'
export WHISPER_CPP_HOME=/path/to/whisper.cpp   # defaults: build/bin/whisper-stream-pcm, models/ggml-base.bin

uv run captions whisper pulse --channel '<channel from session new>' -v
```

Use **`--dry-run`** to print the resolved `ffmpeg | whisper-stream-pcm` shell command without Ably or audio. Paths can be overridden with `--whisper-binary`, `-m` / `--model`, or env **`CAPTIONS_WHISPER_STREAM_PCM`** / **`CAPTIONS_WHISPER_MODEL`**.

**Pulse publishes raw stdout lines** (TTY escapes preserved). Normalization happens in the subscriber page so on-screen edits match what a terminal would show.

## Two-phone rehearsal checklist

1. Export `CAPTIONS_ABLY_API_KEY` on the facilitator laptop.
2. Run `uv run captions session new` — copy **`subscriber_token`** and note **`channel`**.
3. Start `python -m http.server 8765` from `web/`.
4. On phone A (subscriber): open subscriber URL **with `?channel=`** matching the CLI output exactly; paste **subscriber** token; status should reach **Listening · channel attached** before testing.
5. On laptop: open publisher test page → connect with **publisher** token → send partial/final; confirm phone A updates.
6. End session by closing tabs; optionally rotate session (`session new`) for the next meeting so old tokens expire naturally.

## Troubleshooting (both show “connected” but no captions move)

1. **Channel mismatch** — The publisher tab’s channel string must exactly match the subscriber URL `?channel=` value (same spelling and case; Ably channel names are case-sensitive).

2. **Swapped tokens** — Publisher UI needs **`publisher_token`**. Subscriber paste needs **`subscriber_token`**.

3. **Stale subscriber storage** — After changing sessions, subscriber may cache an old channel/token; tap **Clear saved token** and reopen with the new `?channel=`.

4. **Empty publisher draft** — The dev publisher requires **non-empty** text before **Send partial** / **Send final**.

5. **DevTools console** — Publisher logs **`Ably publish failed`** on rejections; subscriber logs **`[captions-relay subscriber]`** for subscribe/attach errors.

6. **`Channel denied access…` / Ably code 40160** — Tokens are capped to the channel you minted for. Typical causes: the **subscriber** token was pasted into the publisher (no **publish** on that channel); or **`CAPTIONS_ABLY_API_KEY`** is a **restricted** key — minted caps are intersected with the key’s dashboard scope, which can forbid publish entirely. Prefer a default full-access/root key until you carve a dedicated token-minting key with **publish + subscribe** on the channels or patterns you use (e.g. `captions:*`).

# jitsi-captions

Python CLI plus static web pages that relay **live caption text** to a small audience over **[Ably](https://ably.com)** pub/sub — a parallel “companion captions” lane while video runs in **Jitsi Meet** (or any call). Whisper / STT stays on your machine; subscribers only receive **short JSON caption events**.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) and Python **3.13+**
- An Ably account and **root API key** (Dashboard → API keys)

## Install (dev)

```bash
cd /path/to/jitsi-captions
uv sync
export JITSI_CAPTIONS_ABLY_API_KEY='your_app_id.key_id:secret'
```

Keep the root key **only** on the facilitator’s machine (or a future token broker). Browsers get **short-lived tokens** from the CLI.

## Create a session

```bash
uv run jitsi-captions session new
```

Or JSON for scripting:

```bash
uv run jitsi-captions session new --json
```

This prints:

- `session_id` and `channel` (`captions:<hex>`)
- **`publisher_token`** — **secret**; used in the tab that runs captioning / whisper
- **`subscriber_token`** — share with Deaf / processing-access participants
- Default TTL **4 hours** (override with `--ttl` or env `JITSI_CAPTIONS_TOKEN_TTL`)

Refresh tokens only:

```bash
uv run jitsi-captions tokens publisher <session_id>
uv run jitsi-captions tokens subscriber <session_id>
```

## Serve the static web UI

Ably uses WebSockets from `https` or `http://localhost`. Open the pages via a **local static server** (not `file://`):

```bash
cd web
python -m http.server 8765
```

- **Subscribers (phones / second screen):**  
  `http://localhost:8765/subscriber/index.html?channel=captions:YOUR_SESSION_HEX`  
  Paste the **subscriber** token, tap **Save & connect**.

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
await connectJitsiCaptionsPublisher({
  channel: "captions:…………",
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

Or use the class directly for more control:

```javascript
const pub = new JitsiCaptionsAblyPublisher({ debounceMs: 450, minIntervalMs: 450 });
await pub.connect({ channel, token });
pub.publishCaption({ text: "…", kind: "partial" });
```

Coalescing keeps partial traffic roughly under **~2 messages/second**; finals flush immediately.

## Two-phone rehearsal checklist

1. Export `JITSI_CAPTIONS_ABLY_API_KEY` on the facilitator laptop.
2. Run `uv run jitsi-captions session new` — copy **`subscriber_token`** and note **`channel`**.
3. Start `python -m http.server 8765` from `web/`.
4. On phone A (subscriber): open subscriber URL **with `?channel=`** matching the CLI output exactly; paste **subscriber** token; status should reach **Listening · channel attached** before testing.
5. On laptop: open publisher test page → connect with **publisher** token → send partial/final; confirm phone A updates.
6. End session by closing tabs; optionally rotate session (`session new`) for the next meeting so old tokens expire naturally.

## Troubleshooting (both show “connected” but no captions move)

1. **Channel mismatch** — The publisher tab’s channel string must match the subscriber URL `?channel=` for the same session (`captions:` plus the same lowercase session hex — hyphens are ignored; `captions:591d0445…` equals `captions:591d0445-c949-…` after normalization).

2. **Swapped tokens** — Publisher UI needs **`publisher_token`**. Subscriber paste needs **`subscriber_token`**.

3. **Stale subscriber storage** — After changing sessions, subscriber may cache an old channel/token; tap **Clear saved token** and reopen with the new `?channel=`.

4. **Empty publisher draft** — The dev publisher requires **non-empty** text before **Send partial** / **Send final**.

5. **DevTools console** — Publisher logs **`Ably publish failed`** on rejections; subscriber logs **`[jitsi-captions subscriber]`** for subscribe/attach errors.

6. **`Channel denied access…` / Ably code 40160** — Tokens are capped to your session channel only. Typical causes: the **subscriber** token was pasted into the publisher (no **publish** on that channel); or **`JITSI_CAPTIONS_ABLY_API_KEY`** is a **restricted** key — minted caps are intersected with the key’s dashboard scope, which can forbid publish entirely. Prefer a default full-access/root key until you carve a dedicated token-minting key with **publish + subscribe** on **`captions:*`** (or the exact channel prefix you use).

7. **`channelId = cap` in an error message** — That usually means the Realtime library is attaching to **`captions:cap`**, almost always because the publisher **channel field was shortened or pasted wrong** (`cap` parses as hexadecimal). Paste the whole **`captions:` + session hex** from `session new` (32 lowercase hex characters after you strip hyphens).

/**
 * Ably publisher helper for browser-side caption apps (e.g. whisper.cpp in WebAssembly).
 *
 * Usage after `connect()`:
 *   const pub = new CaptionsAblyPublisher();
 *   await pub.connect({ channel: '<Ably channel name>', token: '<publisher token>' });
 *   pub.publishCaption({ text: 'hello', kind: 'partial' });
 *
 * From whisper: call publishCaption on each model update; coalescing runs internally.
 * You may send raw whisper/terminal-style chunks (CSI/OSC escapes, CR rewrites); the
 * subscriber normalizes them the same way as the CLI pipeline’s TTY handling.
 */

/** Ably channel naming (matches ``captions_relay.config``). */
const MAX_CHANNEL_LEN = 2048;

function validateAblyChannelName(name) {
  const channel = String(name ?? "").trim();
  if (!channel) {
    throw new Error("Channel name must be non-empty.");
  }
  if (channel.includes("\n") || channel.includes("\r")) {
    throw new Error("Channel name must not contain newline characters.");
  }
  if (channel.startsWith("[") || channel.startsWith(":")) {
    throw new Error("Channel name must not start with '[' or ':'.");
  }
  if (channel.length > MAX_CHANNEL_LEN) {
    throw new Error(`Channel name must be at most ${MAX_CHANNEL_LEN} characters.`);
  }
  const ns = channel.split(":", 1)[0];
  if (ns.includes("*")) {
    throw new Error("Channel namespace (before the first ':') must not contain '*'.");
  }
  return channel;
}

function extractChannelFromInput(raw) {
  let s = String(raw ?? "").trim();
  if (!s) {
    return s;
  }
  const lower = s.toLowerCase();
  if (lower.includes("channel=") || lower.includes("channel%3d")) {
    try {
      if (lower.startsWith("http://") || lower.startsWith("https://")) {
        const u = new URL(s);
        const c = u.searchParams.get("channel");
        if (c && c.trim()) {
          return c.trim();
        }
      }
    } catch (_) {
      /* ignore */
    }
    const m = s.match(/[#?&]channel=([^&#]+)/i);
    if (m) {
      return decodeURIComponent(m[1]).trim();
    }
  }
  return s;
}

function normalizeCaptionChannel(raw) {
  const s = extractChannelFromInput(raw);
  if (!s) {
    throw new Error("Channel name must be non-empty.");
  }
  return validateAblyChannelName(s);
}

class CaptionsAblyPublisher {
  static CAPTION_EVENT = "caption";

  /**
   * @param {{ debounceMs?: number, minIntervalMs?: number }} [options]
   */
  constructor(options = {}) {
    this._debounceMs = options.debounceMs ?? 450;
    this._minIntervalMs = options.minIntervalMs ?? 450;
    this._ably = null;
    this._channel = null;
    this._timer = null;
    this._pending = null;
    this._lastFlush = 0;
    this._queued = null;
  }

  /** Exposed for test harness wiring only. */
  get realtime() {
    return this._ably;
  }

  /**
   * Wait for Realtime connectivity only — do **not** call `channel.attach()` here.
   * An eager attach validates capabilities early and rejects with obscure errors while
   * the first publish would perform attach anyway. Capability mismatches otherwise show
   * up on first publish (with clearer logs).
   * @param {{ channel: string, token: string }} params
   * @returns {Promise<void>}
   */
  connect({ channel, token }) {
    const ch = normalizeCaptionChannel(channel);
    if (!token?.trim()) {
      return Promise.reject(new Error("token is required"));
    }

    this.close();
    this._ably = new Ably.Realtime({ token: token.trim() });
    this._channel = this._ably.channels.get(ch);

    return new Promise((resolve, reject) => {
      const fail = (err) => reject(err.reason || err);

      const done = () => resolve();

      this._ably.connection.once("failed", fail);

      if (this._ably.connection.state === "connected") {
        done();
      } else {
        this._ably.connection.once("connected", done);
      }
    });
  }

  /**
   * @param {{ text: string, kind?: 'partial'|'final', speaker?: { id?: string, name?: string } }} payload
   */
  publishCaption(payload) {
    if (!this._channel) {
      throw new Error("connect() first");
    }
    const text = (payload.text ?? "").trim();
    const kind = payload.kind === "final" ? "final" : "partial";
    const body = {
      t: new Date().toISOString(),
      text,
      kind,
    };
    if (payload.speaker && typeof payload.speaker === "object") {
      const id = String(payload.speaker.id ?? "").trim();
      const name = String(payload.speaker.name ?? "").trim();
      if (id || name) {
        const label = name || id;
        body.speaker = { id: id || name, name: label };
        if (label && text) {
          const prefix = label + ": ";
          body.text = text.startsWith(prefix) ? text : prefix + text;
        }
      }
    }

    if (kind === "final") {
      if (this._timer) {
        clearTimeout(this._timer);
        this._timer = null;
      }
      if (this._queued) {
        clearTimeout(this._queued);
        this._queued = null;
      }
      this._pending = null;
      this._sendWithSpacing(body);
      return;
    }

    this._pending = body;
    if (this._timer) {
      clearTimeout(this._timer);
    }
    this._timer = setTimeout(() => {
      this._timer = null;
      const latest = this._pending;
      this._pending = null;
      if (latest && latest.kind === "partial") {
        this._sendWithSpacing(latest);
      }
    }, this._debounceMs);
  }

  _sendWithSpacing(body) {
    if (!body.text) {
      return;
    }
    const now = Date.now();
    const elapsed = now - this._lastFlush;
    const delay = Math.max(0, this._minIntervalMs - elapsed);

    const send = async () => {
      if (!this._channel) {
        return;
      }
      this._lastFlush = Date.now();
      try {
        await this._channel.publish(CaptionsAblyPublisher.CAPTION_EVENT, body);
      } catch (err) {
        console.error("[captions-relay] Ably publish failed:", err);
      }
    };

    if (delay > 0 && body.kind !== "final") {
      if (this._queued) {
        clearTimeout(this._queued);
      }
      this._queued = setTimeout(() => {
        this._queued = null;
        void send();
      }, delay);
    } else {
      void send();
    }
  }

  get connectionState() {
    return this._ably?.connection.state ?? "closed";
  }

  /** Attached + ready to publish (helps debug UX). */
  get channelState() {
    return this._channel?.state ?? "closed";
  }

  close() {
    if (this._timer) {
      clearTimeout(this._timer);
      this._timer = null;
    }
    if (this._queued) {
      clearTimeout(this._queued);
      this._queued = null;
    }
    this._pending = null;
    if (this._ably) {
      this._ably.close();
      this._ably = null;
      this._channel = null;
    }
  }
}

globalThis.CaptionsAblyPublisher = CaptionsAblyPublisher;
globalThis.normalizeCaptionChannel = normalizeCaptionChannel;

/** @deprecated Use the class — kept for quick paste integration */
function publishCaption(payload) {
  if (!globalThis.__captionsPublisher) {
    throw new Error("Call connectCaptionsPublisher first");
  }
  globalThis.__captionsPublisher.publishCaption(payload);
}

/**
 * One-liner setup for whisper hooks: stores singleton used by publishCaption().
 * @param {{ channel: string, token: string }} params
 */
async function connectCaptionsPublisher(params) {
  const p = new CaptionsAblyPublisher();
  await p.connect(params);
  globalThis.__captionsPublisher = p;
  return p;
}

globalThis.connectCaptionsPublisher = connectCaptionsPublisher;
globalThis.publishCaption = publishCaption;

/**
 * Ably publisher helper for browser-side caption apps (e.g. whisper.cpp in WebAssembly).
 *
 * Usage after `connect()`:
 *   const pub = new JitsiCaptionsAblyPublisher();
 *   await pub.connect({ channel: 'captions:…', token: '<publisher token>' });
 *   pub.publishCaption({ text: 'hello', kind: 'partial' });
 *
 * From whisper: call publishCaption on each model update; coalescing runs internally.
 */

/** Match tokens minted by the CLI (`captions:` + lowercase hex; dashed UUID collapses here). */
function normalizeCaptionChannel(raw) {
  let s = String(raw ?? "").trim();
  const idx = s.indexOf("captions:");
  if (idx > 0) {
    s = s.slice(idx);
  }
  const m = /^captions:([a-fA-F0-9-]+)$/.exec(s);
  if (!m) {
    throw new Error(
      "Channel must be exactly captions:<session_hex> from `jitsi-captions session new` (hex only after the colon). " +
        "Example: captions:591d0445c9494c0dbf4891b54278ca92 — no spaces, prefixes, or query junk."
    );
  }
  const slug = m[1].replace(/-/g, "").toLowerCase();
  if (!/^[0-9a-f]+$/.test(slug)) {
    throw new Error(
      "Channel slug after captions: must be hexadecimal (optional hyphens allowed for UUID)."
    );
  }
  if (slug === "cap") {
    throw new Error(
      'Channel "captions:cap" is not a session id — paste the full channel from `jitsi-captions session new` (captions: plus 32 hex digits).'
    );
  }
  return `captions:${slug}`;
}

class JitsiCaptionsAblyPublisher {
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
   * @param {{ text: string, kind?: 'partial'|'final' }} payload
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
        await this._channel.publish(JitsiCaptionsAblyPublisher.CAPTION_EVENT, body);
      } catch (err) {
        console.error("[jitsi-captions] Ably publish failed:", err);
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

globalThis.JitsiCaptionsAblyPublisher = JitsiCaptionsAblyPublisher;
globalThis.normalizeCaptionChannel = normalizeCaptionChannel;

/** @deprecated Use the class — kept for quick paste integration */
function publishCaption(payload) {
  if (!globalThis.__jitsiCaptionsPublisher) {
    throw new Error("Call connectJitsiCaptionsPublisher first");
  }
  globalThis.__jitsiCaptionsPublisher.publishCaption(payload);
}

/**
 * One-liner setup for whisper hooks: stores singleton used by publishCaption().
 * @param {{ channel: string, token: string }} params
 */
async function connectJitsiCaptionsPublisher(params) {
  const p = new JitsiCaptionsAblyPublisher();
  await p.connect(params);
  globalThis.__jitsiCaptionsPublisher = p;
  return p;
}

globalThis.connectJitsiCaptionsPublisher = connectJitsiCaptionsPublisher;
globalThis.publishCaption = publishCaption;

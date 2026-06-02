'use strict';

// ─── Polyfills ────────────────────────────────────────────────────────────────
// All polyfills must be installed before lib-jitsi-meet is required, because
// that module accesses browser globals at parse/require time.

const wrtc = require('@roamhq/wrtc');

const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!DOCTYPE html><html><body></body></html>', {
    url: 'https://localhost/',
    pretendToBeVisual: false,
});
const jsdomWindow = dom.window;

const WRTC_GLOBALS = [
    'MediaStream',
    'MediaStreamTrack',
    'RTCDataChannel',
    'RTCDataChannelEvent',
    'RTCDtlsTransport',
    'RTCIceCandidate',
    'RTCIceTransport',
    'RTCPeerConnection',
    'RTCPeerConnectionIceEvent',
    'RTCPeerConnectionIceErrorEvent',
    'RTCRtpReceiver',
    'RTCRtpSender',
    'RTCSctpTransport',
    'RTCSessionDescription',
];
for (const key of WRTC_GLOBALS) {
    if (wrtc[key] !== undefined) {
        jsdomWindow[key] = wrtc[key];
        global[key] = wrtc[key];
    }
}

function RTCRtpTransceiver() {}
jsdomWindow.RTCRtpTransceiver = RTCRtpTransceiver;
global.RTCRtpTransceiver = RTCRtpTransceiver;

function sdpRejectVideo(sdp) {
    if (!sdp || !sdp.includes('m=video')) return sdp;

    const CRLF = sdp.includes('\r\n') ? '\r\n' : '\n';
    const videoMids = new Set();

    const patched = sdp
        .split(/(?=\r?\nm=)/)
        .map(section => {
            if (!/\nm=video/i.test(section)) return section;

            const midMatch = section.match(/\na=mid:(\S+)/);
            if (midMatch) videoMids.add(midMatch[1]);

            const mLineMatch = section.match(/\nm=video [^\r\n]+/);
            if (!mLineMatch) return section;
            const mLine = mLineMatch[0].replace(/(\nm=video )\d+/, '$10');
            const mid = midMatch ? `${CRLF}a=mid:${midMatch[1]}` : '';
            return `${mLine}${CRLF}c=IN IP4 0.0.0.0${mid}${CRLF}a=inactive`;
        })
        .join('');

    return patched.replace(
        /(\na=group:BUNDLE)([ \t][^\r\n]+)/g,
        (_, prefix, mids) => {
            const kept = mids.trim().split(/\s+/).filter(m => !videoMids.has(m));
            return kept.length ? `${prefix} ${kept.join(' ')}` : prefix;
        },
    );
}

const _origSRD = wrtc.RTCPeerConnection.prototype.setRemoteDescription;
wrtc.RTCPeerConnection.prototype.setRemoteDescription = function patchedSRD(desc) {
    if (desc && desc.type === 'offer' && desc.sdp) {
        const patchedSdp = sdpRejectVideo(desc.sdp);
        if (patchedSdp !== desc.sdp) {
            desc = new wrtc.RTCSessionDescription({ type: desc.type, sdp: patchedSdp });
        }
    }
    return _origSRD.call(this, desc);
};

global.window = jsdomWindow;
global.self = jsdomWindow;
global.document = jsdomWindow.document;

for (const proto of [jsdomWindow.Element.prototype, jsdomWindow.Document.prototype]) {
    const origQS = proto.querySelector;
    proto.querySelector = function patchedQS(sel) {
        try { return origQS.call(this, sel); } catch (e) {
            if (e.name === 'SyntaxError') return null;
            throw e;
        }
    };
    const origQSA = proto.querySelectorAll;
    proto.querySelectorAll = function patchedQSA(sel) {
        try { return origQSA.call(this, sel); } catch (e) {
            if (e.name === 'SyntaxError') return jsdomWindow.document.createDocumentFragment().querySelectorAll('x');
            throw e;
        }
    };
}

const JSDOM_GLOBALS = [
    'XMLHttpRequest',
    'DOMParser',
    'Event',
    'EventTarget',
    'CustomEvent',
    'MutationObserver',
    'localStorage',
    'sessionStorage',
    'navigator',
    'location',
    'performance',
    'crypto',
    'WebSocket',
    'fetch',
    'Headers',
    'Request',
    'Response',
];
for (const key of JSDOM_GLOBALS) {
    if (jsdomWindow[key] !== undefined && global[key] === undefined) {
        global[key] = jsdomWindow[key];
    }
}

if (!global.crypto) {
    global.crypto = require('crypto').webcrypto;
}
if (!jsdomWindow.crypto) {
    jsdomWindow.crypto = require('crypto').webcrypto;
}

const _noMediaDevices = {
    getUserMedia: () => Promise.reject(new Error('headless — no media devices')),
    enumerateDevices: () => Promise.resolve([]),
};
for (const nav of [global.navigator, jsdomWindow.navigator].filter(Boolean)) {
    if (!nav.mediaDevices) {
        Object.defineProperty(nav, 'mediaDevices', {
            value: _noMediaDevices,
            configurable: true,
        });
    }
}

const _origWarn = console.warn;
console.warn = () => {};
const JitsiMeetJS = require('lib-jitsi-meet/dist/umd/lib-jitsi-meet.min.js');
console.warn = _origWarn;

const fs = require('fs');
const { execSync } = require('child_process');
const { URL } = require('url');
const path = require('path');
const os = require('os');

const { RTCAudioSink } = wrtc.nonstandard;

function log(...args) {
    console.error(...args);
}

function emitCtrl(obj) {
    process.stdout.write(`${JSON.stringify(obj)}\n`);
}

function printUsage() {
    log('Usage: node index.js <jitsi-meeting-url> [options]');
    log('');
    log('  jitsi-meeting-url   Full URL of the meeting, e.g. https://meet.jit.si/MyRoom');
    log('');
    log('Options:');
    log('  --per-speaker           One FIFO per remote audio track (default)');
    log('  --pipe-dir <dir>        Directory for per-track FIFOs (required with --per-speaker)');
    log('  --mixed [<pipe-path>]   Sum all tracks into one FIFO (legacy mode)');
    log('');
    log('Audio output: raw PCM, signed 16-bit LE, 48 kHz, mono');
}

const rawArgs = process.argv.slice(2);
if (rawArgs.length === 0 || rawArgs[0] === '--help' || rawArgs[0] === '-h') {
    printUsage();
    process.exit(0);
}

let meetingUrl = null;
let mode = 'per-speaker';
let mixedPipePath = null;
let pipeDir = null;

for (let i = 0; i < rawArgs.length; i++) {
    const arg = rawArgs[i];
    if (arg === '--per-speaker') {
        mode = 'per-speaker';
    } else if (arg === '--mixed') {
        mode = 'mixed';
        const next = rawArgs[i + 1];
        if (next && !next.startsWith('-')) {
            mixedPipePath = next;
            i += 1;
        }
    } else if (arg === '--pipe-dir') {
        pipeDir = rawArgs[i + 1];
        if (!pipeDir) {
            log('Missing value for --pipe-dir');
            process.exit(1);
        }
        i += 1;
    } else if (!meetingUrl) {
        meetingUrl = arg;
    } else {
        log(`Unexpected argument: ${arg}`);
        printUsage();
        process.exit(1);
    }
}

if (!meetingUrl) {
    log('Missing jitsi-meeting-url');
    printUsage();
    process.exit(1);
}

if (mode === 'per-speaker' && !pipeDir) {
    log('--pipe-dir is required with --per-speaker');
    process.exit(1);
}

if (mode === 'mixed' && !mixedPipePath) {
    mixedPipePath = path.join(os.tmpdir(), `jitsi-audio-${Date.now()}.pcm`);
}

function parseMeetingUrl(urlString) {
    const u = new URL(urlString);
    const domain = u.hostname;
    const pathParts = u.pathname.replace(/^\//, '').replace(/\/$/, '').split('/').filter(Boolean);

    let subdir = null;
    let roomName;

    // meet.jit.si/moderated/<hash> uses a separate XMPP subdir and the hash alone
    // as the MUC room name — not "moderated/<hash>".
    if (pathParts[0] === 'moderated' && pathParts.length >= 2) {
        subdir = 'moderated';
        roomName = pathParts[pathParts.length - 1].toLowerCase();
    } else {
        roomName = pathParts.join('/').toLowerCase();
    }

    if (!roomName) {
        throw new Error('No room name found in URL path');
    }

    let jwt = null;
    if (u.hash && u.hash.length > 1) {
        jwt = new URLSearchParams(u.hash.slice(1)).get('jwt');
    }
    if (!jwt) {
        jwt = u.searchParams.get('jwt');
    }

    return { domain, roomName, subdir, jwt, conferenceRoomName: roomName };
}

function appendRoomParam(url, room) {
    if (!url || !room) return url;
    const sep = url.includes('?') ? '&' : '?';
    return `${url}${sep}room=${encodeURIComponent(room)}`;
}

let domain, roomName, conferenceRoomName, meetingSubdir, meetingJwt;
try {
    ({ domain, roomName, subdir: meetingSubdir, jwt: meetingJwt, conferenceRoomName } = parseMeetingUrl(meetingUrl));
} catch (err) {
    log(`Invalid meeting URL: ${meetingUrl}`);
    log(err.message);
    process.exit(1);
}

log(`Meeting: ${meetingUrl}`);
log(`Domain:  ${domain}`);
log(`Room:    ${roomName}`);
if (meetingSubdir) {
    log(`Subdir:  ${meetingSubdir}`);
}
if (meetingJwt) {
    log('JWT:     (present in URL)');
}
log(`Mode:    ${mode}`);

const SAMPLE_RATE = 48000;
const FRAME_MS = 10;
const FRAME_SAMPLES = (SAMPLE_RATE * FRAME_MS) / 1000;

let createdMixedPipe = false;
let mixedPipeFd = null;
let mixedPipeStream = null;
const latestSamples = new Map();
let mixerInterval = null;

function setupMixedPipe() {
    if (mixedPipeStream) return;
    if (!fs.existsSync(mixedPipePath)) {
        execSync(`mkfifo "${mixedPipePath}"`);
        createdMixedPipe = true;
    }
    log(`Audio pipe: ${mixedPipePath}`);
    mixedPipeFd = fs.openSync(mixedPipePath, fs.constants.O_RDWR);
    mixedPipeStream = fs.createWriteStream(null, { fd: mixedPipeFd });
    mixedPipeStream.on('error', (err) => {
        if (err.code !== 'EPIPE') {
            log('Pipe write error:', err.message);
        }
    });
}

function startMixer() {
    if (mode !== 'mixed' || mixerInterval) return;
    setupMixedPipe();
    mixerInterval = setInterval(() => {
        if (trackSinks.size === 0) return;

        const mixed = new Int16Array(FRAME_SAMPLES);
        for (const entry of trackSinks.values()) {
            if (entry.muted) continue;
            const samples = latestSamples.get(entry.trackId);
            if (!samples) continue;
            const len = Math.min(samples.length, FRAME_SAMPLES);
            for (let i = 0; i < len; i++) {
                mixed[i] = Math.max(-32768, Math.min(32767, mixed[i] + samples[i]));
            }
        }
        try {
            mixedPipeStream.write(Buffer.from(mixed.buffer));
        } catch (_) {
            // no-op
        }
    }, FRAME_MS);
}

function stopMixer() {
    if (mixerInterval) {
        clearInterval(mixerInterval);
        mixerInterval = null;
    }
}

function closeMixedPipe() {
    stopMixer();
    if (mixedPipeStream) {
        try { mixedPipeStream.end(); } catch (_) {}
        mixedPipeStream = null;
    }
    if (mixedPipeFd !== null) {
        try { fs.closeSync(mixedPipeFd); } catch (_) {}
        mixedPipeFd = null;
    }
    if (createdMixedPipe) {
        try { fs.unlinkSync(mixedPipePath); } catch (_) {}
        createdMixedPipe = false;
    }
}

function sanitizeTrackId(trackId) {
    return String(trackId).replace(/[^a-zA-Z0-9._-]+/g, '_');
}

function createTrackPipe(trackId) {
    const fileName = `${sanitizeTrackId(trackId)}.pcm`;
    const pipePath = path.join(pipeDir, fileName);
    execSync(`mkfifo "${pipePath}"`);
    const pipeFd = fs.openSync(pipePath, fs.constants.O_RDWR);
    const pipeStream = fs.createWriteStream(null, { fd: pipeFd });
    pipeStream.on('error', (err) => {
        if (err.code !== 'EPIPE') {
            log(`Pipe write error (${trackId}):`, err.message);
        }
    });
    return { pipePath, pipeFd, pipeStream };
}

function closeTrackPipe(entry) {
    if (!entry) return;
    try { entry.sink.stop(); } catch (_) {}
    if (entry.pipeStream) {
        try { entry.pipeStream.end(); } catch (_) {}
    }
    if (entry.pipeFd !== null && entry.pipeFd !== undefined) {
        try { fs.closeSync(entry.pipeFd); } catch (_) {}
    }
    if (entry.pipePath) {
        try { fs.unlinkSync(entry.pipePath); } catch (_) {}
    }
}

const trackSinks = new Map();
const unmutedParticipants = new Map();

function logUnmuted() {
    const names = [...unmutedParticipants.values()];
    if (names.length > 0) {
        log(`Unmuted participants: ${names.join(', ')}`);
    } else {
        log('Unmuted participants: (none)');
    }
}

function participantName(participantId) {
    const participant = room ? room.getParticipantById(participantId) : null;
    return participant ? (participant.getDisplayName() || participantId) : participantId;
}

function emitTrackAdded(trackId, participantId, name, pipePath, muted) {
    emitCtrl({
        event: 'track',
        action: 'added',
        trackId,
        participantId,
        name,
        pipe: pipePath,
        sampleRate: SAMPLE_RATE,
        muted: !!muted,
    });
}

function emitTrackRemoved(trackId) {
    emitCtrl({ event: 'track', action: 'removed', trackId });
}

function emitTrackMute(trackId, muted) {
    emitCtrl({ event: 'track', action: 'mute', trackId, muted });
}

function onTrackData(entry, samples) {
    // Mark the sink as alive on the very first frame (even while muted, the sink
    // delivers silence frames). A sink that never fires this is orphaned/dead.
    entry.gotData = true;
    if (entry.muted) return;
    if (!entry.receivingAudio) {
        entry.receivingAudio = true;
        log(`Receiving audio from ${entry.name} (${entry.trackId})`);
    }
    if (mode === 'mixed') {
        latestSamples.set(entry.trackId, samples);
        return;
    }
    try {
        entry.pipeStream.write(Buffer.from(
            samples.buffer,
            samples.byteOffset,
            samples.byteLength,
        ));
    } catch (_) {
        // no-op
    }
}

function attachAudioTrack(track) {
    if (track.isLocal() || track.getType() !== 'audio') return;

    const trackId = track.getId();
    if (trackSinks.has(trackId)) {
        return;
    }

    const participantId = track.getParticipantId();
    const mediaTrack = track.track;
    if (!mediaTrack) {
        log(`No MediaStreamTrack yet for ${trackId} — waiting…`);
        let attempts = 0;
        const waitForMedia = setInterval(() => {
            attempts += 1;
            if (trackSinks.has(trackId)) {
                clearInterval(waitForMedia);
                return;
            }
            if (track.track) {
                clearInterval(waitForMedia);
                attachAudioTrack(track);
                return;
            }
            if (attempts >= 300) {
                clearInterval(waitForMedia);
                log(`Timed out waiting for MediaStreamTrack on ${trackId}`);
            }
        }, 100);
        return;
    }

    const name = participantName(participantId);
    const muted = track.isMuted();
    let pipePath = null;
    let pipeFd = null;
    let pipeStream = null;

    if (mode === 'per-speaker') {
        ({ pipePath, pipeFd, pipeStream } = createTrackPipe(trackId));
        emitTrackAdded(trackId, participantId, name, pipePath, muted);
    }

    const entry = {
        trackId,
        track,
        sink: null,
        participantId,
        name,
        muted,
        receivingAudio: false,
        gotData: false,
        addedAt: Date.now(),
        sinkStartedAt: 0,
        sinkRestarts: 0,
        pipePath,
        pipeFd,
        pipeStream,
    };
    createSinkForEntry(entry, mediaTrack);
    trackSinks.set(trackId, entry);
    log(`Attached audio track for ${name} (${trackId}) — muted=${muted}`);

    if (!muted) {
        unmutedParticipants.set(participantId, name);
        logUnmuted();
    }

    if (mode === 'per-speaker' && muted) {
        emitTrackMute(trackId, true);
    }
}

// Wire an RTCAudioSink to the entry's pipe/mixer. Used both for the initial
// attach and when the watchdog needs to replace an orphaned sink.
function createSinkForEntry(entry, mediaTrack) {
    const sink = new RTCAudioSink(mediaTrack);
    sink.ondata = ({ samples }) => onTrackData(entry, samples);
    entry.sink = sink;
    entry.gotData = false;
    entry.sinkStartedAt = Date.now();
}

// In headless wrtc + lib-jitsi-meet, a sink created during the noisy join /
// renegotiation window for a track that is *already* sending audio can end up
// orphaned and never receive PCM. Because Jitsi audio mute/unmute is signaling
// only (it never re-creates the remote track), that dead sink is never replaced
// on its own. Detect sinks that produced zero frames and rebuild them against
// the track's current MediaStreamTrack.
const SINK_DATA_GRACE_MS = 2500;
const MAX_SINK_RESTARTS = 10;
// When the join/renegotiation window orphans the *receiver* (not just the sink),
// rebuilding sinks against the same dead track never recovers audio — only a fresh
// RTCPeerConnection does. If no PCM ever arrives from any unmuted track within this
// window, escalate to a full reconnect (process exits retryably; the Python wrapper
// relaunches with a new connection — the same recovery a manual restart performs).
const NO_AUDIO_RECONNECT_MS = 9000;
// When the bot joins a room where a participant is already present, lib-jitsi-meet
// sometimes never delivers TRACK_ADDED for that participant and the roster sweep finds
// no audio track, so no sink is ever created. A fresh connection renegotiates and gets
// the track, so reconnect if participants are present but no track attached in time.
const NO_TRACKS_RECONNECT_MS = 8000;
let sinkWatchdog = null;

function checkSinks() {
    const now = Date.now();
    for (const entry of trackSinks.values()) {
        if (entry.muted || entry.gotData) continue;
        if (now - entry.sinkStartedAt < SINK_DATA_GRACE_MS) continue;
        if (entry.sinkRestarts >= MAX_SINK_RESTARTS) continue;

        const mediaTrack = entry.track && entry.track.track;
        if (!mediaTrack) continue;

        entry.sinkRestarts += 1;
        log(
            `No audio from ${entry.name} (${entry.trackId}) after ${SINK_DATA_GRACE_MS}ms `
            + `— rebuilding audio sink (attempt ${entry.sinkRestarts}/${MAX_SINK_RESTARTS})`,
        );
        try { entry.sink.stop(); } catch (_) { /* no-op */ }
        createSinkForEntry(entry, mediaTrack);
    }

    maybeReconnectForNoAudio(now);
}

// Force a full reconnect (which the Python wrapper turns into a fresh connection) when
// audio cannot reach whisper on this connection. Two cases:
//   A. Remote participants are present but no audio track ever attached (missed
//      TRACK_ADDED on join — the common "needs a manual restart" symptom).
//   B. A track attached but its receiver is orphaned and produces no PCM.
function maybeReconnectForNoAudio(now) {
    if (disconnecting || !conferenceReady) return;

    if (trackSinks.size === 0) {
        let participantCount = 0;
        try { participantCount = (room && room.getParticipants() || []).length; } catch (_) { return; }
        if (participantCount === 0 || !conferenceJoinedAt) return;
        if (now - conferenceJoinedAt < NO_TRACKS_RECONNECT_MS) return;
        log(
            `No remote audio tracks attached ${NO_TRACKS_RECONNECT_MS}ms after join despite `
            + `${participantCount} participant(s) — forcing a full reconnect`,
        );
        disconnect('no audio tracks attached — reconnecting', { retryable: true });
        return;
    }

    const unmuted = [...trackSinks.values()].filter(e => !e.muted);
    if (unmuted.length === 0) return;
    if (unmuted.some(e => e.gotData)) return;
    if (!unmuted.some(e => now - e.addedAt >= NO_AUDIO_RECONNECT_MS)) return;

    log(
        `No audio from any unmuted track after ${NO_AUDIO_RECONNECT_MS}ms and sink rebuilds `
        + '— forcing a full reconnect',
    );
    disconnect('no audio received — reconnecting', { retryable: true });
}

function startSinkWatchdog() {
    if (sinkWatchdog) return;
    sinkWatchdog = setInterval(checkSinks, 1000);
    if (typeof sinkWatchdog.unref === 'function') sinkWatchdog.unref();
}

// Tracks that already exist when the bot joins are expected to arrive via
// TRACK_ADDED, but that event is not always delivered for participants who were
// already in the room. Walk the roster and attach any audio track we missed.
function attachExistingTracks() {
    if (!room) return;
    let participants = [];
    try { participants = room.getParticipants() || []; } catch (_) { return; }
    for (const participant of participants) {
        let tracks = [];
        try { tracks = (participant.getTracks && participant.getTracks()) || []; } catch (_) { /* no-op */ }
        for (const track of tracks) {
            try {
                if (!track.isLocal() && track.getType() === 'audio' && !trackSinks.has(track.getId())) {
                    log(`Reconciling existing audio track from ${participantName(track.getParticipantId())}`);
                    attachAudioTrack(track);
                }
            } catch (_) { /* no-op */ }
        }
    }
}

function addAudioTrack(track) {
    attachAudioTrack(track);
}

function removeAudioTrack(track) {
    if (track.isLocal() || track.getType() !== 'audio') return;

    const trackId = track.getId();
    const entry = trackSinks.get(trackId);
    if (!entry) return;

    if (mode === 'per-speaker') {
        emitTrackRemoved(trackId);
        closeTrackPipe(entry);
    } else {
        try { entry.sink.stop(); } catch (_) {}
        latestSamples.delete(trackId);
    }
    trackSinks.delete(trackId);
}

function onTrackMuteChanged(track) {
    if (track.isLocal() || track.getType() !== 'audio') return;

    const trackId = track.getId();
    const entry = trackSinks.get(trackId);
    const participantId = track.getParticipantId();
    const name = participantName(participantId);
    const muted = track.isMuted();

    if (entry) {
        entry.muted = muted;
    }

    if (muted) {
        unmutedParticipants.delete(participantId);
    } else {
        unmutedParticipants.set(participantId, name);
    }
    logUnmuted();

    if (mode === 'per-speaker' && entry) {
        emitTrackMute(trackId, muted);
    }
}

let disconnecting = false;
let exitCode = 0;
let room = null;
let connection = null;
let conferenceReady = false;
let conferenceJoinedAt = 0;

async function disconnect(reason, { retryable = false } = {}) {
    if (disconnecting) return;
    disconnecting = true;
    if (retryable) exitCode = 1;

    if (reason) log(`\nDisconnecting: ${reason}`);
    else log('\nDisconnecting…');

    for (const entry of trackSinks.values()) {
        if (mode === 'per-speaker') {
            closeTrackPipe(entry);
        } else {
            try { entry.sink.stop(); } catch (_) {}
        }
    }
    trackSinks.clear();
    latestSamples.clear();
    closeMixedPipe();

    if (room) {
        try { await room.leave(); } catch (_) {}
        room = null;
    }

    if (connection) {
        try { connection.disconnect(); } catch (_) {}
        connection = null;
    }

    process.exit(exitCode);
}

process.on('SIGTERM', () => disconnect('SIGTERM'));
process.on('SIGINT', () => disconnect('SIGINT'));

function isLibJitsiInternalError(err) {
    if (!err) return false;
    const msg = err.message || String(err);
    const stack = err.stack || '';
    return (
        msg.includes('setVideoCodecs') ||
        msg.includes('setCodecPreferences') ||
        msg.includes('getVideoCodecs') ||
        msg.includes('ClearedQueueError') ||
        stack.includes('lib-jitsi-meet.min.js')
    );
}

process.on('uncaughtException', (err) => {
    if (disconnecting) return;
    if (isLibJitsiInternalError(err)) return;
    log('Fatal uncaught error:', err.message);
    disconnect('fatal error');
});

process.on('unhandledRejection', (reason) => {
    if (disconnecting) return;
    const err = reason instanceof Error ? reason : new Error(String(reason));
    if (isLibJitsiInternalError(err)) return;
    log('Fatal unhandled rejection:', err.message);
    disconnect('unhandled rejection');
});

async function fetchServerConfig(targetDomain, subdir) {
    const configPath = subdir ? `/${subdir}/config.js` : '/config.js';
    try {
        const res = await fetch(`https://${targetDomain}${configPath}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const text = await res.text();
        const sandbox = {};
        require('vm').runInNewContext(text, sandbox);
        if (sandbox.config && typeof sandbox.config === 'object') {
            return sandbox.config;
        }
    } catch (err) {
        log(`Could not load server config from ${targetDomain}${configPath}: ${err.message}`);
    }
    return null;
}

JitsiMeetJS.setLogLevel(JitsiMeetJS.logLevels.ERROR);

JitsiMeetJS.init({
    disableAudioLevels: false,
    disableThirdPartyRequests: true,
    enableAnalyticsLogging: false,
});

function onConferenceReady() {
    if (conferenceReady) return;
    conferenceReady = true;
    startSinkWatchdog();
    if (mode === 'per-speaker') {
        emitCtrl({ event: 'ready' });
    } else {
        startMixer();
    }
}

(async () => {
    const serverConfig = await fetchServerConfig(domain, meetingSubdir);

    const hosts = (serverConfig && serverConfig.hosts) || {
        domain,
        anonymousdomain: `guest.${domain}`,
        muc: meetingSubdir
            ? `conference.${meetingSubdir}.${domain}`
            : `conference.${domain}`,
    };

    const baseServiceUrl = (serverConfig && (serverConfig.websocket || serverConfig.bosh))
        || (meetingSubdir
            ? `wss://${domain}/${meetingSubdir}/xmpp-websocket`
            : `wss://${domain}/xmpp-websocket`);
    const serviceUrl = appendRoomParam(baseServiceUrl, roomName);

    const connectionOptions = {
        hosts,
        serviceUrl,
        enableWebsocketResume: true,
    };
    if (serverConfig && serverConfig.conferenceRequestUrl) {
        connectionOptions.conferenceRequestUrl = appendRoomParam(
            serverConfig.conferenceRequestUrl,
            roomName,
        );
    }
    if (serverConfig && serverConfig.websocketKeepAliveUrl) {
        // Room param pins keep-alive to the same shard as the WebSocket (see jitsi-meet constructOptions).
        connectionOptions.websocketKeepAliveUrl = appendRoomParam(
            serverConfig.websocketKeepAliveUrl,
            roomName,
        );
    }
    if (serverConfig && serverConfig.testing) {
        connectionOptions.testing = serverConfig.testing;
    }

    log(`Connecting to ${domain} (${serviceUrl})…`);

    connection = new JitsiMeetJS.JitsiConnection(null, meetingJwt, connectionOptions);

    connection.addEventListener(
        JitsiMeetJS.events.connection.CONNECTION_ESTABLISHED,
        onConnectionEstablished,
    );
    connection.addEventListener(
        JitsiMeetJS.events.connection.CONNECTION_FAILED,
        (err) => {
            if (err === 'connection.shardChangedError') {
                log('Shard changed — reconnecting…');
            } else {
                log('XMPP connection failed:', err);
            }
            disconnect('connection failed', { retryable: true });
        },
    );
    connection.addEventListener(
        JitsiMeetJS.events.connection.CONNECTION_DISCONNECTED,
        () => {
            if (!disconnecting) {
                log('XMPP connection dropped unexpectedly');
                disconnect('connection dropped', { retryable: true });
            }
        },
    );

    connection.connect({ name: conferenceRoomName });
})().catch((err) => {
    log('Startup error:', err);
    disconnect('startup error');
});

function onConnectionEstablished() {
    log('XMPP connection established — joining room…');

    const confOptions = {
        openBridgeChannel: 'datachannel',
        startWithAudioMuted: true,
        startWithVideoMuted: true,
        p2p: { enabled: false },
        testing: { enableCodecSelectionAPI: false },
        videoQuality: { enableAdaptiveMode: false },
    };

    room = connection.initJitsiConference(conferenceRoomName, confOptions);

    room.on(JitsiMeetJS.events.conference.CONFERENCE_ERROR, (err) => {
        log('Conference error:', err);
        disconnect('conference error', { retryable: true });
    });

    room.on(JitsiMeetJS.events.conference.CONFERENCE_JOINED, () => {
        room.setDisplayName('captions-bot');
        conferenceJoinedAt = Date.now();

        try {
            room.setReceiverConstraints({ lastN: 0, defaultConstraints: { maxHeight: 0 } });
        } catch (_) {}

        log(`Joined room "${roomName}" as captions-bot — listening for audio…`);
        onConferenceReady();

        // Existing participants' tracks may not all surface via TRACK_ADDED.
        // Sweep the roster a few times as the session settles to catch them.
        for (const delay of [1000, 3000, 6000, 10000]) {
            setTimeout(attachExistingTracks, delay);
        }
    });

    room.on(JitsiMeetJS.events.conference.CONFERENCE_LEFT, () => {
        log('Left conference');
    });

    room.on(JitsiMeetJS.events.conference.CONFERENCE_FAILED, (err, ...rest) => {
        if (err === JitsiMeetJS.errors.conference.MEMBERS_ONLY_ERROR) {
            const lobbyJid = rest[0];
            const lobbyWaitingForHost = rest[1];
            log(
                'MEMBERS_ONLY_ERROR — lobby JID:',
                JSON.stringify(lobbyJid),
                'waitingForHost:',
                lobbyWaitingForHost,
            );
            // lobbyWaitingForHost means the room needs JWT/moderator auth, not a knock-to-enter lobby.
            if (lobbyWaitingForHost) {
                log(
                    'Room requires authentication (moderated or secure domain) — '
                    + 'pass the full meeting URL including #jwt=… from the browser address bar',
                );
                disconnect('authentication required', { retryable: false });
                return;
            }
            if (lobbyJid) {
                log('Room has lobby enabled — joining lobby and waiting for moderator approval…');
                let lobbyHeartbeat = null;
                room.joinLobby('captions-bot', '').then(() => {
                    log('In lobby — waiting for moderator to admit captions-bot…');
                    lobbyHeartbeat = setInterval(() => {
                        log('Still waiting in lobby for moderator approval…');
                    }, 15_000);
                }).catch((e) => {
                    log('Failed to join lobby:', e && e.message || e);
                    clearInterval(lobbyHeartbeat);
                    disconnect('lobby join failed', { retryable: true });
                });

                room.once(JitsiMeetJS.events.conference.CONFERENCE_JOINED, () => {
                    clearInterval(lobbyHeartbeat);
                });
            } else {
                log('Meeting access denied (no lobby) — will retry…');
                disconnect('members only, no lobby', { retryable: true });
            }
            return;
        }
        if (err === JitsiMeetJS.errors.conference.CONFERENCE_ACCESS_DENIED) {
            log('Access denied (rejected from lobby by moderator) — will retry…');
            disconnect('access denied by moderator', { retryable: true });
            return;
        }
        log('Conference failed:', err, ...rest);
        disconnect('conference failed', { retryable: true });
    });

    room.on(JitsiMeetJS.events.conference.KICKED, () => {
        log('Kicked from conference by moderator');
        disconnect('kicked by moderator', { retryable: false });
    });

    room.on(JitsiMeetJS.events.conference.USER_JOINED, (id, participant) => {
        const name = participant.getDisplayName() || id;
        log(`Participant joined: ${name}`);
        setTimeout(attachExistingTracks, 1000);
    });

    room.on(JitsiMeetJS.events.conference.USER_LEFT, (id) => {
        const name = unmutedParticipants.get(id) || id;
        unmutedParticipants.delete(id);
        log(`Participant left: ${name}`);
        logUnmuted();

        const remaining = room ? room.getParticipants() : [];
        if (remaining.length === 0) {
            setImmediate(() => disconnect('all participants have left'));
        }
    });

    room.on(JitsiMeetJS.events.conference.TRACK_ADDED, addAudioTrack);
    room.on(JitsiMeetJS.events.conference.TRACK_REMOVED, removeAudioTrack);
    room.on(JitsiMeetJS.events.conference.TRACK_MUTE_CHANGED, onTrackMuteChanged);

    room.join();
}

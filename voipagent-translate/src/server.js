const fs = require('fs');
const path = require('path');
const http = require('http');
const express = require('express');
const WebSocket = require('ws');
require('dotenv').config();

const HOST = process.env.HOST || '127.0.0.1';
const PORT = Number(process.env.PORT || 4010);
const EVENT_LOG = process.env.TRANSLATION_EVENT_LOG || '/opt/voipagent-translate/data/translation-events.jsonl';
const RECENT_CALL_HOURS = Number(process.env.RECENT_CALL_HOURS || 12);
const MAX_EVENTS_PER_CALL = Number(process.env.MAX_EVENTS_PER_CALL || 500);

const app = express();
const server = http.createServer(app);
const wss = new WebSocket.Server({ server, path: '/ws' });

const calls = new Map();

let fileOffset = 0;
let tailRemainder = '';
let tailInProgress = false;

function ensureEventLog() {
  fs.mkdirSync(path.dirname(EVENT_LOG), { recursive: true });
  if (!fs.existsSync(EVENT_LOG)) {
    fs.writeFileSync(EVENT_LOG, '', 'utf8');
  }
}

function safeJsonParse(line) {
  try {
    return JSON.parse(line);
  } catch (error) {
    console.error('Failed to parse dashboard event line:', error.message);
    return null;
  }
}

function normalizeTs(value) {
  if (!value) {
    return new Date().toISOString();
  }
  return value;
}

function getOrCreateCall(callId) {
  if (!calls.has(callId)) {
    calls.set(callId, {
      callId,
      cid: '',
      dest: '',
      lang: '',
      callerUuid: '',
      calleeUuid: '',
      active: true,
      startedAt: null,
      readyAt: null,
      endedAt: null,
      lastEventAt: null,
      endReason: '',
      disconnectedRole: '',
      rolesConnected: {
        caller: false,
        callee: false
      },
      events: []
    });
  }
  return calls.get(callId);
}

function pushCallEvent(call, event) {
  call.events.push(event);
  if (call.events.length > MAX_EVENTS_PER_CALL) {
    call.events.splice(0, call.events.length - MAX_EVENTS_PER_CALL);
  }
}

function applyPlaybackUpdate(call, event, ts) {
  const responseId = event.responseId || '';
  for (let index = call.events.length - 1; index >= 0; index -= 1) {
    const existing = call.events[index];
    const sameResponse = responseId && existing.responseId === responseId;
    const fallbackMatch =
      !responseId &&
      existing.type === 'utterance.translated' &&
      existing.direction === (event.direction || '') &&
      existing.text === (event.text || '');
    if (!sameResponse && !fallbackMatch) {
      continue;
    }
    existing.playbackStartedAt = ts;
    existing.playbackMs = Number(event.playbackMs || 0);
    break;
  }
}

function applyEvent(event, { silent = false } = {}) {
  if (!event || !event.type) {
    return;
  }

  const ts = normalizeTs(event.ts);
  const callId = event.callId;
  if (!callId) {
    return;
  }

  const call = getOrCreateCall(callId);
  call.lastEventAt = ts;
  if (!call.startedAt) {
    call.startedAt = ts;
  }

  if (event.cid) call.cid = event.cid;
  if (event.dest) call.dest = event.dest;
  if (event.lang) call.lang = event.lang;
  if (event.callerUuid) call.callerUuid = event.callerUuid;
  if (event.calleeUuid) call.calleeUuid = event.calleeUuid;

  switch (event.type) {
    case 'call.upsert':
      call.active = event.active !== false;
      if (call.active) {
        call.endedAt = null;
        call.endReason = '';
        call.disconnectedRole = '';
      }
      if (event.role === 'caller' || event.role === 'callee') {
        call.rolesConnected[event.role] = true;
      }
      break;

    case 'call.ready':
      call.active = true;
      call.endedAt = null;
      call.endReason = '';
      call.disconnectedRole = '';
      call.readyAt = ts;
      break;

    case 'utterance.original':
    case 'utterance.translated':
    case 'utterance.filtered':
      if (!call.endedAt) {
        call.active = true;
      }
      pushCallEvent(call, {
        id: `${callId}:${call.events.length + 1}:${Date.parse(ts)}`,
        type: event.type,
        ts,
        direction: event.direction || '',
        srcLang: event.srcLang || '',
        dstLang: event.dstLang || '',
        responseId: event.responseId || '',
        text: event.text || '',
        original: event.original || '',
        reason: event.reason || '',
        playbackStartedAt: event.playbackStartedAt || '',
        playbackMs: Number(event.playbackMs || 0)
      });
      break;

    case 'utterance.playback':
      if (!call.endedAt) {
        call.active = true;
      }
      applyPlaybackUpdate(call, event, ts);
      break;

    case 'call.ended':
      call.active = false;
      call.endedAt = ts;
      call.endReason = event.reason || '';
      call.disconnectedRole = event.role || '';
      break;

    default:
      break;
  }

  if (!silent) {
    broadcast({
      type: 'call.updated',
      call: summarizeCall(call),
      event: publicEvent(event)
    });
  }
}

function publicEvent(event) {
  if (!event) {
    return null;
  }
  if (event.type === 'call.upsert' || event.type === 'call.ready' || event.type === 'call.ended') {
    return {
      type: event.type,
      ts: normalizeTs(event.ts),
      role: event.role || '',
      reason: event.reason || ''
    };
  }
  if (event.type === 'utterance.original' || event.type === 'utterance.translated' || event.type === 'utterance.filtered') {
    return {
      type: event.type,
      ts: normalizeTs(event.ts),
      direction: event.direction || '',
      responseId: event.responseId || '',
      text: event.text || '',
      original: event.original || '',
      reason: event.reason || '',
      playbackStartedAt: event.playbackStartedAt || '',
      playbackMs: Number(event.playbackMs || 0)
    };
  }
  if (event.type === 'utterance.playback') {
    return {
      type: event.type,
      ts: normalizeTs(event.ts),
      direction: event.direction || '',
      responseId: event.responseId || '',
      text: event.text || '',
      playbackMs: Number(event.playbackMs || 0)
    };
  }
  return null;
}

function summarizeCall(call) {
  const isActive = call.active && !call.endedAt;
  return {
    callId: call.callId,
    cid: call.cid,
    dest: call.dest,
    lang: call.lang,
    callerUuid: call.callerUuid,
    calleeUuid: call.calleeUuid,
    active: isActive,
    startedAt: call.startedAt,
    readyAt: call.readyAt,
    endedAt: call.endedAt,
    lastEventAt: call.lastEventAt,
    endReason: call.endReason,
    disconnectedRole: call.disconnectedRole,
    rolesConnected: call.rolesConnected,
    eventCount: call.events.length
  };
}

function visibleSummaries() {
  return Array.from(calls.values())
    .filter((call) => call.active && !call.endedAt)
    .sort((a, b) => {
      return Date.parse(b.lastEventAt || b.startedAt || 0) - Date.parse(a.lastEventAt || a.startedAt || 0);
    })
    .map(summarizeCall);
}

function broadcast(payload) {
  const message = JSON.stringify(payload);
  for (const client of wss.clients) {
    if (client.readyState === WebSocket.OPEN) {
      client.send(message);
    }
  }
}

function processChunk(text, { silent = false } = {}) {
  tailRemainder += text;

  while (true) {
    const newlineIndex = tailRemainder.indexOf('\n');
    if (newlineIndex === -1) {
      break;
    }

    const line = tailRemainder.slice(0, newlineIndex).trim();
    tailRemainder = tailRemainder.slice(newlineIndex + 1);
    if (!line) {
      continue;
    }

    const event = safeJsonParse(line);
    if (event) {
      applyEvent(event, { silent });
    }
  }
}

function loadHistory() {
  ensureEventLog();
  const content = fs.readFileSync(EVENT_LOG, 'utf8');
  tailRemainder = '';
  processChunk(content, { silent: true });
  fileOffset = Buffer.byteLength(content);
}

function tailEventLog() {
  if (tailInProgress) {
    return;
  }

  tailInProgress = true;
  fs.stat(EVENT_LOG, (error, stats) => {
    if (error) {
      tailInProgress = false;
      return;
    }

    if (stats.size < fileOffset) {
      calls.clear();
      fileOffset = 0;
      tailRemainder = '';
      loadHistory();
      broadcast({ type: 'snapshot', calls: visibleSummaries() });
      tailInProgress = false;
      return;
    }

    if (stats.size === fileOffset) {
      tailInProgress = false;
      return;
    }

    const stream = fs.createReadStream(EVENT_LOG, {
      start: fileOffset,
      end: stats.size - 1,
      encoding: 'utf8'
    });

    let chunk = '';
    stream.on('data', (data) => {
      chunk += data;
    });

    stream.on('end', () => {
      fileOffset = stats.size;
      processChunk(chunk, { silent: false });
      tailInProgress = false;
    });

    stream.on('error', () => {
      tailInProgress = false;
    });
  });
}

function pruneCalls() {
  const cutoff = Date.now() - (RECENT_CALL_HOURS * 60 * 60 * 1000);
  for (const [callId, call] of calls.entries()) {
    const ts = Date.parse(call.lastEventAt || call.startedAt || 0);
    if (!call.active && ts && ts < cutoff) {
      calls.delete(callId);
    }
  }
}

app.use(express.static(path.join(__dirname, '..', 'public')));

app.get('/health', (_req, res) => {
  res.json({ ok: true, calls: calls.size });
});

app.get('/api/calls', (_req, res) => {
  res.json({ calls: visibleSummaries() });
});

app.get('/api/calls/:callId', (req, res) => {
  const call = calls.get(req.params.callId);
  if (!call) {
    res.status(404).json({ error: 'Call not found' });
    return;
  }

  res.json({
    call: summarizeCall(call),
    events: call.events
  });
});

wss.on('connection', (socket) => {
  socket.send(JSON.stringify({
    type: 'snapshot',
    calls: visibleSummaries()
  }));
});

ensureEventLog();
loadHistory();
setInterval(tailEventLog, 700);
setInterval(pruneCalls, 60_000);

server.listen(PORT, HOST, () => {
  console.log(`voipagent-translate listening on http://${HOST}:${PORT}`);
  console.log(`Reading events from ${EVENT_LOG}`);
});

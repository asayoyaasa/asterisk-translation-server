const state = {
  calls: new Map(),
  selectedCallId: null,
  eventsByCall: new Map(),
  search: ''
};

const callListEl = document.getElementById('call-list');
const timelineEl = document.getElementById('timeline');
const emptyStateEl = document.getElementById('empty-state');
const callTitleEl = document.getElementById('call-title');
const callStatusEl = document.getElementById('call-status');
const callLangEl = document.getElementById('call-lang');
const callCidEl = document.getElementById('call-cid');
const searchEl = document.getElementById('search');

function formatWhen(value) {
  if (!value) return '-';
  return new Date(value).toLocaleString();
}

function statusLabel(call) {
  if (!call) return 'Waiting';
  if (call.active) return 'Active';
  return 'Ended';
}

function statusClass(call) {
  if (!call) return 'idle';
  if (call.active) return 'active';
  return 'ended';
}

function sortedCalls() {
  return Array.from(state.calls.values())
    .filter((call) => call.active)
    .filter((call) => {
      if (!state.search) return true;
      const haystack = [
        call.callId,
        call.cid,
        call.dest,
        call.lang,
        call.callerUuid,
        call.calleeUuid
      ].join(' ').toLowerCase();
      return haystack.includes(state.search.toLowerCase());
    })
    .sort((a, b) => {
      return Date.parse(b.lastEventAt || b.startedAt || 0) - Date.parse(a.lastEventAt || a.startedAt || 0);
    });
}

function syncSelectedCall() {
  const calls = sortedCalls();
  if (!calls.length) {
    state.selectedCallId = null;
    return;
  }

  const selected = state.selectedCallId ? state.calls.get(state.selectedCallId) : null;
  if (!selected || !selected.active || !calls.some((call) => call.callId === state.selectedCallId)) {
    state.selectedCallId = calls[0].callId;
  }
}

function renderCallList() {
  const calls = sortedCalls();
  callListEl.innerHTML = '';

  if (!calls.length) {
    callListEl.innerHTML = '<div class="empty-state"><h3>No active calls</h3><p>Only live calls are shown here. Start a call to see it appear.</p></div>';
    return;
  }

  for (const call of calls) {
    const card = document.createElement('button');
    card.type = 'button';
    card.className = `call-card${call.callId === state.selectedCallId ? ' active' : ''}`;
    card.innerHTML = `
      <div class="call-card-head">
        <div class="call-card-title">${call.cid || call.dest || call.callId.slice(0, 8)}</div>
        <span class="status-dot ${call.active ? 'active' : ''}"></span>
      </div>
      <div class="call-card-subtitle">${call.dest || 'No destination'} • ${call.lang || 'Unknown language'}</div>
      <div class="call-card-meta">
        <span class="call-card-mini">Live now</span>
        <span class="call-card-mini">${formatWhen(call.lastEventAt || call.startedAt)}</span>
      </div>
    `;
    card.addEventListener('click', () => {
      state.selectedCallId = call.callId;
      if (!state.eventsByCall.has(call.callId)) {
        fetchCall(call.callId);
      }
      render();
    });
    callListEl.appendChild(card);
  }
}

function eventSide(event) {
  const direction = event.direction || '';
  if (direction === 'caller→callee') {
    return 'caller';
  }
  if (direction === 'callee→caller') {
    return 'callee';
  }
  return 'caller';
}

function eventSpeakerLabel(event) {
  const side = eventSide(event);
  const stage = event.type === 'utterance.original' ? 'Original' : 'Translated';
  return `${side === 'caller' ? 'Caller' : 'Callee'} • ${stage}`;
}

function playbackState(event) {
  if (event.type !== 'utterance.translated') {
    return {
      status: 'idle',
      progressPct: 0,
      elapsedMs: 0,
      durationMs: 0
    };
  }

  const durationMs = Number(event.playbackMs || 0);
  if (!event.playbackStartedAt || !durationMs) {
    return {
      status: 'queued',
      progressPct: 0,
      elapsedMs: 0,
      durationMs: 0
    };
  }

  const startedAtMs = Date.parse(event.playbackStartedAt);
  const elapsedMs = Math.max(0, Date.now() - startedAtMs);
  const progressPct = Math.max(0, Math.min(100, (elapsedMs / durationMs) * 100));
  return {
    status: elapsedMs >= durationMs ? 'done' : 'playing',
    progressPct,
    elapsedMs: Math.min(elapsedMs, durationMs),
    durationMs
  };
}

function playbackLabel(event) {
  const playback = playbackState(event);
  if (event.type !== 'utterance.translated') return '';
  if (playback.status === 'queued') return 'Queued';
  if (playback.status === 'playing') return 'Speaking';
  return 'Spoken';
}

function karaokeTextMarkup(event) {
  const text = escapeHtml(event.text || '[empty]');
  if (event.type !== 'utterance.translated') {
    return `<div class="timeline-card-text">${text}</div>`;
  }

  const playback = playbackState(event);
  const style = playback.durationMs
    ? ` style="--karaoke-duration:${playback.durationMs}ms; --karaoke-delay:-${playback.elapsedMs}ms; --karaoke-progress:${playback.progressPct}%"`
    : '';

  return `
    <div class="timeline-card-text karaoke-text ${playback.status}"${style}>
      <span class="karaoke-base">${text}</span>
      <span class="karaoke-fill">${text}</span>
    </div>
    <div class="karaoke-meter">
      <div class="karaoke-meter-fill ${playback.status}"${style}></div>
    </div>
  `;
}

function groupTimelineEvents(events) {
  const groups = [];

  for (const event of events) {
    const side = eventSide(event);
    const previous = groups[groups.length - 1];
    const canPairWithPrevious =
      previous &&
      previous.side === side &&
      previous.direction === (event.direction || '') &&
      previous.events.length < 2 &&
      previous.events[previous.events.length - 1].type !== event.type;

    if (canPairWithPrevious) {
      previous.events.push(event);
      previous.lastTs = event.ts;
      continue;
    }

    groups.push({
      side,
      direction: event.direction || '',
      lastTs: event.ts,
      events: [event]
    });
  }

  return groups;
}

function scrollTimelineToLatest() {
  const lastCard = timelineEl.lastElementChild;
  if (!lastCard) return;
  window.requestAnimationFrame(() => {
    lastCard.scrollIntoView({ block: 'end', behavior: 'smooth' });
  });
}

function renderTimeline() {
  const call = state.selectedCallId ? state.calls.get(state.selectedCallId) : null;
  const events = state.selectedCallId
    ? (state.eventsByCall.get(state.selectedCallId) || []).filter((event) =>
        event.type === 'utterance.original' || event.type === 'utterance.translated')
      : [];
  const groups = groupTimelineEvents(events);

  if (!call) {
    emptyStateEl.classList.remove('hidden');
    timelineEl.classList.add('hidden');
    callTitleEl.textContent = 'No call selected';
    callStatusEl.className = 'status-pill idle';
    callStatusEl.textContent = 'Waiting';
    callLangEl.textContent = 'Language: -';
    callCidEl.textContent = 'CID: -';
    return;
  }

  callTitleEl.textContent = `${call.dest || call.callId} • ${call.callId.slice(0, 8)}`;
  callStatusEl.className = `status-pill ${statusClass(call)}`;
  callStatusEl.textContent = statusLabel(call);
  callLangEl.textContent = `Language: ${call.lang || '-'}`;
  callCidEl.textContent = `CID: ${call.cid || '-'}`;

  emptyStateEl.classList.add('hidden');
  timelineEl.classList.remove('hidden');
  timelineEl.innerHTML = '';

  if (!groups.length) {
    timelineEl.innerHTML = '<div class="empty-state"><h3>No transcript yet</h3><p>This call is selected, but no ORIGINAL or TRANSLATED rows have arrived yet.</p></div>';
    return;
  }

  for (const group of groups) {
    const groupEl = document.createElement('section');
    groupEl.className = `chat-group ${group.side}`;

    const directionLabel = group.direction || 'unknown direction';
    groupEl.innerHTML = `
      <div class="chat-group-head ${group.side}">
        <div class="chat-group-badges">
          <span class="speaker-pill ${group.side}">${group.side === 'caller' ? 'Caller' : 'Callee'}</span>
          <span class="direction-label">${escapeHtml(directionLabel)}</span>
        </div>
        <div class="timeline-card-subtitle">${formatWhen(group.lastTs)}</div>
      </div>
    `;

    const stackEl = document.createElement('div');
    stackEl.className = `chat-stack ${group.side}`;

    for (const event of group.events) {
      const stage = event.type === 'utterance.original'
        ? 'original'
        : event.type === 'utterance.translated'
        ? 'translated'
        : 'filtered';

      const bubble = document.createElement('article');
      bubble.className = `message-bubble ${stage} ${group.side}`;
      const playback = playbackLabel(event);
      bubble.innerHTML = `
        <div class="message-bubble-head">
          <div class="message-bubble-badges">
            <span class="stage-pill ${stage}">${stage.toUpperCase()}</span>
            ${playback ? `<span class="playback-pill ${playback.toLowerCase()}">${playback}</span>` : ''}
          </div>
          <span class="timeline-card-subtitle">${escapeHtml(eventSpeakerLabel(event))}</span>
        </div>
        ${karaokeTextMarkup(event)}
        ${event.reason ? `<div class="timeline-card-subtitle">Reason: ${escapeHtml(event.reason)}</div>` : ''}
      `;
      stackEl.appendChild(bubble);
    }

    groupEl.appendChild(stackEl);
    timelineEl.appendChild(groupEl);
  }

  scrollTimelineToLatest();
}

function escapeHtml(value) {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function render() {
  renderCallList();
  renderTimeline();
}

async function fetchCalls() {
  const response = await fetch('/api/calls');
  const data = await response.json();
  state.calls.clear();
  for (const call of data.calls) {
    state.calls.set(call.callId, call);
  }
  syncSelectedCall();
  if (state.selectedCallId) {
    await fetchCall(state.selectedCallId);
  }
  render();
}

async function fetchCall(callId) {
  const response = await fetch(`/api/calls/${encodeURIComponent(callId)}`);
  if (!response.ok) return;
  const data = await response.json();
  state.calls.set(callId, data.call);
  state.eventsByCall.set(callId, data.events);
  render();
}

function connectWebSocket() {
  const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
  const socket = new WebSocket(`${protocol}://${window.location.host}/ws`);

  socket.addEventListener('message', async (message) => {
    const payload = JSON.parse(message.data);

    if (payload.type === 'snapshot') {
      state.calls.clear();
      for (const call of payload.calls) {
        state.calls.set(call.callId, call);
      }
      syncSelectedCall();
      if (state.selectedCallId) {
        await fetchCall(state.selectedCallId);
      }
      render();
      return;
    }

    if (payload.type === 'call.updated' && payload.call) {
      if (payload.call.active) {
        state.calls.set(payload.call.callId, payload.call);
      } else {
        state.calls.delete(payload.call.callId);
      }
      if (payload.event && payload.call.callId) {
        const eventTypes = new Set(['utterance.original', 'utterance.translated']);
        if (eventTypes.has(payload.event.type)) {
          const current = state.eventsByCall.get(payload.call.callId) || [];
          state.eventsByCall.set(payload.call.callId, [...current, payload.event]);
        } else if (payload.event.type === 'utterance.playback') {
          const current = state.eventsByCall.get(payload.call.callId) || [];
          const updated = current.map((event) => {
            const sameResponse = payload.event.responseId && event.responseId === payload.event.responseId;
            const fallbackMatch =
              !payload.event.responseId &&
              event.type === 'utterance.translated' &&
              event.direction === payload.event.direction &&
              event.text === payload.event.text;
            if (!sameResponse && !fallbackMatch) {
              return event;
            }
            return {
              ...event,
              playbackStartedAt: payload.event.ts,
              playbackMs: Number(payload.event.playbackMs || 0)
            };
          });
          state.eventsByCall.set(payload.call.callId, updated);
        }
      }
      syncSelectedCall();
      if (state.selectedCallId && !state.eventsByCall.has(state.selectedCallId)) {
        await fetchCall(state.selectedCallId);
      }
      render();
    }
  });

  socket.addEventListener('close', () => {
    setTimeout(connectWebSocket, 1500);
  });
}

searchEl.addEventListener('input', (event) => {
  state.search = event.target.value || '';
  renderCallList();
});

fetchCalls().catch((error) => {
  console.error(error);
});
connectWebSocket();

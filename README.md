# Asterisk Real-Time Translation Server

A production-grade, full-duplex, simultaneous-interpretation voice translation system for live phone calls. Built on Asterisk 20 AudioSocket and the OpenAI Realtime API. Translates both directions of a call in real time — callers hear each other in their own language with a maximum lag of 6 seconds, mirroring how professional human interpreters work.

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Simultaneous Interpretation Engine](#simultaneous-interpretation-engine)
- [Anti-Hallucination System](#anti-hallucination-system)
- [Session Pre-warming](#session-pre-warming)
- [Barge-in Handling](#barge-in-handling)
- [Requirements](#requirements)
- [Installation](#installation)
- [Asterisk Configuration](#asterisk-configuration)
- [Language Detection](#language-detection)
- [Configuration Reference](#configuration-reference)
- [Deployment](#deployment)
- [Log Reference](#log-reference)
- [Audio Pipeline](#audio-pipeline)
- [Known Limitations](#known-limitations)

---

## Overview

When a call comes in, the server:

1. Detects the destination country and maps it to a language
2. Pre-warms two OpenAI Realtime sessions during ringback
3. Bridges both audio legs through a simultaneous-interpretation pipeline
4. Translates speech in real time, chunk by chunk, with barge-in support

```
Caller (English) ──► AudioSocket ──► Translation Server ──► OpenAI Realtime ──► Callee hears Indonesian
Caller (English) ◄── AudioSocket ◄── Translation Server ◄── OpenAI Realtime ◄── Callee speaks Indonesian
```

Both directions run as fully independent pipelines. Each speaker can interrupt the other at any time and the system handles it gracefully.

---

## Architecture

### Call Setup Flow

```
1.  Caller dials → VoipNow → Asterisk [from-voipnow]
2.  Asterisk generates CALLER_UUID + CALLEE_UUID
3.  Writes /tmp/call_CALLER_UUID.txt: "caller_uuid callee_uuid dest_number callerid"
4.  Answer() + AudioSocket(CALLER_UUID, 127.0.0.1:5001)
5.  Server identifies role=caller, starts ringback (425Hz), fires AMI Originate
6.  ── DURING RINGBACK: two OpenAI sessions pre-warmed in background ──
7.  Callee answers → AudioSocket(CALLEE_UUID, 127.0.0.1:5001)
8.  Server identifies role=callee, stops ringback, starts bridge
9.  Two one_way_bridge coroutines launched in parallel
```

### Core Coroutines (per bridge direction)

Each `one_way_bridge` runs two coroutines gathered together:

| Coroutine | Role |
|---|---|
| `chunker()` | Reads raw audio from the call leg, streams it live to OpenAI, commits chunks on time/clause/barge-in triggers |
| `pipe_out()` | Receives translated audio from OpenAI, plays it to the other party, runs the post-filter, handles all OpenAI events |

### Shared State (per bridge direction)

| Variable | Type | Purpose |
|---|---|---|
| `chunk_done` | `asyncio.Event` | Set by pipe_out after cooldown; gates the next chunk |
| `partial_transcript` | `list[str]` | Updated by pipe_out ASR deltas; read by chunker clause trigger |
| `last_delta_time` | `list[float]` | Timestamp of last ASR delta; used for temporal stability |
| `abort_playback` | `asyncio.Event` | Set on barge-in; checked per frame in pipe_out |
| `manual_commit_pending` | `list[bool]` | Set by chunker before response.create; prevents pipe_out double-commit |
| `speaking_flag` | `list[bool]` | True while translation audio is playing; suppresses echo input |
| `peer_speaking_flag` | `list[bool]` | The other direction's speaking_flag; triggers barge-in |

---

## Simultaneous Interpretation Engine

The core innovation is `chunker()`, which replaces the traditional wait-for-VAD approach with a multi-trigger commit system.

### How it works

Audio is **streamed live to OpenAI continuously** while `chunker()` watches three independent commit triggers:

```
Speaker talks
     │
     ▼
chunker() — streams every frame to OpenAI live
     │
     ├── Time trigger: elapsed >= 6s → commit
     │
     ├── Clause trigger: partial transcript ends with punctuation
     │   + at least 4 words + 400ms ASR quiet → commit
     │
     └── Barge-in: peer starts speaking → drop + cancel
                 │
                 ▼
     input_audio_buffer.commit + response.create (conversation:none)
                 │
                 ▼
     OpenAI translates → pipe_out plays audio to other party
```

### Time Trigger

Every 6 seconds of continuous speech (`CHUNK_SECONDS = 6`), the current audio window is force-committed and translated. This is the hard latency ceiling — a speaker talking for 1 minute will have their speech translated in ~10 chunks as they go, with the callee hearing translations every 6 seconds rather than waiting the full minute.

### Clause Trigger

`pipe_out` updates `partial_transcript` with ASR delta events as the speaker talks. `chunker` checks this on every audio frame and fires early if:

- Partial transcript is at least 30 characters
- Last character is a clause boundary marker: `. , ! ? ; : ، 。 、 ！ ？`
- At least 4 words in the transcript (prevents micro-fragments)
- At least 400ms have passed since the last ASR delta (temporal stability — prevents committing mid-thought when the speaker briefly pauses)

### Stateless Translation

Every `response.create` includes `"conversation": "none"` and the full translation prompt. Each chunk is translated in complete isolation with no memory of previous chunks. This prevents context drift — the model cannot "helpfully" inject earlier conversation topics into the translation.

### Double-Commit Prevention

`manual_commit_pending[0]` is set to `True` immediately before every manual `response.create` in `_commit_chunk`. When `pipe_out` receives the resulting `input_audio_buffer.committed` event, it checks this flag first. If True, it clears the flag and skips its own `response.create`. If False (VAD committed naturally), it fires `response.create` normally. This ensures exactly one translation per audio commit.

---

## Anti-Hallucination System

Every translation passes through a multi-layer post-filter in `pipe_out` before any audio plays. If any check fails, the entire response is silently blocked.

| Filter | Detects | Example blocked output |
|---|---|---|
| Empty transcript | Ghost audio with no speech | Silent response |
| Low confidence | Filler sounds, fragments under 2 chars | "uh", "um", "hmm" |
| Assistant phrases | Model responding as chatbot | "How can I help you today?" |
| Sentence expansion | Output has significantly more sentences than input | 1 sentence in → 4 sentences out |
| Length ratio | Output more than 3× longer than input (20 char cap for very short inputs) | "Ya" → "Yes, that is certainly correct" |
| Hallucinated numbers | Numbers in translation not present in original | "HKN revenue" → "$200 million" |

When a filter blocks a response:
1. `current_response_blocked = True` — all subsequent `response.audio.delta` events for this response are dropped
2. `response.cancel` is sent to OpenAI (if a response is active) to stop generation
3. `chunk_done.set()` — the pipeline unblocks immediately for the next utterance

The filter runs on `response.audio_transcript.done` which arrives just before or alongside the audio. Because `response_active` is tracked, `response.cancel` is only sent when there's actually something to cancel.

---

## Session Pre-warming

OpenAI Realtime sessions take 500ms–1s to initialize. Without pre-warming, the first word a speaker says after the call connects is lost to cold-start latency.

### How it works

The moment the caller connects (during ringback), two `prewarm_openai_session` tasks are launched as background tasks:

```python
pw_caller = asyncio.create_task(
    prewarm_openai_session("English", lang, ...))  # for caller → callee
pw_callee = asyncio.create_task(
    prewarm_openai_session(lang, "English", ...))  # for callee → caller
```

Each prewarm task:
1. Opens a WebSocket to OpenAI
2. Sends `session.update` with the full configuration
3. Waits for `session.created` acknowledgement
4. Keeps the WebSocket alive (idle loop) until the bridge takes ownership

When the callee answers and `run_bridge()` starts, it retrieves the pre-warmed WebSocket references and passes them directly to `one_way_bridge` via `prewarmed_ws=`. The bridge skips its own `session.update` and is immediately ready.

### Ownership Transfer

Pre-warmed sessions use `await websockets.connect(...)` (not `async with`) so the WebSocket stays open after the prewarm function's loop ends. The prewarm task is cancelled by `run_bridge()` once the bridge has taken over. The WebSocket is only closed in `finally` if `ws_holder[0] is None` (prewarm failed), ensuring the bridge always owns a valid connection.

---

## Barge-in Handling

When one party speaks while the other's translation is playing:

### In `chunker()` (audio collection side)

1. `current_chunk` is dropped and `sending_live` reset
2. `input_audio_buffer.clear` sent to OpenAI
3. `abort_playback.set()` — signals pipe_out to stop immediately
4. `response.cancel` sent to OpenAI (silently ignores `response_cancel_not_active`)

### In `pipe_out()` (playback side)

- `abort_playback` is checked at the start of every `response.audio.delta` event
- Inside the frame playback loop, `abort_playback` is also checked every 320-byte frame (every ~20ms)
- Silent padding frames are skipped if playback was aborted
- When abort is detected, `response.cancel` + `input_audio_buffer.clear` are sent

Result: audio stops within one frame (~20ms) of barge-in detection. No overlap, no ghost speech.

---

## Requirements

### System

- Ubuntu 20.04+ / Debian 11+
- Asterisk 20+ with AudioSocket module enabled
- Python 3.10+
- SIP trunk (tested with DIDLogic)
- VoipNow or similar PBX for inbound routing

### Python Dependencies

```
websockets
soxr
numpy==1.26.4
```

> **Note:** numpy 2.x is incompatible with older CPU instruction sets. Pin to 1.26.4.

### External Services

- OpenAI API key with access to `gpt-4o-realtime-preview-2024-12-17`
- Asterisk AMI credentials for call origination

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/asayoyaasa/asterisk-translation-server.git
cd asterisk-translation-server
```

### 2. Create Python virtual environment

```bash
python3 -m venv /opt/translation-server/venv
source /opt/translation-server/venv/bin/activate
pip install websockets soxr "numpy==1.26.4"
```

### 3. Copy files

```bash
cp server.py languages.py /opt/translation-server/
```

### 4. Create environment file

```bash
cat > /etc/translation-server.env << 'EOF'
OPENAI_API_KEY=sk-your-key-here
PYTHONUNBUFFERED=1
EOF
chmod 600 /etc/translation-server.env
```

### 5. Install systemd service

Create `/etc/systemd/system/translation-server.service`:

```ini
[Unit]
Description=OpenAI Realtime Translation Server
After=network.target asterisk.service

[Service]
Type=simple
User=root
EnvironmentFile=/etc/translation-server.env
ExecStart=/opt/translation-server/venv/bin/python3 /opt/translation-server/server.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable translation-server
systemctl start translation-server
```

---

## Asterisk Configuration

### extensions.conf

```
[from-voipnow]
exten => _X.,1,NoOp(Inbound call to ${EXTEN} from ${CALLERID(num)})
 same => n,Set(CALLER_UUID=${SHELL(uuidgen)})
 same => n,Set(CALLEE_UUID=${SHELL(uuidgen)})
 same => n,System(echo "${CALLER_UUID} ${CALLEE_UUID} ${EXTEN} ${CALLERID(num)}" > /tmp/call_${CALLER_UUID}.txt)
 same => n,Answer()
 same => n,AudioSocket(${CALLER_UUID},127.0.0.1:5001)
 same => n,Hangup()

[callee-audiosocket]
exten => s,1,NoOp(Outbound callee leg)
 same => n,Answer()
 same => n,AudioSocket(${CALLEE_UUID},127.0.0.1:5001)
 same => n,Hangup()
```

### pjsip.conf (DIDLogic outbound trunk)

```ini
[didlogic-outbound]
type=endpoint
transport=transport-udp
context=callee-audiosocket
disallow=all
allow=ulaw
outbound_auth=didlogic-auth
aors=didlogic-aor

[didlogic-auth]
type=auth
auth_type=userpass
username=YOUR_DIDLOGIC_USERNAME
password=YOUR_DIDLOGIC_PASSWORD

[didlogic-aor]
type=aor
contact=sip:sip.didlogic.com
```

### manager.conf (AMI access)

```ini
[translation]
secret=TrServer2024!
deny=0.0.0.0/0.0.0.0
permit=127.0.0.1/255.255.255.0
read=all
write=originate
```

---

## Language Detection

Language is detected from the destination number's country calling code using `languages.py`. Covers 100+ countries.

| Prefix | Language | Prefix | Language | Prefix | Language |
|---|---|---|---|---|---|
| `62` | Indonesian | `1` | English | `44` | English |
| `86` | Chinese | `81` | Japanese | `82` | Korean |
| `55` | Portuguese | `33` | French | `49` | German |
| `34` | Spanish | `39` | Italian | `7` | Russian |
| `66` | Thai | `84` | Vietnamese | `60` | Malay |
| `91` | Hindi | `92` | Urdu | `90` | Turkish |
| `98` | Persian | `20` | Arabic | `27` | Afrikaans |

Full list in `languages.py`. To add a language, add an entry to `COUNTRY_LANGUAGES`.

---

## Configuration Reference

### Server constants (top of `server.py`)

| Constant | Default | Description |
|---|---|---|
| `HOST` | `127.0.0.1` | AudioSocket listener address |
| `PORT` | `5001` | AudioSocket listener port |
| `AMI_HOST` | `127.0.0.1` | Asterisk AMI host |
| `AMI_PORT` | `5038` | Asterisk AMI port |
| `AMI_USER` | `translation` | AMI username |
| `AMI_SECRET` | `TrServer2024!` | AMI password — **change this** |
| `OPENAI_MODEL` | `gpt-4o-realtime-preview-2024-12-17` | OpenAI model |

### Chunker constants (inside `one_way_bridge`)

| Constant | Default | Description |
|---|---|---|
| `CHUNK_SECONDS` | `6` | Max seconds before force-committing a chunk. Lower = more live, higher = fewer cuts. Range 3–10. |
| `CLAUSE_CHARS` | `30` | Minimum partial transcript length before clause trigger fires |

### Cooldown (inside `delayed_clear` in `pipe_out`)

600ms suppression after each translation plays. Prevents translated audio playing through the phone speaker from being picked up as input and re-translated. Increase to 1000–1500ms on noisy international lines if echo hallucinations occur.

### VAD settings

`semantic_vad` with `eagerness: high` — commits when the sentence is linguistically complete rather than after a fixed silence window. `create_response: False` disables automatic response generation so the chunker has full control over when to translate.

### Voice

Currently set to `verse`. Valid OpenAI Realtime API voices: `ash`, `ballad`, `coral`, `sage`, `verse`. Note: `alloy` is not a valid Realtime API voice (it exists for other OpenAI TTS endpoints only).

---

## Deployment

### Deploy updated server.py

```bash
scp server.py root@YOUR_SERVER:/opt/translation-server/server.py
ssh root@YOUR_SERVER systemctl restart translation-server
```

### View live logs

```bash
ssh root@YOUR_SERVER tail -f /var/log/translation-server.log
```

---

## Log Reference

### Normal call flow

```
New connection from ('127.0.0.1', XXXXX)
UUID: <caller-uuid>
Role: caller | Dest: 6281291960446 | Lang: Indonesian | CID: 61283180213
Caller connected - playing ringback, originating to 6281291960446
AMI originate: Response: Success
Pre-warming OpenAI sessions during ringback...
[prewarm English→Indonesian] session ready
[prewarm Indonesian→English] session ready
New connection from ('127.0.0.1', XXXXX)
UUID: <callee-uuid>
Role: callee | Dest: 6281291960446 | Lang: Indonesian | CID: 61283180213
Callee connected!
Both legs ready - starting bridge (callee arrived second)
=== BRIDGE ACTIVE EN <-> Indonesian ===
Using pre-warmed session for caller→callee
Using pre-warmed session for callee→caller
```

### Translation in progress

```
[caller→callee] speech detected
[caller→callee] chunk committed (clause, 87 frames)
[caller→callee] ORIGINAL : Hello, how are you doing today?
[caller→callee] TRANSLATED: Halo, apa kabar kamu hari ini?
[caller→callee] chunk done → ready for next (after 600ms cooldown)
```

```
[caller→callee] chunk committed (time, 300 frames)   ← 6s time trigger
[caller→callee] ORIGINAL : So what I want to explain is that our product...
[caller→callee] TRANSLATED: Jadi yang ingin saya jelaskan adalah produk kami...
```

### Barge-in

```
[caller→callee] barge-in — dropping 45 buffered frames
[caller→callee] barge-in — interrupting playback
[callee→caller] speech detected
```

### Post-filter blocking

```
[caller→callee] POST-FILTER [assistant phrase] blocked: "Sure, I'd be happy to help with that..."
[caller→callee] POST-FILTER [hallucinated numbers] blocked: "pendapatan sebesar $200 juta"
[caller→callee] POST-FILTER [length ratio] blocked: "Ya" → "Yes, that is certainly correct and I agree"
[caller→callee] POST-FILTER [empty transcript] blocked ghost audio
```

### Pre-warm failure (fallback)

```
No pre-warmed session for caller→callee — opening fresh connection
```

### Hangup

```
caller hung up
caller disconnected - hanging up callee
```

---

## Audio Pipeline

| Stage | Format | Sample Rate |
|---|---|---|
| Asterisk AudioSocket | Signed 16-bit PCM (slin16) | 8 kHz |
| Upsampled for OpenAI | Signed 16-bit PCM | 24 kHz |
| OpenAI output | Signed 16-bit PCM | 24 kHz |
| Downsampled for Asterisk | Signed 16-bit PCM (slin16) | 8 kHz |

Resampling uses [soxr](https://sourceforge.net/projects/soxr/) at VHQ quality. Audio is paced at 19ms intervals per 320-byte frame. Three silent padding frames (60ms) are appended after each translation chunk to prevent word cutoff artifacts on the telephony side.

AudioSocket frame format (Asterisk protocol):
```
[1 byte type] [2 bytes length big-endian] [N bytes payload]
type 0x01 = UUID
type 0x10 = audio
type 0xFF = hangup
```

---

## Known Limitations

- Uses OpenAI Realtime API Beta (`gpt-4o-realtime-preview-2024-12-17`). The GA model (`gpt-realtime`) was tested but does not follow translation instructions reliably.
- Proper nouns on noisy phone lines can be misheared by the transcription model — the anti-inference prompt rules reduce but do not eliminate this.
- VHQ resampling is CPU-intensive. For 50+ concurrent calls, switch to `quality="HQ"` in `resample_up()` and `resample_down()` for ~50% CPU reduction with minimal quality loss.
- The clause trigger requires punctuation in the partial transcript. Languages or speakers that don't use punctuation in speech will rely on the time trigger only (maximum 6-second lag).
- Both parties talking simultaneously at call start (before either translation has played) may cause brief echo due to the mutual suppression flags not being set yet.

---

## License

MIT

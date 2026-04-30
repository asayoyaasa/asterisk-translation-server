# Asterisk Translation Server

Real-time phone-call translation for Asterisk AudioSocket using the OpenAI Realtime API.

This service sits between two call legs:

- the caller, who is assumed to be speaking English
- the callee, whose language is inferred from the destination phone number

For each direction, the server listens to raw 8 kHz PCM from Asterisk, resamples it to 24 kHz for OpenAI, asks the model to translate only what was actually said, then resamples the generated audio back to 8 kHz and plays it to the other party.

---

## Current Status (2026-04-30)

Translation is operational. Two bugs fixed this session:

**Bug 1 — VoipNow wrong trunk (caller heard directly, no translation)**
Root cause: the 888 prefix route in VoipNow was pointing to the wrong SIP trunk, so calls bypassed Asterisk entirely and connected caller to callee directly.
Fix: corrected the trunk in VoipNow admin. No code changes needed.

**Bug 2 — Callee hears nothing for caller's first utterance (~33s gap)**
Root cause: OpenAI Realtime API occasionally delays `response.audio.done` by 30+ seconds after `response.created`. The `utterance_done` asyncio.Event stayed blocked for that entire window, so all subsequent caller speech was silently staged and never reached the callee.
Fix: added an 8-second watchdog task in `pipe_out()`. If no `response.audio.done` (or finalization) arrives within 8s of `response.created`, the watchdog calls `finalize_response()` and unblocks the queue. Log signature when it fires:
```
[caller→callee] response watchdog: no completion after 8s, forcing finalize
```
This fix is deployed. The service was restarted at Apr 30 12:33:58 and is currently active.

**Action needed**: make a test call to confirm both directions translate. If the watchdog fires in the log, the fix is working.

---

## How It Works

1. User dials `888` + destination number from VoipNow PBX
2. VoipNow strips the `888` prefix and forwards the call via SIP to our Asterisk at `188.116.36.83:5060`
3. Asterisk routes the call through `[from-voipnow]` dialplan context, writes call metadata to `/tmp/call_<uuid>.txt`, then hands audio to AudioSocket on `127.0.0.1:5001`
4. Translation server (`server.py`) receives the caller leg, plays ringback, and originates the callee leg via AMI to DIDLogic
5. Once both legs are connected, two independent OpenAI Realtime sessions run:
   - `caller→callee`: English → destination language
   - `callee→caller`: destination language → English

---

## VoipNow SIP Trunk Requirements

The `888` prefix route in VoipNow **must** be configured to forward to our Asterisk:

| Field | Value |
|-------|-------|
| Host | `188.116.36.83` |
| Port | `5060` |
| Protocol | UDP |
| Strip prefix | Yes — strip `888` before forwarding |

Our Asterisk identifies VoipNow by IP only (no auth). Both `89.38.54.13` and `89.38.54.17` are whitelisted in the PJSIP identify rules. If VoipNow's source IP changes, add it to `/etc/asterisk/pjsip.conf` under a new `[voipnow-identify]` section and run `asterisk -rx "module reload res_pjsip"`.

---

## Troubleshooting: Voice Heard Directly (No Translation)

Symptom: caller and callee hear each other without translation.

This means calls are **bypassing the translation server** entirely. Check in order:

### 1. Is Asterisk receiving SIP traffic from VoipNow?

```bash
# Brief tcpdump on SIP port — run this on the VPS (188.116.36.83)
timeout 10 tcpdump -i any -n port 5060

# Look for voipnow channels in Asterisk
grep 'voipnow' /var/log/asterisk/messages.log | tail -10
```

If no traffic appears, VoipNow is not forwarding the call to our Asterisk. Fix the 888 route in VoipNow admin — this was the root cause on 2026-04-30.

### 2. Is the translation server listening?

```bash
ss -ltnp | grep 5001
# Expect: LISTEN 0 100 127.0.0.1:5001
```

### 3. Is the translation server receiving connections?

```bash
tail -f /var/log/translation-server.log
# Should show "New connection from peer" when a call arrives
```

If Asterisk receives the call but translation-server shows nothing:

```bash
nc -z 127.0.0.1 5001 && echo 'port open' || echo 'port closed'
```

### 4. Did the callee originate?

```bash
# Check if AMI user exists and password matches
grep -A5 '\[translation\]' /etc/asterisk/manager.conf
# Compare with AMI_SECRET in server.py (hardcoded: "TrServer2024!")

# Check if DIDLogic is registered
asterisk -rx "pjsip show registrations"
```

### 5. Verify PJSIP identify rules

```bash
asterisk -rx "pjsip show identify"
# voipnow-identify should show match=89.38.54.13/32
# voipnow2-identify should show match=89.38.54.17/32
```

If VoipNow now sends from a different IP, add to `/etc/asterisk/pjsip.conf`:

```ini
[voipnow3-identify]
type=identify
endpoint=voipnow
match=<new_voipnow_ip>
```

Then: `asterisk -rx "module reload res_pjsip"`

---

## Troubleshooting: One Direction Silent (callee hears nothing)

Symptom: one party hears translations, the other hears silence.

This was the second bug fixed on 2026-04-30. The `utterance_done` event gets stuck blocked when OpenAI delays finalizing a response.

Check the log for:
```bash
grep -E "watchdog|forced finalize|response watchdog" /var/log/translation-server.log
```

If the watchdog fires frequently (multiple times per call), OpenAI Realtime is consistently slow to finalize. The 8s threshold in `_response_watchdog()` (inside `pipe_out()` in `server.py`) can be lowered — but going below 4s risks cutting off legitimate long translations.

If the watchdog never fires but one direction is still silent:
1. Check `[METRICS]` lines — `played=0` means audio is not reaching the destination socket
2. Check `filtered=N` — if high, the post-filter is blocking translations (check `post_filter()` in `server.py`)
3. Check that both legs connected: log should show "Both legs ready - starting bridge"
4. Check that `one_way_bridge` did not crash: look for `[caller→callee] bridge error:` lines

---

## Repository Files

- `server.py` — runtime entrypoint; call setup, AMI origination, OpenAI sessions, audio bridging, filtering, cleanup, metrics
- `prompt.md` — system prompt template; tells model to translate literally and use `[SKIP]` for noise
- `languages.py` — maps E.164 country codes to language names for destination language selection

---

## Call Setup

Asterisk writes a file to `/tmp/call_<caller_uuid>.txt` before connecting to AudioSocket:

```
<CALLER_UUID> <CALLEE_UUID> <DEST_NUMBER> <REAL_CALLERID>
```

When a socket connects, `server.py` reads the UUID frame and looks up the file. If the UUID matches the caller UUID it's the caller leg; if it matches the callee UUID it's the callee leg.

Caller flow:
1. Connect → send silence → start ringback → originate callee via AMI

Callee flow:
1. Connect → stop ringback → bridge starts

---

## Bridge Model

`run_bridge()` launches four coroutines:

- `one_way_bridge()` caller→callee
- `one_way_bridge()` callee→caller
- keepalive loop for caller leg
- keepalive loop for callee leg

Each `one_way_bridge()` owns one OpenAI Realtime session.

---

## Audio Pipeline

1. Asterisk sends 8 kHz signed 16-bit PCM chunks (AudioSocket protocol)
2. `resample_up()` converts to 24 kHz for OpenAI
3. Audio appended into OpenAI input buffer
4. Local silence detection commits utterances (~320 ms trailing silence threshold)
5. OpenAI transcribes (Whisper) and translates (realtime model)
6. OpenAI returns translated audio at 24 kHz
7. `resample_down()` converts back to 8 kHz
8. Translated audio streamed to other call leg

---

## Translation Pipeline Details

Three tasks inside each `one_way_bridge()`:

- `collector()` — reads source audio; streams to OpenAI when no TTS playing; stages locally when TTS active
- `sender()` — replays staged utterances after current response finishes (serializes overlapping speech)
- `pipe_out()` — reads OpenAI events, plays translated audio, cleans up conversation items, handles errors

### Response lifecycle

```
response.created
  → watchdog armed (8s timeout)
  → collector switches to staging mode (utterance_done cleared)
response.audio.delta  (0..N chunks)
response.audio.done
  → finish_after_timeout() starts (2.5s)
response.audio_transcript.done
  → post_filter() decides whether to play or drop
  → finalize_response() called
    → watchdog cancelled
    → audio played to dst (if not filtered)
    → 550ms cooldown
    → utterance_done set (unblocks sender/collector)
```

If OpenAI skips `response.audio.done` or delays it >8s, the watchdog fires `finalize_response()` directly.

---

## Hallucination Controls

1. Stateless utterances — conversation items deleted after each response
2. Short-utterance guard — utterances under `MIN_AUDIO_CHUNKS` (8 chunks ~160 ms) dropped
3. Transcript-gated response — `response.create` only fires after `input_audio_transcription.completed`
4. Post-filtering — blocks empty output, `[SKIP]`, too-short translations, assistant phrases, near-duplicates
5. Lower temperature (0.6) — reduces improvisation

---

## Language Selection

`languages.py` maps the destination number prefix to a language name:

1. Strip leading `+`
2. Try 3-digit prefix → 2-digit → 1-digit
3. Default: English

Examples: `62...` → Indonesian, `84...` → Vietnamese, `81...` → Japanese, `1...` → English

---

## OpenAI Configuration

| Setting | Value |
|---------|-------|
| Model | `gpt-4o-realtime-preview-2024-12-17` (env `OPENAI_MODEL` overrides) |
| WS endpoint | `wss://api.openai.com/v1/realtime` |
| Transcription | `whisper-1` |
| Voice | `marin` |
| Temperature | `0.6` |

`OPENAI_API_KEY` is loaded from `/etc/translation-server.env`.
`OPENAI_MODEL` can be overridden in `/etc/translation-server.env` — the server reads it with a fallback to the hardcoded date-specific model.

---

## Service Configuration

Systemd unit: `translation-server.service`

```
WorkingDirectory: /opt/translation-server
ExecStart: /opt/translation-server/venv/bin/python /opt/translation-server/server.py
EnvironmentFile: /etc/translation-server.env
User: deploy
```

Restart/check:
```bash
systemctl restart translation-server
journalctl -u translation-server -f
```

Logs also written to `/var/log/translation-server.log`.

Metrics logged every 60 s: `played`, `filtered`, `dupes`. If `played=0` across multiple calls, translation audio is not reaching the callee.

---

## Requirements

### System
- Python 3.10+
- Asterisk with AudioSocket (`app_audiosocket.so`, `res_audiosocket.so`)
- Asterisk AMI enabled with `[translation]` user
- Network access to `wss://api.openai.com`

### Python packages
```
websockets
soxr
numpy
```

---

## Asterisk Configuration

### pjsip.conf — VoipNow endpoints

```ini
[voipnow-auth]
type=auth
auth_type=userpass
username=asterisk-bridge
password=Bridge2024Secure!

[voipnow-aor]
type=aor
contact=sip:89.38.54.13:5060

[voipnow]
type=endpoint
transport=transport-udp
context=from-voipnow
disallow=all
allow=ulaw
allow=alaw
direct_media=no
aors=voipnow-aor

[voipnow-identify]
type=identify
endpoint=voipnow
match=89.38.54.13
```

### extensions.conf — from-voipnow context

```
[from-voipnow]
exten => _X.,1,NoOp(=== Translation Call Started ===)
 same => n,Set(DEST_NUMBER=${EXTEN})
 same => n,Set(CALLER_UUID=${SHELL(uuidgen | tr -d '\n')})
 same => n,Set(CALLEE_UUID=${SHELL(uuidgen | tr -d '\n')})
 same => n,Set(REAL_CALLERID=${CALLERID(num)})
 same => n,Progress()
 same => n,Answer()
 same => n,System(echo "${CALLER_UUID} ${CALLEE_UUID} ${DEST_NUMBER} ${REAL_CALLERID}" > /tmp/call_${CALLER_UUID}.txt)
 same => n,AudioSocket(${CALLER_UUID},127.0.0.1:5001)
 same => n,Hangup()

[callee-audiosocket]
exten => s,1,Answer()
 same => n,AudioSocket(${CALLEE_UUID},127.0.0.1:5001)
 same => n,Hangup()
```

Note: VoipNow strips the `888` prefix before forwarding to Asterisk, so `${EXTEN}` in the dialplan receives the bare destination number (no `888` prefix). Do not add prefix-stripping here.

### manager.conf — AMI user

```ini
[translation]
secret=TrServer2024!
deny=0.0.0.0/0.0.0.0
permit=127.0.0.1/255.255.255.255
read=all
write=all
```

---

## Known Issues

- Caller source language is fixed to English; callee language auto-detected from destination prefix
- `AMI_SECRET` hardcoded in `server.py` as `"TrServer2024!"` — should be moved to env var
- `OPENAI_MODEL` default is a date-specific model ID — if the model is deprecated by OpenAI, add `OPENAI_MODEL=gpt-4o-realtime-preview` to `/etc/translation-server.env` as an override
- Response watchdog fires at 8s; if OpenAI is consistently slow to finalize (seen in logs), consider lowering to 5s — but test first to ensure long translations still complete

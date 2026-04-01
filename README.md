# Asterisk Translation Server

Real-time phone-call translation for Asterisk AudioSocket using the OpenAI Realtime API.

This service sits between two call legs:

- the caller, who is assumed to be speaking English
- the callee, whose language is inferred from the destination phone number

For each direction, the server listens to raw 8 kHz PCM from Asterisk, resamples it to 24 kHz for OpenAI, asks the model to translate only what was actually said, then resamples the generated audio back to 8 kHz and plays it to the other party.

The current implementation is optimized for live calls, low latency, and damage control when the model or ASR gets confused. It is not a chatbot. It is a narrow translation bridge.

## What This Project Does

At a high level, the server:

1. Accepts AudioSocket connections from Asterisk on `127.0.0.1:5001`
2. Matches the incoming UUID to call metadata stored in `/tmp/call_<caller_uuid>.txt`
3. Determines whether the connection is the caller leg or callee leg
4. Starts ringback for the caller while originating the outbound call over AMI
5. Once both legs are present, starts two independent translation pipelines:
   - `caller -> callee` translates English into the destination language
   - `callee -> caller` translates the destination language back into English
6. Streams translated audio back into the opposite leg in near real time

## Repository Files

- `server.py`
  The only supported runtime entrypoint. Handles call setup, AMI origination, OpenAI sessions, audio bridging, filtering, cleanup, and metrics.
- `prompt.md`
  The system prompt template used for translation. It tells the model to translate literally, treat each utterance independently, and output `[SKIP]` instead of inventing speech.
- `languages.py`
  Maps E.164 country codes to a spoken language name. This is how the destination language is chosen.

## Runtime Architecture

## Call Setup

The server expects Asterisk to create a temporary call record in `/tmp` that contains:

- caller UUID
- callee UUID
- destination number
- caller ID

When a socket connects, `server.py` reads the first AudioSocket frame, extracts the UUID, and looks up the call metadata. If the UUID matches the caller UUID from the file, that leg is treated as the caller. If it matches the callee UUID, that leg is treated as the callee.

Caller flow:

1. Caller connects
2. The server sends silence immediately to keep the socket healthy
3. Ringback is played locally
4. The server originates the callee leg through Asterisk AMI

Callee flow:

1. Callee connects
2. The server stops ringback
3. The bridge starts as soon as both legs are alive

## Bridge Model

`run_bridge()` launches four coroutines together:

- one `one_way_bridge()` for `caller -> callee`
- one `one_way_bridge()` for `callee -> caller`
- one keepalive loop for the caller leg
- one keepalive loop for the callee leg

Each `one_way_bridge()` owns one OpenAI realtime session and one direction of translation.

## Audio Pipeline

The AudioSocket side uses 8 kHz signed 16-bit PCM. The OpenAI Realtime API expects 24 kHz PCM. The pipeline is:

1. Asterisk sends 8 kHz audio frames
2. `resample_up()` converts them to 24 kHz
3. Audio is appended into the OpenAI input buffer
4. A commit is triggered when speech plus trailing silence indicates a complete utterance
5. The model transcribes and translates the utterance
6. OpenAI returns translated audio at 24 kHz
7. `resample_down()` converts it back to 8 kHz
8. The translated audio is streamed into the other call leg

## Translation Pipeline Details

The bridge is designed around three internal tasks inside `one_way_bridge()`:

- `collector()`
  Reads source audio from the queue. When no TTS is playing, it streams audio directly to OpenAI. When translated audio is currently being played, it stages incoming speech locally and segments it by silence.
- `sender()`
  Replays staged utterances after the current response has finished, so overlapping speech is serialized instead of dropped.
- `pipe_out()`
  Reads OpenAI events, logs transcripts, plays translated audio, deletes conversation items, handles error recovery, and resets state when a response ends.

This design allows the speaker to continue talking while the other side is still hearing TTS. New utterances are queued and replayed in order.

## Prompting Strategy

The prompt in `prompt.md` is strict on purpose. The model is told:

- output only translated text or `[SKIP]`
- never answer as an assistant
- never add greetings, politeness, or explanations that were not spoken
- preserve names and register
- translate partial utterances literally instead of completing them
- treat each utterance independently
- never use earlier call context to invent a plausible sentence

This prompt is loaded at startup, cached per language pair, and sent as the OpenAI session instructions.

## Hallucination Controls

The current production path uses several defenses together.

### 1. Stateless utterance handling

After each response, the bridge deletes tracked OpenAI conversation items so prior turns do not keep accumulating in model memory.

### 2. Short-utterance guard

Queued utterances shorter than `MIN_AUDIO_CHUNKS` are dropped before replay. This prevents tiny fragments from being committed as if they were complete speech.

### 3. Transcript-gated response creation

Before `response.create` is sent, the bridge waits for the committed input item and then for that specific item's `conversation.item.input_audio_transcription.completed` event. If no usable transcript appears, that utterance is dropped instead of asking the model to improvise.

### 4. Post-filtering

`post_filter()` blocks output when the translated transcript looks suspicious. It filters:

- empty output
- `[SKIP]`
- translations that are far too short for the source
- canned assistant phrases
- known hallucination phrases such as "no translation"
- near-duplicate responses

### 5. Lower temperature

The realtime session is configured with a lower temperature to reduce improvisation and keep output closer to literal translation.

## Response-Lifecycle Protection

One of the main failure modes in realtime translation is overlapping `response.create` calls. If the next response is created before the previous one fully finishes, OpenAI can return:

- `conversation_already_has_active_response`

The current code reduces that risk by:

- serializing `response.create` behind a lock
- tracking `response_active` with an `asyncio.Event`
- only creating a new response after the previous one has been marked complete
- treating commit-empty and already-active-response errors as recoverable conditions

This is especially important during fast turn-taking or when the caller speaks again while TTS is still playing.

## Echo and Barge-In Handling

The server keeps per-direction `speaking_flag` state.

- If the peer is currently playing translated audio and speech is detected, that may be echo leakage, so the bridge can cancel the active response.
- If the local side speaks while its own TTS is playing, the new audio is buffered instead of immediately cancelling that side's response.

This behavior is a compromise between responsiveness and stability. It tries to avoid echo loops without throwing away legitimate follow-up speech.

## Language Selection

`languages.py` maps destination phone prefixes to language names. The logic:

1. strips the leading `+`
2. tries a 3-digit prefix
3. then a 2-digit prefix
4. then a 1-digit prefix
5. falls back to English

Examples:

- `62...` -> Indonesian
- `84...` -> Vietnamese
- `52...` -> Spanish
- `1...` -> English

The selected language is used in two places:

- the language-pair prompt
- the Whisper transcription hint sent to OpenAI

## OpenAI Configuration

The current server uses:

- model: `gpt-4o-realtime-preview-2024-12-17`
- realtime websocket endpoint: `wss://api.openai.com/v1/realtime`
- input transcription model: `whisper-1`
- voice: `marin`

Session settings are configured in `server.py` during `session.update`.

## Requirements

### System

- Linux server with Python 3.10+
- Asterisk with AudioSocket enabled
- Asterisk AMI enabled
- network access to OpenAI realtime websocket APIs

### Python packages

- `websockets`
- `soxr`
- `numpy`

Install example:

```bash
python3 -m venv venv
source venv/bin/activate
pip install websockets soxr numpy
```

## Configuration

The server currently reads:

- `OPENAI_API_KEY` from the environment

Hard-coded values in `server.py`:

- `HOST`, `PORT`
- `AMI_HOST`, `AMI_PORT`
- `AMI_USER`, `AMI_SECRET`
- `OPENAI_MODEL`

If you want this to be easier to deploy across environments, the next cleanup would be to move AMI credentials, bind address, and model selection into environment variables or a config file.

## Running the Server

Local run:

```bash
python3 server.py
```

The server listens on:

```text
127.0.0.1:5001
```

Typical production deployment uses a `systemd` service such as:

```bash
systemctl restart translation-server
journalctl -u translation-server -f
```

## Logging

Logs are written to:

- stdout
- `/var/log/translation-server.log`

Typical log events include:

- new connections and UUID detection
- caller or callee role assignment
- AMI originate attempts
- prompt cache creation
- speech detection
- local silence commits
- original transcripts
- translated transcripts
- filtered outputs
- queue replay behavior
- cleanup and periodic metrics

Metrics currently logged every 60 seconds:

- `played`
- `filtered`
- `dupes`

## Current Production Behavior

The active implementation in `server.py` assumes:

- caller language is English
- callee language is determined from the destination number
- both directions use separate realtime sessions
- translation should be literal, not conversational
- damaged or missing ASR should prefer dropping an utterance over inventing a sentence

## Known Limitations

- The caller source language is currently fixed to English.
- Some recovery logic still depends on event ordering from the Realtime API.
- Post-filtering happens after transcript events, so it reduces bad output a lot but cannot guarantee zero audible hallucinations in every edge case.
- Credentials and several deployment settings are still hard-coded in `server.py`.

## Recommended Next Improvements

If you keep evolving this service, the highest-value next steps are:

1. Move configuration into environment variables
2. Add a text-first translation mode for ultra-high-risk utterances
3. Add structured integration tests around replay, duplicate filtering, and active-response races
4. Split `server.py` into modules for call control, OpenAI bridge logic, filtering, and configuration
5. Add a deployment section with full Asterisk dialplan and service unit examples

## Summary

This repository is a realtime bilingual phone bridge, not a general assistant. The important parts of the design are:

- low-latency audio conversion between Asterisk and OpenAI
- strict prompting to keep translations literal
- queueing and replay so speech is not lost during TTS
- guards against empty commits and overlapping responses
- cleanup of prior conversation state to reduce hallucinated carry-over

If you are debugging weird translations, start with these three files in order:

1. `server.py`
2. `prompt.md`
3. `languages.py`

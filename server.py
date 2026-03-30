import asyncio, websockets, json, os, base64, logging, glob, struct, uuid as uuid_mod
import soxr
import numpy as np

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("/var/log/translation-server.log")])
log = logging.getLogger(__name__)

from languages import get_language

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
HOST, PORT = "127.0.0.1", 5001
AMI_HOST, AMI_PORT = "127.0.0.1", 5038
AMI_USER, AMI_SECRET = "translation", "TrServer2024!"

# ── Beta API (proven working) ──────────────────────────────────────────────────
OPENAI_MODEL  = "gpt-4o-realtime-preview-2024-12-17"
OPENAI_WS_URL = f"wss://api.openai.com/v1/realtime?model={OPENAI_MODEL}"
# ──────────────────────────────────────────────────────────────────────────────

MSG_UUID, MSG_AUDIO, MSG_HANGUP = 0x01, 0x10, 0xff
SILENCE_PAYLOAD = bytes(320)
SILENCE = struct.pack(">BH", MSG_AUDIO, 320) + SILENCE_PAYLOAD
HANGUP_FRAME = struct.pack(">BH", MSG_HANGUP, 0)

calls = {}

def build_frame(audio): return struct.pack(">BH", MSG_AUDIO, len(audio)) + audio
def parse_uuid(p): return str(uuid_mod.UUID(bytes=p)) if len(p)==16 else p.decode("utf-8","ignore").strip("\x00").strip()

def resample_up(audio):
    """8kHz slin16 → 24kHz for OpenAI"""
    samples = np.frombuffer(audio, dtype=np.int16).astype(np.float32)
    return soxr.resample(samples, 8000, 24000, quality="VHQ").astype(np.int16).tobytes()

def resample_down(audio):
    """24kHz from OpenAI → 8kHz slin16 for Asterisk"""
    samples = np.frombuffer(audio, dtype=np.int16).astype(np.float32)
    return soxr.resample(samples, 24000, 8000, quality="VHQ").astype(np.int16).tobytes()

def find_call(uuid):
    try:
        with open(f"/tmp/call_{uuid}.txt") as f:
            parts = f.read().strip().split()
            if len(parts) >= 3:
                return parts[0], parts[1], parts[2], parts[3] if len(parts)>=4 else "", "caller"
    except: pass
    for fp in glob.glob("/tmp/call_*.txt"):
        try:
            with open(fp) as f:
                parts = f.read().strip().split()
                if len(parts) >= 3 and parts[1] == uuid:
                    return parts[0], parts[1], parts[2], parts[3] if len(parts)>=4 else "", "callee"
        except: pass
    return None, None, None, None, None

async def ami_originate(callee_uuid, dest, cid):
    log.info(f"AMI originating to {dest} | callee: {callee_uuid} | CID: {cid}")
    try:
        r, w = await asyncio.open_connection(AMI_HOST, AMI_PORT)
        await r.readline()
        w.write(f"Action: Login\r\nUsername: {AMI_USER}\r\nSecret: {AMI_SECRET}\r\n\r\n".encode())
        await w.drain()
        resp = b""
        while b"\r\n\r\n" not in resp: resp += await asyncio.wait_for(r.read(4096), timeout=5)
        if b"Authentication accepted" not in resp:
            log.error("AMI login failed"); w.close(); return
        w.write((f"Action: Originate\r\nChannel: PJSIP/{dest}@didlogic-outbound\r\n"
                 f"Context: callee-audiosocket\r\nExten: s\r\nPriority: 1\r\nTimeout: 60000\r\n"
                 f"CallerID: {cid}\r\nVariable: CALLEE_UUID={callee_uuid},REAL_CALLERID={cid}\r\n"
                 f"Async: yes\r\n\r\n").encode())
        await w.drain()
        resp = b""
        while b"\r\n\r\n" not in resp: resp += await asyncio.wait_for(r.read(4096), timeout=5)
        log.info(f"AMI originate: {resp.decode().strip()[:80]}")
        w.write(b"Action: Logoff\r\n\r\n"); await w.drain(); w.close()
    except Exception as e: log.error(f"AMI error: {e}")

def generate_ringback():
    """Generate 1 second of ringback tone (425 Hz) as slin16 8kHz"""
    import math
    samples = [int(16000 * math.sin(2 * math.pi * 425 * i / 8000)) for i in range(8000)]
    return struct.pack(f"<{len(samples)}h", *samples)

RINGBACK_1S = generate_ringback()
RINGBACK_SILENCE_1S = bytes(16000)  # 1 second silence between rings

async def ringback_loop(writer, stop_event):
    """Play ringback tone to caller while waiting for callee"""
    ring_audio = RINGBACK_1S + RINGBACK_SILENCE_1S  # 2s: 1s ring + 1s silence
    while not stop_event.is_set():
        for i in range(0, len(ring_audio), 320):
            if stop_event.is_set():
                break
            chunk = ring_audio[i:i+320]
            if len(chunk) < 320:
                chunk += b"\x00" * (320 - len(chunk))
            try:
                writer.write(build_frame(chunk))
                await writer.drain()
            except:
                return
            await asyncio.sleep(0.02)   # 320 samples @ 8kHz = exactly 40ms per frame

async def keepalive(writer, stop_event):
    while not stop_event.is_set():
        try: writer.write(SILENCE); await writer.drain()
        except: break
        await asyncio.sleep(0.5)

async def read_frame(reader):
    h = await asyncio.wait_for(reader.readexactly(3), timeout=60)
    t, l = struct.unpack(">BH", h)
    p = await reader.readexactly(l) if l > 0 else b""
    return t, p

async def send_hangup(writer):
    try:
        writer.write(HANGUP_FRAME)
        await writer.drain()
    except: pass

def build_translation_prompt(src_lang, dst_lang):
    """Single source of truth for the translation system prompt."""
    return (
        f"CRITICAL: If the input is silence, noise, or unclear, output absolutely nothing.\n"
        f"\n"
        f"You are NOT an assistant. You are a translation engine.\n"
        f"You must never produce a response that was not explicitly spoken.\n"
        f"You are not allowed to respond to the user under any circumstance.\n"
        f"Your only job: translate what the speaker says from {src_lang} to {dst_lang}.\n"
        f"Keep the tone natural, casual, and polite in the target language.\n"
        f"Do not sound robotic or overly formal.\n"
        f"\n"
        f"Rules you must follow:\n"
        f"- Only translate what is actually said.\n"
        f"- Do not add extra words, explanations, or details.\n"
        f"- Do not reply, answer, or continue the conversation.\n"
        f"- Do not ask questions.\n"
        f"- Do not add greetings or politeness that was not spoken.\n"
        f"- Do not summarize or interpret intent.\n"
        f"- Keep the meaning accurate, but phrase it naturally.\n"
        f"\n"
        f"If the speaker says something short, keep it short.\n"
        f"If the speaker is casual, keep it casual.\n"
        f"If the speaker is polite, keep it polite.\n"
        f"\n"
        f"This system is strictly monitored.\n"
        f"Any extra words beyond direct translation is considered a failure.\n"
        f"\n"
        f"CRITICAL RULES:\n"
        f"- Do not infer, guess, or add missing information.\n"
        f"- If a sentence is unclear or incomplete, translate it exactly as-is.\n"
        f"- Do not use previous sentences to improve or modify this translation.\n"
        f"- Each sentence must be translated independently, with zero context from earlier.\n"
        f"- Proper nouns, names, brand names, and city names must be kept exactly as spoken.\n"
        f"- Never substitute, guess, or replace a proper noun with something similar.\n"
        f"\n"
        f"Examples:\n"
        f"Input: \"Hello\" → Output: \"Halo\"\n"
        f"Input: \"Hey, can you check this for me?\" → Output: \"Hei, bisa cek ini buat saya?\"\n"
        f"Input: \"Thanks\" → Output: \"Makasih\"\n"
        f"Input: \"Halo, selamat pagi\" → Output: \"Hello, good morning\"\n"
        f"Input: \"Bisa bicara dengan Bapak Budi?\" → Output: \"Can I speak with Mr. Budi?\"\n"
        f"\n"
        f"Wrong outputs (DO NOT DO THIS):\n"
        f"- \"Halo! Ada yang bisa saya bantu?\"\n"
        f"- \"Tentu, saya akan membantu Anda.\"\n"
        f"- \"Sure, I can help you with that!\"\n"
        f"- Anything that adds new meaning or continues the conversation."
    )

async def prewarm_openai_session(src_lang, dst_lang, src_lang_code, ready_event, ws_holder, stop_event):
    """
    Opens the OpenAI WebSocket and sends session.update during ringback so the
    session is fully initialised before the callee picks up.
    Sets ready_event when the session.created ack arrives.
    ws_holder[0] will contain the live websocket for one_way_bridge to reuse.
    """
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "OpenAI-Beta": "realtime=v1"}
    ws = None
    try:
        # Use explicit connect (not async with) so the WS stays open after this
        # function would otherwise exit — the bridge task holds the reference
        # and the prewarm task keeps running until bridge cancels it.
        ws = await websockets.connect(OPENAI_WS_URL, additional_headers=headers)
        ws_holder[0] = ws
        await ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "modalities": ["audio", "text"],
                "instructions": build_translation_prompt(src_lang, dst_lang),
                "voice": "verse",
                "input_audio_format": "pcm16",
                "output_audio_format": "pcm16",
                "input_audio_transcription": {"model": "gpt-4o-mini-transcribe", "language": src_lang_code},
                # create_response:false — VAD commits the turn but does NOT auto-fire
                # a response. We manually send response.create with conversation:none
                # on input_audio_buffer.committed so EVERY utterance is stateless.
                "turn_detection": {
                    "type": "semantic_vad",
                    "eagerness": "high",
                    "create_response": False,
                },
            }
        }))
        # Wait for session.created then signal ready
        async for msg in ws:
            if stop_event.is_set():
                return  # bridge has taken over — exit quietly
            try:
                event = json.loads(msg)
                if event.get("type") == "session.created":
                    log.info(f"[prewarm {src_lang}→{dst_lang}] session ready")
                    ready_event.set()
                elif event.get("type") == "error":
                    log.error(f"[prewarm] error: {event}")
                    return
                if ready_event.is_set() and stop_event.is_set():
                    return
            except Exception as e:
                log.error(f"[prewarm] {e}")
                return
    except asyncio.CancelledError:
        pass  # bridge cancelled us cleanly — WS ownership transferred
    except Exception as e:
        log.error(f"[prewarm {src_lang}→{dst_lang}] connect failed: {e}")
        ready_event.set()  # unblock bridge — ws_holder[0] stays None, bridge opens fresh session
    finally:
        # Only close if the bridge never took ownership (ws_holder[0] is None means
        # prewarm failed; if non-None the bridge owns the WS and will close it)
        if ws and ws_holder[0] is None:
            try: await ws.close()
            except: pass

# ISO 639-1 codes for Whisper language hint — covers all languages in languages.py
LANG_CODES = {
    "afrikaans": "af",    "albanian": "sq",     "amharic": "am",
    "arabic": "ar",       "bosnian": "bs",       "bulgarian": "bg",
    "catalan": "ca",      "chinese": "zh",       "mandarin": "zh",
    "croatian": "hr",     "czech": "cs",         "danish": "da",
    "dhivehi": "dv",      "dutch": "nl",         "dzongkha": "dz",
    "english": "en",      "estonian": "et",      "fijian": "fj",
    "filipino": "tl",     "finnish": "fi",       "french": "fr",
    "georgian": "ka",     "german": "de",        "greek": "el",
    "hebrew": "he",       "hindi": "hi",         "hungarian": "hu",
    "icelandic": "is",    "indonesian": "id",    "italian": "it",
    "japanese": "ja",     "khmer": "km",         "kinyarwanda": "rw",
    "korean": "ko",       "kyrgyz": "ky",        "lao": "lo",
    "latvian": "lv",      "lithuanian": "lt",    "luxembourgish": "lb",
    "malagasy": "mg",     "malay": "ms",         "maltese": "mt",
    "mongolian": "mn",    "montenegrin": "sr",   "nepali": "ne",
    "norwegian": "no",    "persian": "fa",       "polish": "pl",
    "portuguese": "pt",   "romanian": "ro",      "russian": "ru",
    "serbian": "sr",      "sesotho": "st",       "sinhala": "si",
    "slovak": "sk",       "slovenian": "sl",     "somali": "so",
    "spanish": "es",      "swahili": "sw",       "swati": "ss",
    "swedish": "sv",      "tajik": "tg",         "thai": "th",
    "tigrinya": "ti",     "turkish": "tr",       "turkmen": "tk",
    "ukrainian": "uk",    "urdu": "ur",          "uzbek": "uz",
    "vietnamese": "vi",   "welsh": "cy",
}

async def one_way_bridge(label, src_queue, dst_writer, dst_lock, src_alive_fn, dst_alive_fn, src_lang, dst_lang, speaking_flag, peer_speaking_flag=None, prewarmed_ws=None):
    """
    Simultaneous-interpreter bridge.

    chunker() runs two triggers in parallel:
      - Time trigger:   every CHUNK_SECONDS of continuous speech, force-commit
      - Clause trigger: commit immediately on clause boundary in partial transcript
      - Barge-in:       when peer starts speaking, stop + drain the queue

    pipe_out() plays OpenAI audio directly, interruptible frame-by-frame
    via abort_playback when peer starts speaking (barge-in).

    Result: speaker is at most CHUNK_SECONDS behind, never the full sentence length.
    """
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "OpenAI-Beta": "realtime=v1"}
    src_lang_code = LANG_CODES.get(src_lang.lower(), None)

    CHUNK_SECONDS = 6          # max seconds before force-committing a chunk
    CLAUSE_CHARS  = 30         # min chars in partial transcript before clause-split allowed

    # Clause boundary markers — language-agnostic punctuation + common connectives
    CLAUSE_MARKERS = {',', '.', '!', '?', ';', ':', '،', '。', '、', '！', '？'}

    # Signals pipe_out finished playing + cooldown for current chunk
    chunk_done = asyncio.Event()
    chunk_done.set()

    # Shared: partial transcript text from OpenAI delta events (written by pipe_out)
    partial_transcript = ['']

    # Timestamp of last ASR delta — used for temporal stability in clause trigger
    last_delta_time = [0.0]


    # Signals pipe_out to abort current playback (set on barge-in)
    abort_playback = asyncio.Event()

    # Set by chunker before manual commit so pipe_out skips its own
    # response.create on the committed event — prevents double-translation.
    manual_commit_pending = [False]

    async def _run_with_ws(ws):
        # Cache once — reused on every response.create throughout the call
        _prompt = build_translation_prompt(src_lang, dst_lang)
        _loop = asyncio.get_running_loop()

        if prewarmed_ws is None:
            await ws.send(json.dumps({
                "type": "session.update",
                "session": {
                    "modalities": ["audio", "text"],
                    "instructions": _prompt,
                    "voice": "verse",
                    "input_audio_format": "pcm16",
                    "output_audio_format": "pcm16",
                    "input_audio_transcription": {"model": "gpt-4o-mini-transcribe", "language": src_lang_code},
                    "turn_detection": {
                        "type": "semantic_vad",
                        "eagerness": "high",
                        "create_response": False,
                    },
                }
            }))
            log.info(f"[{label}] OpenAI ready ({src_lang} → {dst_lang})")
        else:
            log.info(f"[{label}] reusing pre-warmed OpenAI session ({src_lang} → {dst_lang})")
            try:
                await ws.send(json.dumps({"type": "input_audio_buffer.clear"}))
            except: pass
            await asyncio.sleep(0.075)

        # ── chunker: collects audio and commits chunks on time/clause/barge-in ──
        async def chunker():
            current_chunk = []         # raw PCM chunks for the current window
            chunk_start_time = None    # when we started collecting this chunk
            sending_live = False       # True when audio is flowing directly to OpenAI

            async def _commit_chunk(reason):
                nonlocal current_chunk, chunk_start_time, sending_live
                if not current_chunk:
                    return
                try:
                    if sending_live:
                        # Already streaming — just commit what OpenAI has buffered
                        await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                    else:
                        # Queued chunk — send + commit in one go
                        await ws.send(json.dumps({"type": "input_audio_buffer.clear"}))
                        for raw in current_chunk:
                            audio24 = resample_up(raw)
                            await ws.send(json.dumps({
                                "type": "input_audio_buffer.append",
                                "audio": base64.b64encode(audio24).decode()
                            }))
                        await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                    # Set BEFORE response.create in both paths so pipe_out skips
                    # its own response.create on the committed event
                    manual_commit_pending[0] = True
                    await ws.send(json.dumps({"type": "response.create", "response": {
                        "conversation": "none",
                        "instructions": _prompt
                    }}))
                    log.info(f"[{label}] chunk committed ({reason}, {len(current_chunk)} frames)")
                except Exception as e:
                    log.error(f"[{label}] commit error: {e}")
                current_chunk = []
                chunk_start_time = None
                partial_transcript[0] = ''
                sending_live = False

            while src_alive_fn():
                # ── barge-in check ────────────────────────────────────────
                peer_talking = peer_speaking_flag and peer_speaking_flag[0]
                if peer_talking:
                    if current_chunk or sending_live:
                        log.info(f"[{label}] barge-in — dropping {len(current_chunk)} buffered frames")
                        current_chunk = []
                        chunk_start_time = None
                        partial_transcript[0] = ''
                        sending_live = False  # must reset so next commit uses correct path
                        try:
                            await ws.send(json.dumps({"type": "input_audio_buffer.clear"}))
                        except: pass
                    # Signal pipe_out to stop current playback immediately
                    log.info(f"[{label}] barge-in — interrupting playback")
                    abort_playback.set()
                    # Cancel any in-progress OpenAI response immediately.
                    # Silently ignore response_cancel_not_active — it just means
                    # no response was running, which is fine.
                    try:
                        await ws.send(json.dumps({"type": "response.cancel"}))
                    except: pass  # response_cancel_not_active is expected here
                    await asyncio.sleep(0.1)
                    continue

                abort_playback.clear()

                # ── backlog guard ─────────────────────────────────────────
                # If the audio queue is dangerously full, the system is lagging.
                # Reset cleanly rather than produce a corrupted translation.
                if src_queue.qsize() > 120:
                    log.warning(f"[{label}] audio backlog too large ({src_queue.qsize()}) — resetting chunk")
                    current_chunk = []
                    chunk_start_time = None
                    partial_transcript[0] = ''
                    sending_live = False
                    try:
                        await ws.send(json.dumps({"type": "input_audio_buffer.clear"}))
                    except: pass
                    continue

                # ── read next audio frame ────────────────────────────────
                try:
                    audio = await asyncio.wait_for(src_queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    # No audio — check if time trigger should fire
                    if current_chunk and chunk_start_time:
                        elapsed = _loop.time() - chunk_start_time  # timeout path
                        if elapsed >= CHUNK_SECONDS:
                            await _commit_chunk("time")
                    continue
                except Exception as e:
                    log.error(f"[{label}] chunker read: {e}")
                    return

                # Cache loop time once per iteration — used by both triggers
                now = _loop.time()

                if not current_chunk:
                    chunk_start_time = now

                current_chunk.append(audio)

                # Stream live to OpenAI while collecting
                try:
                    audio24 = resample_up(audio)
                    await ws.send(json.dumps({
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(audio24).decode()
                    }))
                    sending_live = True
                except Exception as e:
                    log.error(f"[{label}] chunker stream: {e}")
                    return

                # ── time trigger ─────────────────────────────────────────
                if chunk_start_time:
                    elapsed = now - chunk_start_time
                    if elapsed >= CHUNK_SECONDS:
                        await _commit_chunk("time")
                        continue

                # ── clause trigger ────────────────────────────────────────
                pt = partial_transcript[0]
                quiet_ms = now - last_delta_time[0]
                # Require: enough chars + ends with marker + 4+ words +
                # 400ms of ASR quiet (ensures clause is actually complete,
                # not just a mid-sentence pause before more words arrive)
                if (len(pt) >= CLAUSE_CHARS
                        and pt and pt[-1] in CLAUSE_MARKERS
                        and pt.count(" ") >= 4
                        and last_delta_time[0] > 0
                        and quiet_ms >= 0.4):
                    await _commit_chunk("clause")

        # ── pipe_out: OpenAI events → audio to dst ───────────────────────
        async def pipe_out():
            import re as _re
            auto_reset_task = None
            current_response_blocked = False
            response_active = False
            last_original = ""

            def count_sentences(text):
                parts = _re.split(r'[.!?]+', text.strip())
                return len([p for p in parts if p.strip()])

            def length_ratio_ok(src, dst):
                if not src or not dst:
                    return True
                if len(src) < 5:
                    return len(dst) <= 20
                return len(dst) <= len(src) * 3

            def looks_low_confidence(text):
                t = text.strip().lower()
                if len(t) < 2:
                    return True
                if t in {"uh", "um", "hmm", "hm", "ah", "oh", "er"}:
                    return True
                if "..." in t:
                    return True
                return False

            def has_new_numbers(src, dst):
                import re as _re2
                src_nums = set(_re2.findall(r'\d+', src))
                dst_nums = set(_re2.findall(r'\d+', dst))
                return bool(dst_nums - src_nums)

            ASSISTANT_PHRASES = [
                "how can i help", "how may i help", "how can i assist",
                "how may i assist", "is there anything else", "anything else i can",
                "i'd be happy to", "i would be happy to", "i'm here to help",
                "i am here to help", "thank you for calling", "thank you for contacting",
                "have a great day", "have a nice day", "is there something i can",
                "let me know if you need", "don't hesitate to",
            ]

            async for msg in ws:
                try:
                    event = json.loads(msg)
                    etype = event.get("type", "")

                    if etype == "response.output_item.added":
                        current_response_blocked = False
                        response_active = True

                    elif etype == "input_audio_buffer.committed":
                        if manual_commit_pending[0]:
                            # chunker already sent response.create — skip to avoid duplicate
                            manual_commit_pending[0] = False
                        elif not (speaking_flag[0] or (peer_speaking_flag and peer_speaking_flag[0])):
                            # VAD auto-committed naturally — fire stateless response.create
                            try:
                                await ws.send(json.dumps({"type": "response.create", "response": {
                                    "conversation": "none",
                                    "instructions": _prompt
                                }}))
                            except Exception as e:
                                log.error(f"[{label}] response.create after commit failed: {e}")
                        else:
                            try:
                                await ws.send(json.dumps({"type": "input_audio_buffer.clear"}))
                            except: pass

                    elif etype == "input_audio_buffer.speech_started":
                        if speaking_flag[0] or (peer_speaking_flag and peer_speaking_flag[0]):
                            log.info(f"[{label}] speech detected during cooldown — cancelling echo response")
                            try:
                                if response_active:
                                    await ws.send(json.dumps({"type": "response.cancel"}))
                                await ws.send(json.dumps({"type": "input_audio_buffer.clear"}))
                            except: pass
                        else:
                            log.info(f"[{label}] speech detected")

                    elif etype == "conversation.item.input_audio_transcription.delta":
                        # Update shared partial transcript + timestamp for clause trigger
                        delta = event.get("delta", "")
                        partial_transcript[0] += delta
                        last_delta_time[0] = _loop.time()

                    elif etype == "conversation.item.input_audio_transcription.completed":
                        last_original = event.get("transcript", "").strip()
                        log.info(f"[{label}] ORIGINAL : {last_original}")
                        partial_transcript[0] = ''

                    elif etype == "response.audio_transcript.done":
                        translated_text = event.get("transcript", "").strip()
                        log.info(f"[{label}] TRANSLATED: {translated_text}")

                        if not translated_text:
                            log.warning(f"[{label}] POST-FILTER [empty transcript] blocked ghost audio")
                            current_response_blocked = True
                        elif looks_low_confidence(translated_text):
                            log.warning(f"[{label}] POST-FILTER [low confidence] blocked: {translated_text!r}")
                            current_response_blocked = True
                        else:
                            t_lower       = translated_text.lower()
                            phrase_hit    = any(p in t_lower for p in ASSISTANT_PHRASES)
                            orig_s        = max(count_sentences(last_original), 1)
                            out_s         = count_sentences(translated_text)
                            expansion_hit = out_s > orig_s + 1
                            ratio_hit     = not length_ratio_ok(last_original, translated_text)
                            number_hit    = has_new_numbers(last_original, translated_text)

                            if phrase_hit or expansion_hit or ratio_hit or number_hit:
                                reason = ("assistant phrase" if phrase_hit
                                          else "sentence expansion" if expansion_hit
                                          else "length ratio" if ratio_hit
                                          else "hallucinated numbers")
                                log.warning(f"[{label}] POST-FILTER [{reason}] blocked: {translated_text!r}")
                                current_response_blocked = True
                                if auto_reset_task and not auto_reset_task.done():
                                    auto_reset_task.cancel()
                                speaking_flag[0] = False
                                chunk_done.set()
                                try:
                                    if response_active:
                                        await ws.send(json.dumps({"type": "response.cancel"}))
                                    await ws.send(json.dumps({"type": "input_audio_buffer.clear"}))
                                except: pass

                    elif etype == "response.audio_transcript.delta":
                        pass

                    elif etype == "response.audio.delta":
                        if current_response_blocked:
                            continue
                        # Abort mid-playback if barge-in
                        if abort_playback.is_set():
                            log.info(f"[{label}] playback aborted (barge-in)")
                            current_response_blocked = True
                            try:
                                if response_active:
                                    await ws.send(json.dumps({"type": "response.cancel"}))
                                await ws.send(json.dumps({"type": "input_audio_buffer.clear"}))
                            except: pass
                            continue
                        chunk = base64.b64decode(event.get("delta", ""))
                        if chunk and dst_alive_fn():
                            if not speaking_flag[0]:
                                speaking_flag[0] = True
                                try:
                                    await ws.send(json.dumps({"type": "input_audio_buffer.clear"}))
                                except: pass

                            if auto_reset_task and not auto_reset_task.done():
                                auto_reset_task.cancel()

                            audio8 = resample_down(chunk)
                            async with dst_lock:
                                try:
                                    frames_sent = 0
                                    for i in range(0, len(audio8), 320):
                                        # Check barge-in on every frame
                                        if abort_playback.is_set():
                                            break
                                        fd = audio8[i:i+320]
                                        if len(fd) < 320:
                                            fd += b"\x00" * (320 - len(fd))
                                        dst_writer.write(build_frame(fd))
                                        await dst_writer.drain()
                                        await asyncio.sleep(0.019)
                                        frames_sent += 1
                                    # Only pad if we completed normally (not aborted)
                                    if not abort_playback.is_set():
                                        for _ in range(3):
                                            dst_writer.write(build_frame(b"\x00" * 320))
                                            await dst_writer.drain()
                                            await asyncio.sleep(0.019)
                                    log.debug(f"[{label}] sent {frames_sent} frames to dst")
                                except Exception as e:
                                    log.error(f"[{label}] audio write failed: {e}")

                            async def auto_reset(flag=speaking_flag):
                                await asyncio.sleep(1.2)
                                if flag[0]:
                                    log.warning(f"[{label}] auto-reset speaking_flag after 1.2s timeout")
                                    flag[0] = False
                                    chunk_done.set()
                            auto_reset_task = asyncio.create_task(auto_reset())

                    elif etype == "response.audio.done":
                        response_active = False
                        if auto_reset_task and not auto_reset_task.done():
                            auto_reset_task.cancel()
                        async def delayed_clear(flag=speaking_flag, lbl=label, _ws=ws):
                            try:
                                await _ws.send(json.dumps({"type": "input_audio_buffer.clear"}))
                            except: pass
                            await asyncio.sleep(0.6)
                            flag[0] = False
                            chunk_done.set()
                            log.info(f"[{lbl}] chunk done → ready for next (after 600ms cooldown)")
                        auto_reset_task = asyncio.create_task(delayed_clear())

                    elif etype == "error":
                        log.error(f"[{label}] OpenAI error: {event}")
                        chunk_done.set()

                except Exception as e:
                    log.error(f"[{label}] pipe_out: {e}")

        await asyncio.gather(chunker(), pipe_out())

    try:
        if prewarmed_ws is not None:
            await _run_with_ws(prewarmed_ws)
        else:
            headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "OpenAI-Beta": "realtime=v1"}
            async with websockets.connect(OPENAI_WS_URL, additional_headers=headers) as ws:
                await _run_with_ws(ws)
    except Exception as e:
        log.error(f"[{label}] bridge error: {e}")



async def run_bridge(caller_uuid):
    call = calls.get(caller_uuid)
    if not call: return
    ci = call["caller"]
    ce = call["callee"]
    if not ci or not ce: log.error("Bridge aborted - missing legs"); return

    lang = call["lang"]
    log.info(f"=== BRIDGE ACTIVE EN <-> {lang} ===")

    caller_lock = asyncio.Lock()
    callee_lock = asyncio.Lock()
    callee_speaking = [False]
    caller_speaking = [False]

    # Retrieve pre-warmed websockets if available (opened during ringback)
    # ws_holder[0] is set by prewarm_openai_session once the session is ready
    caller_ws = call.get("prewarm_caller_ws", [None])[0]
    callee_ws = call.get("prewarm_callee_ws", [None])[0]

    if caller_ws:
        log.info("Using pre-warmed session for caller→callee")
    else:
        log.info("No pre-warmed session for caller→callee — opening fresh connection")
    if callee_ws:
        log.info("Using pre-warmed session for callee→caller")
    else:
        log.info("No pre-warmed session for callee→caller — opening fresh connection")

    # Stop prewarm tasks — bridge takes over the websockets from here
    for task_key in ("prewarm_caller_task", "prewarm_callee_task"):
        t = call.get(task_key)
        if t and not t.done():
            t.cancel()

    async def keepalive_both():
        while ci.get("alive") or ce.get("alive"):
            if ci.get("alive"):
                async with caller_lock:
                    try: ci["writer"].write(SILENCE); await ci["writer"].drain()
                    except: pass
            if ce.get("alive"):
                async with callee_lock:
                    try: ce["writer"].write(SILENCE); await ce["writer"].drain()
                    except: pass
            await asyncio.sleep(0.5)

    await asyncio.gather(
        one_way_bridge("caller→callee", ci["queue"], ce["writer"], callee_lock,
                       lambda: ci.get("alive", False), lambda: ce.get("alive", False),
                       "English", lang, callee_speaking, caller_speaking,
                       prewarmed_ws=caller_ws),
        one_way_bridge("callee→caller", ce["queue"], ci["writer"], caller_lock,
                       lambda: ce.get("alive", False), lambda: ci.get("alive", False),
                       lang, "English", caller_speaking, callee_speaking,
                       prewarmed_ws=callee_ws),
        keepalive_both()
    )

async def handle_connection(reader, writer):
    peer = writer.get_extra_info("peername")
    log.info(f"New connection from {peer}")
    try:
        t, p = await asyncio.wait_for(read_frame(reader), timeout=10)
        if t != MSG_UUID: writer.close(); return
        uuid = parse_uuid(p)
        log.info(f"UUID: {uuid}")
        writer.write(SILENCE); await writer.drain()
    except Exception as e:
        log.error(f"UUID read failed: {e}"); writer.close(); return

    caller_uuid, callee_uuid, dest, cid, role = find_call(uuid)
    if not caller_uuid:
        log.error(f"No call for UUID: {uuid}"); writer.close(); return

    lang = get_language(dest) if dest else "English"
    log.info(f"Role: {role} | Dest: {dest} | Lang: {lang} | CID: {cid}")

    queue = asyncio.Queue(maxsize=200)  # ~4s of audio at 8kHz/320-byte frames
    stop_ka = asyncio.Event()
    conn = {"queue": queue, "writer": writer, "alive": True}

    # setdefault: never overwrite existing entry (fixes race where callee
    # arrives before caller and caller's init would wipe the callee conn)
    calls.setdefault(caller_uuid, {"caller": None, "callee": None, "lang": lang, "cid": cid})
    calls[caller_uuid][role] = conn
    # Note: for "caller" role, prewarm keys were already set above

    if role == "caller":
        log.info(f"Caller connected - playing ringback, originating to {dest}")
        asyncio.create_task(ami_originate(callee_uuid, dest, cid))
        ka_task = asyncio.create_task(ringback_loop(writer, stop_ka))

        # Pre-warm both OpenAI sessions during ringback so they're ready
        # the moment callee answers — eliminates session setup latency
        src_lang_code_en = LANG_CODES.get("english", "en")
        src_lang_code_dst = LANG_CODES.get(lang.lower(), None)
        prewarm_stop = asyncio.Event()

        caller_ws_holder = [None]
        callee_ws_holder = [None]
        caller_ready = asyncio.Event()
        callee_ready = asyncio.Event()

        pw_caller = asyncio.create_task(
            prewarm_openai_session("English", lang, src_lang_code_en,
                                   caller_ready, caller_ws_holder, prewarm_stop))
        pw_callee = asyncio.create_task(
            prewarm_openai_session(lang, "English", src_lang_code_dst,
                                   callee_ready, callee_ws_holder, prewarm_stop))

        # calls entry already created by the setdefault above
        calls[caller_uuid].update({
            "prewarm_caller_ws": caller_ws_holder,
            "prewarm_callee_ws": callee_ws_holder,
            "prewarm_caller_task": pw_caller,
            "prewarm_callee_task": pw_callee,
            "prewarm_stop": prewarm_stop,
        })
        log.info("Pre-warming OpenAI sessions during ringback...")
    else:
        ka_task = asyncio.create_task(keepalive(writer, stop_ka))

    # Store stop event BEFORE bridge check so the other side can always find it
    conn["stop_ka_event"] = stop_ka

    if role == "callee":
        log.info("Callee connected!")
        if calls[caller_uuid]["caller"]:
            log.info("Both legs ready - starting bridge (callee arrived second)")
            caller_conn = calls[caller_uuid]["caller"]
            caller_stop = caller_conn.get("stop_ka_event")
            if caller_stop:
                caller_stop.set()
            asyncio.create_task(run_bridge(caller_uuid))
        else:
            log.info("Callee arrived before caller - waiting")

    elif role == "caller":
        # Late-bridge: callee may have arrived before us (fast answer / retry)
        if calls[caller_uuid]["callee"]:
            log.info("Both legs ready - starting bridge (caller arrived second)")
            stop_ka.set()  # stop ringback immediately
            asyncio.create_task(run_bridge(caller_uuid))

    try:
        while True:
            t, p = await read_frame(reader)
            if t == MSG_HANGUP:
                log.info(f"{role} hung up")
                break
            if t == MSG_AUDIO and p:
                try:
                    queue.put_nowait(p)
                except asyncio.QueueFull:
                    pass  # drop oldest audio frame rather than blocking the reader
    except Exception as e:
        log.info(f"{role} ended: {e}")
    finally:
        stop_ka.set()
        conn["alive"] = False
        ka_task.cancel()
        # Cancel prewarm tasks if call ended before bridge started
        # (callee never answered — prewarm would otherwise run indefinitely)
        if role == "caller":
            call_entry = calls.get(caller_uuid, {})
            for task_key in ("prewarm_caller_task", "prewarm_callee_task"):
                t = call_entry.get(task_key)
                if t and not t.done():
                    t.cancel()

        # Hang up the other party
        call = calls.get(caller_uuid, {})
        other_role = "callee" if role == "caller" else "caller"
        other = call.get(other_role)
        if other and other.get("alive"):
            log.info(f"{role} disconnected - hanging up {other_role}")
            other["alive"] = False
            await send_hangup(other["writer"])
            try: other["writer"].close()
            except: pass

        if caller_uuid in calls:
            calls[caller_uuid][role] = None
            if not calls[caller_uuid]["caller"] and not calls[caller_uuid]["callee"]:
                calls.pop(caller_uuid, None)
                try: os.remove(f"/tmp/call_{caller_uuid}.txt")
                except: pass
        try: writer.close()
        except: pass

async def cleanup_calls():
    """Watchdog: remove orphaned call entries that were never cleaned up
    (e.g. crash mid-call before the finally block ran).
    Runs every 60 seconds.
    """
    while True:
        await asyncio.sleep(60)
        stale = [k for k, v in list(calls.items())
                 if not v.get("caller") and not v.get("callee")]
        for k in stale:
            calls.pop(k, None)
            try: os.remove(f"/tmp/call_{k}.txt")
            except: pass
            log.info(f"[cleanup] removed stale call entry {k}")

async def main():
    if not OPENAI_API_KEY: log.error("OPENAI_API_KEY not set!"); return
    server = await asyncio.start_server(handle_connection, HOST, PORT)
    log.info(f"Listening on {HOST}:{PORT}")
    asyncio.create_task(cleanup_calls())
    async with server: await server.serve_forever()

if __name__ == "__main__":
    asyncio.run(main())

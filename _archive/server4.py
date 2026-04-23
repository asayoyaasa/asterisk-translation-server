import asyncio, websockets, json, os, base64, logging, glob, struct, uuid as uuid_mod
import soxr
import numpy as np

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("/var/log/translation-server.log"),
    ],
)
log = logging.getLogger(__name__)

from languages import get_language

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
HOST, PORT           = "127.0.0.1", 5001
AMI_HOST, AMI_PORT   = "127.0.0.1", 5038
AMI_USER, AMI_SECRET = "translation", "TrServer2024!"

OPENAI_MODEL  = "gpt-4o-realtime-preview-2024-12-17"
OPENAI_WS_URL = f"wss://api.openai.com/v1/realtime?model={OPENAI_MODEL}"

MSG_UUID, MSG_AUDIO, MSG_HANGUP = 0x01, 0x10, 0xff
SILENCE_PAYLOAD = bytes(320)
SILENCE      = struct.pack(">BH", MSG_AUDIO, 320) + SILENCE_PAYLOAD
HANGUP_FRAME = struct.pack(">BH", MSG_HANGUP, 0)

calls_lock = asyncio.Lock()
calls: dict = {}

# ── Audio helpers ─────────────────────────────────────────────────────────────

def build_frame(audio: bytes) -> bytes:
    return struct.pack(">BH", MSG_AUDIO, len(audio)) + audio

def parse_uuid(p: bytes) -> str:
    return (str(uuid_mod.UUID(bytes=p)) if len(p) == 16
            else p.decode("utf-8", "ignore").strip("\x00").strip())

def resample_up(audio: bytes) -> bytes:
    """8 kHz PCM16 (Asterisk) -> 24 kHz PCM16 (OpenAI)."""
    s = np.frombuffer(audio, dtype=np.int16).astype(np.float32)
    return soxr.resample(s, 8000, 24000, quality="VHQ").astype(np.int16).tobytes()

def resample_down(audio: bytes) -> bytes:
    """24 kHz PCM16 (OpenAI) -> 8 kHz PCM16 (Asterisk)."""
    s = np.frombuffer(audio, dtype=np.int16).astype(np.float32)
    return soxr.resample(s, 24000, 8000, quality="VHQ").astype(np.int16).tobytes()

def fast_rms(audio: bytes) -> int:
    s = np.frombuffer(audio, dtype=np.int16)
    return int(np.sqrt((s.astype(np.int32) ** 2).mean()))

# ── Prompt ────────────────────────────────────────────────────────────────────

PROMPT_CACHE: dict = {}
BASE_PROMPT: str   = ""

LANGUAGE_HINTS = {
    "ja": "Use natural spoken Japanese. Avoid overly formal keigo.",
    "zh": "Use concise Simplified Chinese.",
    "es": "Use neutral Latin American Spanish.",
    "id": "Use natural conversational Indonesian.",
    "en": "Use natural spoken English.",
}

def load_base_prompt() -> str:
    with open("prompt.md", "r", encoding="utf-8") as f:
        return f.read()

def get_prompt(src_lang: str, dst_lang: str) -> str:
    key = f"{src_lang}-{dst_lang}"
    if key in PROMPT_CACHE:
        return PROMPT_CACHE[key]
    extra = LANGUAGE_HINTS.get(dst_lang.lower(), "Use natural, spoken-style translation.")
    prompt = BASE_PROMPT.format(src_lang=src_lang, dst_lang=dst_lang, extra_rules=extra)
    PROMPT_CACHE[key] = prompt
    log.info(f"Prompt cached for {src_lang}->{dst_lang}")
    return prompt

BASE_PROMPT = load_base_prompt()

# ── Infrastructure helpers ────────────────────────────────────────────────────

def find_call(uuid: str):
    try:
        with open(f"/tmp/call_{uuid}.txt") as f:
            parts = f.read().strip().split()
            if len(parts) >= 3:
                return (parts[0], parts[1], parts[2],
                        parts[3] if len(parts) >= 4 else "", "caller")
    except Exception:
        pass
    for fp in glob.glob("/tmp/call_*.txt"):
        try:
            with open(fp) as f:
                parts = f.read().strip().split()
                if len(parts) >= 3 and parts[1] == uuid:
                    return (parts[0], parts[1], parts[2],
                            parts[3] if len(parts) >= 4 else "", "callee")
        except Exception:
            pass
    return None, None, None, None, None

async def ami_originate(callee_uuid: str, dest: str, cid: str):
    log.info(f"AMI originating to {dest} | callee={callee_uuid} | CID={cid}")
    try:
        r, w = await asyncio.open_connection(AMI_HOST, AMI_PORT)
        await r.readline()
        w.write(
            f"Action: Login\r\nUsername: {AMI_USER}\r\nSecret: {AMI_SECRET}\r\n\r\n"
            .encode()
        )
        await w.drain()
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += await asyncio.wait_for(r.read(4096), timeout=5)
        if b"Authentication accepted" not in buf:
            log.error("AMI login failed"); w.close(); return
        w.write((
            f"Action: Originate\r\nChannel: PJSIP/{dest}@didlogic-outbound\r\n"
            f"Context: callee-audiosocket\r\nExten: s\r\nPriority: 1\r\nTimeout: 60000\r\n"
            f"CallerID: {cid}\r\n"
            f"Variable: CALLEE_UUID={callee_uuid},REAL_CALLERID={cid}\r\n"
            f"Async: yes\r\n\r\n"
        ).encode())
        await w.drain()
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += await asyncio.wait_for(r.read(4096), timeout=5)
        log.info(f"AMI originate: {buf.decode().strip()[:80]}")
        w.write(b"Action: Logoff\r\n\r\n"); await w.drain(); w.close()
    except Exception as e:
        log.error(f"AMI error: {e}")

def generate_ringback() -> bytes:
    import math
    samples = [int(16000 * math.sin(2 * math.pi * 425 * i / 8000)) for i in range(8000)]
    return struct.pack(f"<{len(samples)}h", *samples)

RINGBACK_1S         = generate_ringback()
RINGBACK_SILENCE_1S = bytes(16000)

async def ringback_loop(writer, stop_event):
    ring_audio = RINGBACK_1S + RINGBACK_SILENCE_1S
    while not stop_event.is_set():
        for i in range(0, len(ring_audio), 320):
            if stop_event.is_set():
                break
            chunk = ring_audio[i:i + 320]
            if len(chunk) < 320:
                chunk += b"\x00" * (320 - len(chunk))
            try:
                writer.write(build_frame(chunk))
                await writer.drain()
            except Exception:
                return
            await asyncio.sleep(0.02)

async def keepalive(writer, stop_event):
    while not stop_event.is_set():
        try:
            writer.write(SILENCE)
            await writer.drain()
        except Exception:
            break
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
    except Exception:
        pass

# ── Language codes ────────────────────────────────────────────────────────────

LANG_CODES = {
    "afrikaans": "af", "albanian": "sq", "amharic": "am",
    "arabic": "ar", "bosnian": "bs", "bulgarian": "bg",
    "catalan": "ca", "chinese": "zh", "mandarin": "zh",
    "croatian": "hr", "czech": "cs", "danish": "da",
    "dhivehi": "dv", "dutch": "nl", "dzongkha": "dz",
    "english": "en", "estonian": "et", "fijian": "fj",
    "filipino": "tl", "finnish": "fi", "french": "fr",
    "georgian": "ka", "german": "de", "greek": "el",
    "hebrew": "he", "hindi": "hi", "hungarian": "hu",
    "icelandic": "is", "indonesian": "id", "italian": "it",
    "japanese": "ja", "khmer": "km", "kinyarwanda": "rw",
    "korean": "ko", "kyrgyz": "ky", "lao": "lo",
    "latvian": "lv", "lithuanian": "lt", "luxembourgish": "lb",
    "malagasy": "mg", "malay": "ms", "maltese": "mt",
    "mongolian": "mn", "montenegrin": "sr", "nepali": "ne",
    "norwegian": "no", "persian": "fa", "polish": "pl",
    "portuguese": "pt", "romanian": "ro", "russian": "ru",
    "serbian": "sr", "sesotho": "st", "sinhala": "si",
    "slovak": "sk", "slovenian": "sl", "somali": "so",
    "spanish": "es", "swahili": "sw", "swati": "ss",
    "swedish": "sv", "tajik": "tg", "thai": "th",
    "tigrinya": "ti", "turkish": "tr", "turkmen": "tk",
    "ukrainian": "uk", "urdu": "ur", "uzbek": "uz",
    "vietnamese": "vi", "welsh": "cy",
}

# ── Metrics ───────────────────────────────────────────────────────────────────

class Metrics:
    def __init__(self):
        self.played   = 0
        self.commits  = 0
        self.filtered = 0
        self.dupes    = 0
        self._t       = None

    def tick(self):
        try:
            now = asyncio.get_running_loop().time()
        except RuntimeError:
            return
        if self._t is None:
            self._t = now
            return
        if now - self._t >= 60:
            log.info(
                f"[METRICS] played={self.played} commits={self.commits} "
                f"filtered={self.filtered} dupes={self.dupes}"
            )
            self._t = now

metrics = Metrics()

# ── Pre-warm ──────────────────────────────────────────────────────────────────

async def prewarm_openai_session(
    src_lang: str,
    dst_lang: str,
    src_lang_code: str,
    ready_event: asyncio.Event,
    ws_holder: list,
    stop_event: asyncio.Event,
):
    """
    Opens a WebSocket, sends session.update, waits for session.updated.
    Stores the open socket in ws_holder[0] and then stops consuming messages.
    The WebSocket remains open for the bridge to take ownership.
    """
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "OpenAI-Beta": "realtime=v1"}
    ws = None
    try:
        log.info(f"[prewarm {src_lang}->{dst_lang}] connecting...")
        ws = await websockets.connect(OPENAI_WS_URL, additional_headers=headers)
        ws_holder[0] = ws
        
        await ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "modalities": ["audio", "text"],
                "instructions": get_prompt(src_lang, dst_lang),
                "voice": "verse",
                "input_audio_format": "pcm16",
                "output_audio_format": "pcm16",
                "input_audio_transcription": {
                    "model": "gpt-4o-mini-transcribe",
                    "language": src_lang_code,
                },
                "turn_detection": {
                    "type": "semantic_vad",
                    "eagerness": "high",
                    "create_response": False,
                },
                "temperature": 0.6,   # minimum allowed value
            },
        }))
        
        log.info(f"[prewarm {src_lang}->{dst_lang}] session.update sent, waiting for ready...")
        
        # Read messages only until session.updated, then exit cleanly
        async for msg in ws:
            if stop_event.is_set():
                log.info(f"[prewarm {src_lang}->{dst_lang}] stop_event set, exiting")
                return
            
            try:
                event = json.loads(msg)
                etype = event.get("type", "")
                
                if etype == "session.created":
                    log.info(f"[prewarm {src_lang}->{dst_lang}] session created")
                elif etype == "session.updated":
                    log.info(f"[prewarm {src_lang}->{dst_lang}] session ready")
                    ready_event.set()
                    # Exit cleanly - WebSocket remains open for bridge
                    return
                elif etype == "error":
                    log.error(f"[prewarm {src_lang}->{dst_lang}] error: {event}")
                    # Do NOT set ready_event; let the bridge know prewarm failed
                    return
                else:
                    log.debug(f"[prewarm {src_lang}->{dst_lang}] ignored event: {etype}")
                    
            except Exception as e:
                log.error(f"[prewarm {src_lang}->{dst_lang}] parse error: {e}")
                return

    except asyncio.CancelledError:
        log.info(f"[prewarm {src_lang}->{dst_lang}] cancelled")
    except Exception as e:
        log.error(f"[prewarm {src_lang}->{dst_lang}] connect failed: {e}")
    finally:
        # If ready_event is not set, the WebSocket is unusable; close it.
        if ws is not None and ws_holder[0] is ws and not ready_event.is_set():
            log.info(f"[prewarm {src_lang}->{dst_lang}] closing WebSocket (prewarm failed)")
            try:
                await ws.close()
            except Exception:
                pass
            ws_holder[0] = None  # clear the holder

# ── One-way bridge ────────────────────────────────────────────────────────────

async def one_way_bridge(
    label: str,
    src_queue: asyncio.Queue,
    dst_writer,
    dst_lock: asyncio.Lock,
    src_alive_fn,
    dst_alive_fn,
    src_lang: str,
    dst_lang: str,
    speaking_flag: list,       # [bool]  True while THIS bridge is playing to dst
    peer_speaking_flag: list,  # [bool]  True while PEER bridge is playing to src
    prewarmed_ws=None,
    stop_event: asyncio.Event = None,
):
    """
    Translates audio from src_queue -> OpenAI Realtime API -> dst_writer.
    """
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta": "realtime=v1",
    }
    src_lang_code = LANG_CODES.get(src_lang.lower())

    # ── Tunable constants ─────────────────────────────────────────────────────
    RMS_SILENCE_FLOOR = 80
    COMMIT_INTERVAL   = 8.0
    MIN_FRAMES        = 16

    HALLUCINATED = [
        "tidak ada terjemahan", "no translation", "empty",
    ]
    ASSISTANT_PHRASES = [
        "how can i help", "how may i help", "how can i assist",
        "how may i assist", "is there anything else", "i'd be happy to",
        "i would be happy to", "i'm here to help", "i am here to help",
        "thank you for calling", "thank you for contacting",
        "have a great day", "have a nice day",
        "let me know if you need", "don't hesitate to",
    ]

    # ── Shared mutable state ─────────────────────────────────────────────────
    session_ready   = asyncio.Event()
    abort_playback  = asyncio.Event()
    translation_q   = asyncio.Queue(maxsize=5)

    last_commit_t   = [0.0]
    frames_buffered = [0]
    current_resp_id = [None]
    user_item_ids   = []  # list of user audio item IDs to delete

    # Per-response accumulator
    resp = {
        "id":               None,
        "audio":            [],
        "audio_done":       False,
        "transcript":       "",
        "transcript_done":  False,
        "blocked":          False,
        "original":         "",
    }

    def reset_resp():
        resp["id"]              = None
        resp["audio"]           = []
        resp["audio_done"]      = False
        resp["transcript"]      = ""
        resp["transcript_done"] = False
        resp["blocked"]         = False
        resp["original"]        = ""

    # ── Duplicate detection ───────────────────────────────────────────────────
    last_tx_text = [""]
    last_tx_time = [0.0]

    def _normalize(t: str) -> str:
        import re
        return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", t.lower().strip()))

    def is_duplicate(text: str, now: float) -> bool:
        prev = last_tx_text[0]
        if not prev or (now - last_tx_time[0]) > 8.0:
            return False
        t1, t2 = _normalize(text), _normalize(prev)
        if t1 == t2:
            return True
        if len(t1) > 10 and len(t2) > 10:
            s1, s2 = set(t1.split()), set(t2.split())
            if s1 and s2 and len(s1 & s2) / max(len(s1), len(s2)) > 0.85:
                return True
        return False

    # ── Post-filter ───────────────────────────────────────────────────────────
    def post_filter(text: str, original: str, now: float):
        if not text:
            return True, "empty"
        tl = text.lower()
        ol = original.lower()
        if any(p in tl for p in HALLUCINATED):
            return True, "hallucination"
        if (any(p in tl for p in ASSISTANT_PHRASES)
                and not any(p in ol for p in ASSISTANT_PHRASES)):
            return True, "assistant phrase"
        if is_duplicate(text, now):
            return True, "duplicate"
        return False, ""

    # ── try_enqueue ───────────────────────────────────────────────────────────
    def try_enqueue():
        if not (resp["audio_done"] and resp["transcript_done"]):
            return

        if resp["blocked"] or not resp["audio"]:
            reset_resp()
            return

        item = {"audio": list(resp["audio"]), "transcript": resp["transcript"]}

        if translation_q.full():
            try:
                translation_q.get_nowait()
                log.warning(f"[{label}] translation_q full - dropped oldest chunk")
            except asyncio.QueueEmpty:
                pass

        try:
            translation_q.put_nowait(item)
            log.info(f"[{label}] queued  : {resp['transcript']!r}")
        except asyncio.QueueFull:
            log.warning(f"[{label}] translation_q still full - dropped item")

        reset_resp()

    # ── Inner runner ──────────────────────────────────────────────────────────
    async def _run_with_ws(ws):
        _loop = asyncio.get_running_loop()
        _inner_stop = asyncio.Event()
        
        log.info(f"[{label}] Starting bridge with WebSocket (prewarmed={prewarmed_ws is not None})")

        # ── Serialised outbound WS writer ─────────────────────────────────────
        _ws_q: asyncio.Queue = asyncio.Queue(maxsize=500)

        async def _ws_writer():
            while True:
                msg = await _ws_q.get()
                if msg is None:
                    return
                try:
                    await ws.send(msg)
                except Exception as e:
                    log.error(f"[{label}] ws_writer send error: {e}")
                    return

        def ws_send(d: dict):
            try:
                _ws_q.put_nowait(json.dumps(d))
            except asyncio.QueueFull:
                log.warning(f"[{label}] _ws_q full - dropping {d.get('type', '?')}")

        # Helper to delete old user items
        def delete_old_user_items():
            nonlocal user_item_ids
            for uid in user_item_ids:
                ws_send({"type": "conversation.item.delete", "item_id": uid})
            user_item_ids.clear()

        # ── Configure session ─────────────────────────────────────────────────
        if prewarmed_ws is None:
            log.info(f"[{label}] Using fresh WebSocket connection")
            await ws.send(json.dumps({
                "type": "session.update",
                "session": {
                    "modalities": ["audio", "text"],
                    "instructions": get_prompt(src_lang, dst_lang),
                    "voice": "verse",
                    "input_audio_format": "pcm16",
                    "output_audio_format": "pcm16",
                    "input_audio_transcription": {
                        "model": "gpt-4o-mini-transcribe",
                        "language": src_lang_code,
                    },
                    "turn_detection": {
                        "type": "semantic_vad",
                        "eagerness": "high",
                        "create_response": False,
                    },
                    "temperature": 0.6,
                },
            }))
            log.info(f"[{label}] session.update sent - awaiting session.updated...")
        else:
            log.info(f"[{label}] reusing pre-warmed session ({src_lang}->{dst_lang})")
            ws_send({"type": "input_audio_buffer.clear"})
            # Clear any leftover user items (unlikely, but safe)
            delete_old_user_items()
            session_ready.set()
            last_commit_t[0] = _loop.time()

        # ── streamer: src_queue -> OpenAI buffer ──────────────────────────────
        async def streamer():
            log.info(f"[{label}] streamer started")
            while (src_alive_fn()
                   and not _inner_stop.is_set()
                   and not (stop_event and stop_event.is_set())):

                if not session_ready.is_set():
                    await asyncio.sleep(0.05)
                    continue

                if peer_speaking_flag and peer_speaking_flag[0]:
                    while not src_queue.empty():
                        try:
                            src_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                    ws_send({"type": "input_audio_buffer.clear"})
                    frames_buffered[0] = 0
                    last_commit_t[0]   = _loop.time()
                    await asyncio.sleep(0.05)
                    continue

                elapsed = _loop.time() - last_commit_t[0]
                if frames_buffered[0] >= MIN_FRAMES and elapsed >= COMMIT_INTERVAL:
                    log.info(
                        f"[{label}] time-trigger commit "
                        f"({frames_buffered[0]} frames, {elapsed:.1f}s)"
                    )
                    # Delete old user items before committing
                    delete_old_user_items()
                    ws_send({"type": "input_audio_buffer.commit"})
                    frames_buffered[0] = 0
                    last_commit_t[0]   = _loop.time()
                    await asyncio.sleep(0.1)
                    continue

                try:
                    audio = await asyncio.wait_for(src_queue.get(), timeout=0.05)
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    log.error(f"[{label}] streamer queue read: {e}")
                    return

                if fast_rms(audio) < RMS_SILENCE_FLOOR:
                    continue

                try:
                    audio24 = await _loop.run_in_executor(None, resample_up, audio)
                    ws_send({
                        "type":  "input_audio_buffer.append",
                        "audio": base64.b64encode(audio24).decode(),
                    })
                    frames_buffered[0] += 1
                except Exception as e:
                    log.error(f"[{label}] streamer resample/append: {e}")
                    return

        # ── pipe_out: OpenAI events -> translation_q ──────────────────────────
        async def pipe_out():
            log.info(f"[{label}] pipe_out started")
            try:
                async for raw in ws:
                    if _inner_stop.is_set() or (stop_event and stop_event.is_set()):
                        log.info(f"[{label}] pipe_out stopping due to stop_event")
                        break
                    try:
                        event = json.loads(raw)
                        etype = event.get("type", "")
                        
                        log.debug(f"[{label}] Received event: {etype}")

                        if etype == "session.updated":
                            log.info(f"[{label}] session.updated - ready")
                            session_ready.set()
                            last_commit_t[0] = _loop.time()

                        elif etype == "conversation.item.created":
                            item = event.get("item", {})
                            if item.get("type") == "message" and item.get("role") == "user":
                                uid = item.get("id")
                                if uid:
                                    user_item_ids.append(uid)
                                    log.debug(f"[{label}] stored user item: {uid}")

                        elif etype == "input_audio_buffer.committed":
                            frames_buffered[0] = 0
                            last_commit_t[0]   = _loop.time()
                            reset_resp()
                            abort_playback.clear()
                            # Delete old user items before creating response
                            delete_old_user_items()
                            ws_send({
                                "type": "response.create",
                                "response": {"conversation": "none"},
                            })
                            metrics.commits += 1
                            log.info(f"[{label}] committed -> response.create")

                        elif etype == "response.created":
                            rid = event.get("response", {}).get("id")
                            current_resp_id[0] = rid
                            resp["id"]         = rid
                            log.info(f"[{label}] response created: {rid}")

                        elif etype == "conversation.item.input_audio_transcription.completed":
                            src_text         = event.get("transcript", "").strip()
                            resp["original"] = src_text
                            log.info(f"[{label}] SRC : {src_text!r}")

                        elif etype == "response.audio.delta":
                            if event.get("response_id") == current_resp_id[0]:
                                chunk = base64.b64decode(event.get("delta", ""))
                                if chunk:
                                    resp["audio"].append(chunk)

                        elif etype == "response.audio.done":
                            if event.get("response_id") == current_resp_id[0]:
                                resp["audio_done"] = True
                                try_enqueue()

                        elif etype == "response.audio_transcript.done":
                            if event.get("response_id") == current_resp_id[0]:
                                text = event.get("transcript", "").strip()
                                now  = _loop.time()
                                log.info(f"[{label}] DST : {text!r}")

                                blocked, reason = post_filter(
                                    text, resp["original"], now
                                )
                                if blocked:
                                    log.warning(
                                        f"[{label}] FILTER [{reason}]: {text!r}"
                                    )
                                    resp["blocked"] = True
                                    metrics.filtered += 1
                                else:
                                    resp["transcript"] = text
                                    last_tx_text[0]    = text
                                    last_tx_time[0]    = now

                                resp["transcript_done"] = True
                                try_enqueue()

                        elif etype == "input_audio_buffer.speech_started":
                            if speaking_flag[0]:
                                log.info(
                                    f"[{label}] barge-in -> abort + drain queue"
                                )
                                abort_playback.set()
                                speaking_flag[0] = False
                                while not translation_q.empty():
                                    try:
                                        translation_q.get_nowait()
                                    except asyncio.QueueEmpty:
                                        break
                            else:
                                log.info(f"[{label}] speech started")

                        elif etype == "error":
                            code = event.get("error", {}).get("code", "")
                            benign = {
                                "response_cancel_not_active",
                                "input_audio_buffer_commit_empty",
                                "conversation_item_not_found",
                            }
                            if code in benign:
                                log.debug(f"[{label}] benign error: {code}")
                            else:
                                log.error(f"[{label}] OpenAI error: {event}")

                    except Exception as e:
                        log.error(f"[{label}] pipe_out event parse error: {e}")

            except websockets.exceptions.ConnectionClosed as e:
                log.warning(f"[{label}] WebSocket connection closed: {e}")
            except Exception as e:
                log.error(f"[{label}] pipe_out ws loop error: {e}")
            finally:
                log.info(f"[{label}] pipe_out ended")
                _inner_stop.set()

        # ── sender: translation_q -> dst_writer ──────────────────────────────
        async def sender():
            log.info(f"[{label}] sender started")
            while (src_alive_fn()
                   and not _inner_stop.is_set()
                   and not (stop_event and stop_event.is_set())):

                try:
                    item = await asyncio.wait_for(translation_q.get(), timeout=0.2)
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    log.error(f"[{label}] sender queue read: {e}")
                    return

                if (not dst_alive_fn()
                        or _inner_stop.is_set()
                        or (stop_event and stop_event.is_set())):
                    break

                try:
                    raw_audio = b"".join(item["audio"])
                    audio8    = await _loop.run_in_executor(
                        None, resample_down, raw_audio
                    )
                except Exception as e:
                    log.error(f"[{label}] sender resample: {e}")
                    continue

                abort_playback.clear()
                speaking_flag[0] = True
                ws_send({"type": "input_audio_buffer.clear"})

                log.info(f"[{label}] playing : {item['transcript']!r}")

                try:
                    async with dst_lock:
                        for i in range(0, len(audio8), 320):
                            if abort_playback.is_set():
                                log.info(f"[{label}] playback aborted mid-chunk")
                                break
                            if (not dst_alive_fn()
                                    or _inner_stop.is_set()
                                    or (stop_event and stop_event.is_set())):
                                break
                            frame = audio8[i:i + 320]
                            if len(frame) < 320:
                                frame += b"\x00" * (320 - len(frame))
                            dst_writer.write(build_frame(frame))
                            await dst_writer.drain()
                            await asyncio.sleep(0.019)

                        if not abort_playback.is_set() and dst_alive_fn():
                            for _ in range(4):
                                dst_writer.write(build_frame(b"\x00" * 320))
                                await dst_writer.drain()
                                await asyncio.sleep(0.019)

                except Exception as e:
                    log.error(f"[{label}] sender write: {e}")

                await asyncio.sleep(0.6)
                speaking_flag[0] = False
                metrics.played  += 1
                log.info(f"[{label}] playback done -> ready")

        # ── Gather all three coroutines + ws writer ───────────────────────────
        ws_writer_task = asyncio.create_task(_ws_writer())
        try:
            await asyncio.gather(streamer(), pipe_out(), sender())
        except asyncio.CancelledError:
            log.info(f"[{label}] bridge cancelled")
        finally:
            _inner_stop.set()
            try:
                _ws_q.put_nowait(None)
            except asyncio.QueueFull:
                pass
            try:
                await asyncio.wait_for(ws_writer_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                ws_writer_task.cancel()

    # ── Entry: use pre-warmed socket or open a fresh connection ───────────────
    try:
        if prewarmed_ws is not None:
            log.info(f"[{label}] Using pre-warmed WebSocket")
            await _run_with_ws(prewarmed_ws)
        else:
            log.info(f"[{label}] Opening fresh WebSocket connection")
            async with websockets.connect(OPENAI_WS_URL, additional_headers=headers) as ws:
                await _run_with_ws(ws)
    except asyncio.CancelledError:
        log.debug(f"[{label}] bridge cancelled")
    except Exception as e:
        log.error(f"[{label}] bridge error: {e}")

# ── run_bridge ────────────────────────────────────────────────────────────────

async def run_bridge(caller_uuid: str):
    async with calls_lock:
        call = calls.get(caller_uuid)
        if not call:
            return
        ci = call.get("caller")
        ce = call.get("callee")

    if not ci or not ce:
        log.error("[run_bridge] missing legs - aborting")
        return

    lang = call["lang"]
    log.info(f"=== BRIDGE ACTIVE EN <-> {lang} ===")

    caller_lock = asyncio.Lock()
    callee_lock = asyncio.Lock()
    callee_speaking = [False]
    caller_speaking = [False]

    stop_caller = asyncio.Event()
    stop_callee = asyncio.Event()

    # Get prewarm tasks and stop events
    async with calls_lock:
        entry = calls.get(caller_uuid, {})
        prewarm_stop = entry.get("prewarm_stop")
        cw_holder = entry.get("prewarm_caller_ws", [None])
        ew_holder = entry.get("prewarm_callee_ws", [None])
        pw_caller = entry.get("prewarm_caller_task")
        pw_callee = entry.get("prewarm_callee_task")
        caller_ready = entry.get("prewarm_caller_ready")
        callee_ready = entry.get("prewarm_callee_ready")

    # Signal prewarm tasks to stop (they'll exit cleanly)
    if prewarm_stop:
        log.info("[run_bridge] Signaling prewarm tasks to stop")
        prewarm_stop.set()

    # Wait for prewarm tasks to complete
    for task_name, task in [("caller", pw_caller), ("callee", pw_callee)]:
        if task and not task.done():
            try:
                log.info(f"[run_bridge] Waiting for prewarm {task_name} task to complete")
                await asyncio.wait_for(task, timeout=2.0)
                log.info(f"[run_bridge] Prewarm {task_name} task completed")
            except asyncio.TimeoutError:
                log.warning(f"[run_bridge] Prewarm {task_name} task timeout - cancelling")
                task.cancel()
                try:
                    await task
                except Exception:
                    pass
            except Exception as e:
                log.error(f"[run_bridge] Error waiting for prewarm {task_name}: {e}")

    # Determine if prewarmed WebSockets are usable (ready_event must be set)
    caller_ws = cw_holder[0] if cw_holder else None
    callee_ws = ew_holder[0] if ew_holder else None

    if caller_ws and caller_ready and not caller_ready.is_set():
        log.warning("[run_bridge] Caller prewarm failed, discarding WebSocket")
        try:
            await caller_ws.close()
        except:
            pass
        caller_ws = None
    if callee_ws and callee_ready and not callee_ready.is_set():
        log.warning("[run_bridge] Callee prewarm failed, discarding WebSocket")
        try:
            await callee_ws.close()
        except:
            pass
        callee_ws = None

    # Clear holders to prevent cleanup
    if cw_holder:
        cw_holder[0] = None
    if ew_holder:
        ew_holder[0] = None

    log.info(
        f"[run_bridge] caller_ws={'prewarmed' if caller_ws else 'fresh'} "
        f"callee_ws={'prewarmed' if callee_ws else 'fresh'}"
    )

    # Create bridge tasks
    bridge_caller = asyncio.create_task(one_way_bridge(
        "caller->callee",
        ci["queue"], ce["writer"], callee_lock,
        lambda: ci.get("alive", False),
        lambda: ce.get("alive", False),
        "English", lang,
        callee_speaking, caller_speaking,
        prewarmed_ws=caller_ws,
        stop_event=stop_caller,
    ))
    bridge_callee = asyncio.create_task(one_way_bridge(
        "callee->caller",
        ce["queue"], ci["writer"], caller_lock,
        lambda: ce.get("alive", False),
        lambda: ci.get("alive", False),
        lang, "English",
        caller_speaking, callee_speaking,
        prewarmed_ws=callee_ws,
        stop_event=stop_callee,
    ))

    async with calls_lock:
        if caller_uuid in calls:
            calls[caller_uuid]["bridge_tasks"] = (bridge_caller, bridge_callee)

    # Keepalive task to maintain AudioSocket connections
    async def keepalive_both():
        while ci.get("alive") or ce.get("alive"):
            for conn, lock in ((ci, caller_lock), (ce, callee_lock)):
                if conn.get("alive") and not lock.locked():
                    try:
                        async with lock:
                            conn["writer"].write(SILENCE)
                            await conn["writer"].drain()
                    except Exception:
                        pass
            await asyncio.sleep(0.5)
    
    keepalive_task = asyncio.create_task(keepalive_both())

    try:
        await asyncio.gather(bridge_caller, bridge_callee, keepalive_task)
    except asyncio.CancelledError:
        log.info("[run_bridge] Bridge tasks cancelled")
    finally:
        log.info("[run_bridge] Cleaning up")
        stop_caller.set()
        stop_callee.set()
        keepalive_task.cancel()
        
        for t in (bridge_caller, bridge_callee):
            if not t.done():
                t.cancel()
                try:
                    await t
                except Exception:
                    pass
        
        # Close WebSockets
        for ws_obj in (caller_ws, callee_ws):
            if ws_obj:
                try:
                    await ws_obj.close()
                    log.info("[run_bridge] Closed prewarmed WebSocket")
                except Exception as e:
                    log.error(f"[run_bridge] Error closing WebSocket: {e}")
        
        async with calls_lock:
            if caller_uuid in calls:
                calls[caller_uuid].pop("bridge_tasks", None)

# ── handle_connection ─────────────────────────────────────────────────────────

async def handle_connection(reader, writer):
    peer = writer.get_extra_info("peername")
    log.info(f"New connection from {peer}")

    try:
        t, p = await asyncio.wait_for(read_frame(reader), timeout=10)
        if t != MSG_UUID:
            writer.close()
            return
        uuid = parse_uuid(p)
        log.info(f"UUID: {uuid}")
        writer.write(SILENCE)
        await writer.drain()
    except Exception as e:
        log.error(f"UUID read failed: {e}")
        writer.close()
        return

    caller_uuid, callee_uuid, dest, cid, role = find_call(uuid)
    if not caller_uuid:
        log.error(f"No call entry for UUID: {uuid}")
        writer.close()
        return

    lang = get_language(dest) if dest else "English"
    log.info(f"Role: {role} | Dest: {dest} | Lang: {lang} | CID: {cid}")

    queue   = asyncio.Queue(maxsize=3000)
    stop_ka = asyncio.Event()
    conn    = {"queue": queue, "writer": writer, "alive": True}

    async with calls_lock:
        calls.setdefault(
            caller_uuid,
            {"caller": None, "callee": None, "lang": lang, "cid": cid},
        )
        calls[caller_uuid][role] = conn

    if role == "caller":
        log.info(f"Caller connected - ringback + originate to {dest}")
        asyncio.create_task(ami_originate(callee_uuid, dest, cid))
        ka_task = asyncio.create_task(ringback_loop(writer, stop_ka))

        prewarm_stop = asyncio.Event()
        cw_holder    = [None]
        ew_holder    = [None]
        caller_ready = asyncio.Event()
        callee_ready = asyncio.Event()

        pw_caller = asyncio.create_task(prewarm_openai_session(
            "English", lang,
            LANG_CODES.get("english", "en"),
            caller_ready, cw_holder, prewarm_stop,
        ))
        pw_callee = asyncio.create_task(prewarm_openai_session(
            lang, "English",
            LANG_CODES.get(lang.lower()),
            callee_ready, ew_holder, prewarm_stop,
        ))

        async with calls_lock:
            calls[caller_uuid].update({
                "prewarm_caller_ws":   cw_holder,
                "prewarm_callee_ws":   ew_holder,
                "prewarm_caller_task": pw_caller,
                "prewarm_callee_task": pw_callee,
                "prewarm_stop":        prewarm_stop,
                "prewarm_caller_ready": caller_ready,
                "prewarm_callee_ready": callee_ready,
            })
        log.info("Pre-warming OpenAI sessions during ringback...")

    else:
        ka_task = asyncio.create_task(keepalive(writer, stop_ka))

    conn["stop_ka_event"] = stop_ka

    if role == "callee":
        log.info("Callee connected!")
        async with calls_lock:
            caller_conn = calls.get(caller_uuid, {}).get("caller")
        if caller_conn:
            log.info("Both legs ready - starting bridge (callee arrived second)")
            if caller_conn.get("stop_ka_event"):
                caller_conn["stop_ka_event"].set()
            asyncio.create_task(run_bridge(caller_uuid))
        else:
            log.info("Callee arrived before caller - waiting")
    else:
        async with calls_lock:
            callee_conn = calls.get(caller_uuid, {}).get("callee")
        if callee_conn:
            log.info("Both legs ready - starting bridge (caller arrived second)")
            stop_ka.set()
            asyncio.create_task(run_bridge(caller_uuid))

    # ── Main audio read loop ──────────────────────────────────────────────────
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
                    try:
                        queue.get_nowait()
                        queue.put_nowait(p)
                    except Exception:
                        pass
    except Exception as e:
        log.info(f"{role} connection ended: {e}")
    finally:
        log.info(f"{role} cleaning up")
        stop_ka.set()
        conn["alive"] = False
        ka_task.cancel()

        async with calls_lock:
            bridge_tasks = calls.get(caller_uuid, {}).get("bridge_tasks")
            if bridge_tasks:
                for t in bridge_tasks:
                    if not t.done():
                        t.cancel()

        if role == "caller":
            async with calls_lock:
                for k in ("prewarm_caller_task", "prewarm_callee_task"):
                    t = calls.get(caller_uuid, {}).get(k)
                    if t and not t.done():
                        t.cancel()

        async with calls_lock:
            other_role = "callee" if role == "caller" else "caller"
            other = calls.get(caller_uuid, {}).get(other_role)
            if other and other.get("alive"):
                log.info(f"{role} disconnected - hanging up {other_role}")
                other["alive"] = False
                await send_hangup(other["writer"])
                try:
                    other["writer"].close()
                except Exception:
                    pass
            if caller_uuid in calls:
                calls[caller_uuid][role] = None
                if (not calls[caller_uuid]["caller"]
                        and not calls[caller_uuid]["callee"]):
                    calls.pop(caller_uuid, None)
                    try:
                        os.remove(f"/tmp/call_{caller_uuid}.txt")
                    except Exception:
                        pass

        try:
            writer.close()
        except Exception:
            pass

# ── Housekeeping ──────────────────────────────────────────────────────────────

async def cleanup_calls():
    while True:
        await asyncio.sleep(60)
        async with calls_lock:
            stale = [
                k for k, v in list(calls.items())
                if not v.get("caller") and not v.get("callee")
            ]
            for k in stale:
                calls.pop(k, None)
                try:
                    os.remove(f"/tmp/call_{k}.txt")
                except Exception:
                    pass
                log.info(f"[cleanup] removed stale call {k}")

async def main():
    if not OPENAI_API_KEY:
        log.error("OPENAI_API_KEY not set!")
        return
    server = await asyncio.start_server(handle_connection, HOST, PORT)
    log.info(f"Translation server listening on {HOST}:{PORT}")
    asyncio.create_task(cleanup_calls())

    async def _metrics_loop():
        while True:
            await asyncio.sleep(60)
            metrics.tick()

    asyncio.create_task(_metrics_loop())
    async with server:
        await server.serve_forever()

if __name__ == "__main__":
    asyncio.run(main())

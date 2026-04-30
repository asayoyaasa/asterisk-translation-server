import asyncio, websockets, json, os, base64, logging, glob, struct, uuid as uuid_mod
from collections import deque
from datetime import datetime, timezone
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

OPENAI_MODEL  = os.environ.get("OPENAI_MODEL", "gpt-4o-realtime-preview-2024-12-17")
OPENAI_WS_URL = f"wss://api.openai.com/v1/realtime?model={OPENAI_MODEL}"

MSG_UUID, MSG_AUDIO, MSG_HANGUP = 0x01, 0x10, 0xff
SILENCE_PAYLOAD = bytes(320)
SILENCE = struct.pack(">BH", MSG_AUDIO, 320) + SILENCE_PAYLOAD
HANGUP_FRAME = struct.pack(">BH", MSG_HANGUP, 0)

# ── Local silence detection for staging segmentation ─────────────────────────
SILENCE_THRESHOLD = 300       # mean amplitude threshold for silence
SILENCE_DURATION_CHUNKS = 16  # ~320ms to reduce premature segmentation on natural pauses
MIN_AUDIO_CHUNKS = 8         # ~160ms minimum to avoid tiny commits that often transcribe empty
MIN_VOICED_CHUNKS = 3        # Require ~60ms of voiced audio before treating noise as speech
TRANSCRIPT_WAIT_SECONDS = 5.0

def is_silent_chunk(audio, threshold=SILENCE_THRESHOLD):
    """Check if a 20ms audio chunk is silence based on mean amplitude."""
    samples = np.frombuffer(audio, dtype=np.int16)
    return np.abs(samples).mean() < threshold

def trim_trailing_silence(chunks):
    """Remove trailing silence chunks so OpenAI only gets actual speech."""
    while chunks and is_silent_chunk(chunks[-1]):
        chunks.pop()
    return chunks
# ──────────────────────────────────────────────────────────────────────────────

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

async def ringback_loop(writer, writer_lock, stop_event):
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
                async with writer_lock:
                    writer.write(build_frame(chunk))
                    await writer.drain()
            except:
                return
            await asyncio.sleep(0.018)

async def keepalive(writer, writer_lock, stop_event):
    while not stop_event.is_set():
        try:
            async with writer_lock:
                writer.write(SILENCE)
                await writer.drain()
        except: break
        await asyncio.sleep(0.5)

async def read_frame(reader, timeout=None):
    h = await asyncio.wait_for(reader.readexactly(3), timeout=timeout)
    t, l = struct.unpack(">BH", h)
    p = await reader.readexactly(l) if l > 0 else b""
    return t, p

async def send_hangup(writer, writer_lock=None):
    try:
        if writer_lock:
            async with writer_lock:
                writer.write(HANGUP_FRAME)
                await writer.drain()
        else:
            writer.write(HANGUP_FRAME)
            await writer.drain()
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

# ── Prompt Management ────────────────────────────────────────────────────────

PROMPT_CACHE: dict = {}
BASE_PROMPT: str = ""

LANGUAGE_HINTS = {
    "ja": "Use natural spoken Japanese. Avoid overly formal keigo.",
    "zh": "Use concise Simplified Chinese.",
    "es": "Use neutral Latin American Spanish.",
    "id": "Use natural conversational Indonesian.",
    "en": "Use natural spoken English.",
}

def load_base_prompt() -> str:
    """Load system prompt from file for better translation instructions."""
    try:
        with open("prompt.md", "r", encoding="utf-8") as f:
            return f.read()
    except:
        return "You are a professional telephone interpreter. Translate accurately and concisely."

def get_prompt(src_lang: str, dst_lang: str) -> str:
    """Get cached or generate language-specific prompt."""
    key = f"{src_lang}-{dst_lang}"
    if key in PROMPT_CACHE:
        return PROMPT_CACHE[key]
    extra = LANGUAGE_HINTS.get(dst_lang.lower(), "Use natural, spoken-style translation.")
    prompt = BASE_PROMPT.format(src_lang=src_lang, dst_lang=dst_lang, extra_rules=extra) if "{src_lang}" in BASE_PROMPT else BASE_PROMPT
    PROMPT_CACHE[key] = prompt
    log.info(f"Prompt cached for {src_lang}->{dst_lang}")
    return prompt

BASE_PROMPT = load_base_prompt()

# ── Dashboard event stream ───────────────────────────────────────────────────

DASHBOARD_EVENT_LOG = os.environ.get(
    "TRANSLATION_DASHBOARD_EVENT_LOG",
    "/opt/voipagent-translate/data/translation-events.jsonl",
)

def _dashboard_ts() -> str:
    return datetime.now(timezone.utc).isoformat()

def emit_dashboard_event(event_type: str, **payload):
    """Append structured dashboard events without affecting the live bridge."""
    try:
        if not DASHBOARD_EVENT_LOG:
            return
        event = {"type": event_type, "ts": _dashboard_ts(), **payload}
        parent = os.path.dirname(DASHBOARD_EVENT_LOG)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(DASHBOARD_EVENT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning(f"dashboard event write failed: {e}")

# ── Metrics ──────────────────────────────────────────────────────────────────

class Metrics:
    """Track translation quality metrics."""
    def __init__(self):
        self.played   = 0  # Audio chunks sent to callee
        self.filtered = 0  # Translations blocked by filters
        self.dupes    = 0  # Duplicate translations
        self._t       = None

    def tick(self):
        """Log metrics every 60 seconds."""
        try:
            now = asyncio.get_running_loop().time()
        except RuntimeError:
            return
        if self._t is None:
            self._t = now
            return
        if now - self._t >= 60:
            log.info(f"[METRICS] played={self.played} filtered={self.filtered} dupes={self.dupes}")
            self._t = now

metrics = Metrics()

# ── Hallucination & Duplicate Filtering ──────────────────────────────────────

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

def _normalize(t: str) -> str:
    """Normalize text for duplicate detection."""
    import re
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", t.lower().strip()))

def is_duplicate(text: str, now: float, last_tx_text_ref, last_tx_time_ref) -> bool:
    """Detect if text is a duplicate of recent translation."""
    prev = last_tx_text_ref[0]
    if not prev or (now - last_tx_time_ref[0]) > 8.0:
        return False
    t1, t2 = _normalize(text), _normalize(prev)
    if t1 == t2:
        return True
    if len(t1) > 10 and len(t2) > 10:
        s1, s2 = set(t1.split()), set(t2.split())
        if s1 and s2 and len(s1 & s2) / max(len(s1), len(s2)) > 0.85:
            return True
    return False

def post_filter(text: str, original: str, now: float, last_tx_text_ref, last_tx_time_ref) -> tuple:
    """Filter hallucinations, assistant phrases, and duplicates."""
    if not text:
        return True, "empty"

    # Filter out [SKIP] tokens which should not be spoken
    if text.lower().strip() == "[skip]":
        return True, "skip"

    src_words = len([w for w in original.strip().split() if w])
    out_words = len([w for w in text.strip().split() if w])

    # Prevent overly brief translations for substantial source utterances.
    if src_words >= 3 and out_words <= 1:
        return True, "too short"  # likely hallucination or ASR mismatch

    tl = text.lower()
    ol = original.lower()
    if any(p in tl for p in HALLUCINATED):
        return True, "hallucination"
    if (any(p in tl for p in ASSISTANT_PHRASES)
            and not any(p in ol for p in ASSISTANT_PHRASES)):
        return True, "assistant phrase"
    if is_duplicate(text, now, last_tx_text_ref, last_tx_time_ref):
        return True, "duplicate"
    return False, ""

async def one_way_bridge(label, call_id, cid, dest, src_queue, dst_writer, dst_lock, src_alive_fn, dst_alive_fn, src_lang, dst_lang, speaking_flag, peer_speaking_flag=None):
    """
    Queue-based bridge with continuous speech support.

    Caller can keep talking without waiting for TTS to finish:
    - Each natural pause (~360ms) segments an utterance
    - Utterances queue up and translate sequentially
    - Caller never blocks; callee hears translations back-to-back

    Flow:
      1. collector drains src_queue; when TTS is playing, it buffers audio
         locally and uses silence detection to segment utterances
      2. Each segment is pushed to utterance_queue
      3. sender picks segments one-by-one, replays to OpenAI, waits for
         translation to finish, then picks the next
      4. pipe_out plays TTS audio to dst, sets utterance_done when complete
      5. conversation items are deleted after each response to prevent
         hallucination from accumulated context
    """
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "OpenAI-Beta": "realtime=v1"}
    src_lang_code = LANG_CODES.get(src_lang.lower(), None)
    last_tx_text = [""]
    last_tx_time = [0.0]
    direction_key = "caller_to_callee" if label == "caller→callee" else "callee_to_caller"

    # Queue that holds complete utterance buffers waiting to be translated.
    # Each item is a list of raw 8kHz PCM chunks captured while the caller spoke.
    utterance_queue = asyncio.Queue()

    # Signals that the current translation finished playing + cooldown elapsed
    utterance_done = asyncio.Event()
    utterance_done.set()  # start open — first utterance can go immediately

    try:
        async with websockets.connect(OPENAI_WS_URL, additional_headers=headers) as ws:
            response_active = asyncio.Event()
            response_active.set()
            response_create_lock = asyncio.Lock()
            pending_item_ids = []
            last_original_text = [""]
            committed_input_items = deque()
            committed_input_item_ready = asyncio.Event()
            transcripts_by_item = {}
            transcript_waiters = {}

            async def delete_item_ids(item_ids, reason: str):
                if not item_ids:
                    return
                items_to_delete = list(dict.fromkeys(item_ids))
                for item_id in items_to_delete:
                    try:
                        pending_item_ids.remove(item_id)
                    except ValueError:
                        pass
                    try:
                        await ws.send(json.dumps({
                            "type": "conversation.item.delete",
                            "item_id": item_id
                        }))
                    except:
                        pass
                log.info(f"[{label}] deleted {len(items_to_delete)} conversation items ({reason})")

            async def delete_pending_items(reason: str):
                await delete_item_ids(list(pending_item_ids), reason)

            async def wait_for_next_committed_item(timeout: float = 1.0):
                if committed_input_items:
                    return committed_input_items.popleft()
                committed_input_item_ready.clear()
                await asyncio.wait_for(committed_input_item_ready.wait(), timeout=timeout)
                item_id = committed_input_items.popleft()
                if not committed_input_items:
                    committed_input_item_ready.clear()
                return item_id

            async def wait_for_transcript(item_id: str, timeout: float) -> str:
                if item_id in transcripts_by_item:
                    return transcripts_by_item[item_id]
                waiter = transcript_waiters.get(item_id)
                if waiter is None:
                    waiter = asyncio.Event()
                    transcript_waiters[item_id] = waiter
                await asyncio.wait_for(waiter.wait(), timeout=timeout)
                return transcripts_by_item.get(item_id, "")

            async def create_response(trigger: str) -> bool:
                try:
                    item_id = await wait_for_next_committed_item()
                except asyncio.TimeoutError:
                    log.warning(f"[{label}] committed item timeout after {trigger}")
                    return False

                try:
                    src_text = (await wait_for_transcript(item_id, TRANSCRIPT_WAIT_SECONDS)).strip()
                except asyncio.TimeoutError:
                    log.warning(f"[{label}] transcript timeout after {trigger}")
                    await delete_item_ids([item_id], "transcript timeout")
                    last_original_text[0] = ""
                    transcript_waiters.pop(item_id, None)
                    transcripts_by_item.pop(item_id, None)
                    return False

                transcript_waiters.pop(item_id, None)
                transcripts_by_item.pop(item_id, None)
                last_original_text[0] = src_text

                if not src_text:
                    log.warning(f"[{label}] dropping utterance with empty transcript after {trigger}")
                    await delete_item_ids([item_id], "empty transcript")
                    return False

                async with response_create_lock:
                    await response_active.wait()
                    response_active.clear()
                    try:
                        await ws.send(json.dumps({"type": "response.create"}))
                        return True
                    except Exception:
                        response_active.set()
                        raise

            def queue_utterance(chunks, reason: str):
                if not chunks:
                    return False
                if len(chunks) < MIN_AUDIO_CHUNKS:
                    log.info(f"[{label}] dropped short utterance ({len(chunks)} chunks, {reason})")
                    return False
                utterance_queue.put_nowait(chunks)
                log.info(f"[{label}] queued utterance ({len(chunks)} chunks, queue: {utterance_queue.qsize()}, {reason})")
                return True

            await ws.send(json.dumps({
                "type": "session.update",
                "session": {
                    "modalities": ["audio", "text"],
                    "instructions": get_prompt(src_lang, dst_lang),
                    "voice": "marin",
                    "temperature": 0.6,
                    "turn_detection": None,
                    "input_audio_format": "pcm16",
                    "output_audio_format": "pcm16",
                    "input_audio_transcription": {"model": "whisper-1", "language": src_lang_code},

                }
            }))
            log.info(f"[{label}] OpenAI ready ({src_lang} → {dst_lang})")

            # ── collector: captures utterances from the raw audio queue ──────
            # When TTS is NOT playing: audio flows directly to OpenAI
            #   (server VAD handles segmentation)
            # When TTS IS playing: audio is buffered locally, local silence
            #   detection segments it into separate utterances pushed to
            #   utterance_queue
            async def collector():
                staging = []
                silent_count = 0
                has_speech = False
                staging_voiced_chunks = 0

                vadsilent_count = 0
                pending_audio_chunks = 0
                voiced_audio_chunks = 0
                last_audio_time = asyncio.get_event_loop().time()
                while src_alive_fn():
                    try:
                        audio = await asyncio.wait_for(src_queue.get(), timeout=0.5)
                        last_audio_time = asyncio.get_event_loop().time()
                    except asyncio.TimeoutError:
                        # Flush any leftover staging on timeout if TTS finished
                        if staging and has_speech and utterance_done.is_set():
                            trimmed = trim_trailing_silence(list(staging))
                            queue_utterance(trimmed, "flush on timeout")
                            staging = []
                            has_speech = False
                            staging_voiced_chunks = 0
                            silent_count = 0
                        # Timeout commit for live speech
                        now = asyncio.get_event_loop().time()
                        if has_speech and now - last_audio_time > 1.0 and pending_audio_chunks >= MIN_AUDIO_CHUNKS:
                            try:
                                last_original_text[0] = ""
                                await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                                log.info(f"[{label}] timeout commit after {now - last_audio_time:.1f}s")
                                await create_response("timeout commit")
                            except Exception as e:
                                log.warning(f"[{label}] timeout commit failed: {e}")
                            vadsilent_count = 0
                            has_speech = False
                            voiced_audio_chunks = 0
                            pending_audio_chunks = 0
                        continue
                    except Exception as e:
                        log.error(f"[{label}] collector: {e}")
                        return

                    suppressed = not utterance_done.is_set()

                    if suppressed:
                        # TTS is playing — buffer locally and segment by silence
                        staging.append(audio)

                        if is_silent_chunk(audio):
                            silent_count += 1
                            # Enough silence after speech? → segment break
                            if has_speech and silent_count >= SILENCE_DURATION_CHUNKS:
                                trimmed = trim_trailing_silence(list(staging))
                                queue_utterance(trimmed, "segmented while suppressed")
                                staging = []
                                has_speech = False
                                staging_voiced_chunks = 0
                                silent_count = 0
                        else:
                            silent_count = 0
                            staging_voiced_chunks += 1
                            if staging_voiced_chunks >= MIN_VOICED_CHUNKS:
                                has_speech = True
                    else:
                        # Not suppressed — flush any leftover staging first
                        if staging and has_speech:
                            trimmed = trim_trailing_silence(list(staging))
                            queue_utterance(trimmed, "flush after suppression")
                            staging = []
                            has_speech = False
                            staging_voiced_chunks = 0
                            silent_count = 0
                        elif staging:
                            # Had staging but no speech (just silence) — discard
                            staging = []
                            staging_voiced_chunks = 0
                            silent_count = 0

                        # Normal path: live speech, send straight to OpenAI
                        try:
                            silent = is_silent_chunk(audio)
                            audio24 = resample_up(audio)
                            await ws.send(json.dumps({
                                "type": "input_audio_buffer.append",
                                "audio": base64.b64encode(audio24).decode()
                            }))

                            pending_audio_chunks += 1
                            if not silent:
                                voiced_audio_chunks += 1
                                if voiced_audio_chunks >= MIN_VOICED_CHUNKS and not has_speech:
                                    log.info(f"[{label}] speech detected")
                                    has_speech = True
                                if voiced_audio_chunks >= MIN_VOICED_CHUNKS:
                                    vadsilent_count = 0
                            else:
                                if voiced_audio_chunks > 0:
                                    vadsilent_count += 1

                            # If we only caught a tiny noise burst and then silence,
                            # clear it before it turns into an empty commit.
                            if (not has_speech and voiced_audio_chunks > 0
                                    and vadsilent_count >= SILENCE_DURATION_CHUNKS):
                                log.info(f"[{label}] dropped tentative speech ({voiced_audio_chunks} voiced chunks)")
                                try:
                                    await ws.send(json.dumps({"type": "input_audio_buffer.clear"}))
                                except:
                                    pass
                                vadsilent_count = 0
                                voiced_audio_chunks = 0
                                pending_audio_chunks = 0
                                last_original_text[0] = ""
                                continue

                            # Commit only after speech has been observed, with at least 100ms of audio
                            if has_speech and pending_audio_chunks >= MIN_AUDIO_CHUNKS and vadsilent_count >= SILENCE_DURATION_CHUNKS:
                                try:
                                    last_original_text[0] = ""
                                    await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                                    log.info(f"[{label}] local silence commit: {vadsilent_count} silent chunks (buf {pending_audio_chunks} chunks)")
                                    await create_response("local silence commit")
                                except Exception as e:
                                    log.warning(f"[{label}] local commit failed: {e}")
                                vadsilent_count = 0
                                has_speech = False
                                voiced_audio_chunks = 0
                                pending_audio_chunks = 0

                        except Exception as e:
                            log.error(f"[{label}] collector send: {e}")
                            return

            # ── sender: drains utterance_queue, replays buffered utterances ──
            async def sender():
                while src_alive_fn():
                    try:
                        chunks = await asyncio.wait_for(utterance_queue.get(), timeout=0.5)
                    except asyncio.TimeoutError:
                        continue
                    except Exception as e:
                        log.error(f"[{label}] sender: {e}")
                        return

                    # Wait until current translation has finished + cooldown
                    await utterance_done.wait()
                    await response_active.wait()
                    utterance_done.clear()  # claim the slot

                    queued = utterance_queue.qsize()
                    log.info(f"[{label}] replaying queued utterance ({len(chunks)} chunks, {queued} more in queue)")

                    # Clear OpenAI's buffer then replay the buffered speech
                    try:
                        await ws.send(json.dumps({"type": "input_audio_buffer.clear"}))
                        for chunk in chunks:
                            audio24 = resample_up(chunk)
                            await ws.send(json.dumps({
                                "type": "input_audio_buffer.append",
                                "audio": base64.b64encode(audio24).decode()
                            }))
                        # Manually commit so VAD doesn't need to re-detect silence
                        last_original_text[0] = ""
                        await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                        created = await create_response("queued replay")
                        if not created:
                            utterance_done.set()
                    except Exception as e:
                        log.error(f"[{label}] sender replay: {e}")
                        utterance_done.set()  # unblock on error

            # ── pipe_out: receives OpenAI events, plays audio to dst ─────────
            async def pipe_out():
                auto_reset_task = None
                response_done_fallback_task = None
                response_watchdog_task = None
                response_audio_seen = [False]
                transcript_decided = asyncio.Event()
                current_response_blocked = [True]
                current_response_reason = [""]
                pending_audio_chunks = []
                response_audio_done_seen = [False]
                response_done_seen = [False]
                response_finalized = [False]
                response_finish_task = None
                response_serial = 0
                current_response_id = [None]
                current_response_text = [""]

                async def play_buffered_audio():
                    if not pending_audio_chunks or not dst_alive_fn():
                        return 0
                    frames_sent = 0
                    async with dst_lock:
                        try:
                            for chunk24 in pending_audio_chunks:
                                audio8 = resample_down(chunk24)
                                for i in range(0, len(audio8), 320):
                                    fd = audio8[i:i+320]
                                    if len(fd) < 320:
                                        fd += b"\x00" * (320 - len(fd))
                                    dst_writer.write(build_frame(fd))
                                    await dst_writer.drain()
                                    await asyncio.sleep(0.019)
                                    frames_sent += 1
                            for _ in range(3):
                                dst_writer.write(build_frame(b"\x00" * 320))
                                await dst_writer.drain()
                                await asyncio.sleep(0.019)
                        except Exception as e:
                            log.error(f"[{label}] audio write failed: {e}")
                    pending_audio_chunks.clear()
                    return frames_sent

                async def finalize_response(no_audio: bool = False):
                    nonlocal auto_reset_task, response_done_fallback_task, response_finish_task, response_watchdog_task
                    if response_finalized[0]:
                        return
                    response_finalized[0] = True

                    if auto_reset_task and not auto_reset_task.done():
                        auto_reset_task.cancel()
                    if response_done_fallback_task and not response_done_fallback_task.done():
                        response_done_fallback_task.cancel()
                    if response_finish_task and not response_finish_task.done():
                        response_finish_task.cancel()
                    if response_watchdog_task and not response_watchdog_task.done():
                        response_watchdog_task.cancel()

                    items_to_delete = list(pending_item_ids)
                    pending_item_ids.clear()

                    if (not no_audio
                            and not current_response_blocked[0]
                            and pending_audio_chunks
                            and dst_alive_fn()):
                        speaking_flag[0] = True
                        # Emit playback event BEFORE audio starts so the dashboard
                        # karaoke timer begins in real time, not after audio finishes.
                        estimated_frames = sum(
                            len(resample_down(c)) // 320 + (1 if len(resample_down(c)) % 320 else 0)
                            for c in pending_audio_chunks
                        )
                        if current_response_id[0] and current_response_text[0] and estimated_frames > 0:
                            emit_dashboard_event(
                                "utterance.playback",
                                callId=call_id,
                                cid=cid,
                                dest=dest,
                                direction=label,
                                srcLang=src_lang,
                                dstLang=dst_lang,
                                responseId=current_response_id[0],
                                text=current_response_text[0],
                                playbackMs=estimated_frames * 20,
                            )
                        frames_sent = await play_buffered_audio()
                        log.info(f"[{label}] sent {frames_sent} frames to dst")
                    else:
                        pending_audio_chunks.clear()
                        speaking_flag[0] = False

                    response_active.set()

                    async def delayed_clear(flag=speaking_flag, lbl=label, _ws=ws, _items=items_to_delete):
                        try:
                            await _ws.send(json.dumps({"type": "input_audio_buffer.clear"}))
                        except:
                            pass

                        if _items:
                            pending_item_ids[:0] = _items
                            await delete_pending_items("response audio done")

                        await asyncio.sleep(0.55)
                        flag[0] = False
                        utterance_done.set()
                        log.info(f"[{lbl}] audio done → ready for next utterance (after 550ms cooldown)")

                    auto_reset_task = asyncio.create_task(delayed_clear())

                async def maybe_finalize_response():
                    if response_finalized[0]:
                        return
                    if response_audio_done_seen[0] and transcript_decided.is_set():
                        await finalize_response(no_audio=False)
                    elif response_done_seen[0] and not pending_audio_chunks and transcript_decided.is_set():
                        await finalize_response(no_audio=True)

                async def finish_after_timeout():
                    try:
                        await asyncio.sleep(2.5)
                        if response_finalized[0]:
                            return
                        if not transcript_decided.is_set():
                            current_response_blocked[0] = True
                            current_response_reason[0] = "undecided"
                            log.warning(f"[{label}] transcript decision still missing; dropping buffered audio")
                        await finalize_response(no_audio=not pending_audio_chunks)
                    except asyncio.CancelledError:
                        pass

                async for msg in ws:
                    try:
                        event = json.loads(msg)
                        etype = event.get("type", "")

                        if etype == "input_audio_buffer.speech_started":
                            # Only cancel if the OTHER side's TTS might be
                            # leaking back as echo into our source microphone
                            if peer_speaking_flag and peer_speaking_flag[0]:
                                log.info(f"[{label}] echo detected (peer playing TTS) — cancelling")
                                try:
                                    await ws.send(json.dumps({"type": "response.cancel"}))
                                    await ws.send(json.dumps({"type": "input_audio_buffer.clear"}))
                                except: pass
                                speaking_flag[0] = False
                                utterance_done.set()
                            elif speaking_flag[0]:
                                # OUR side is playing TTS to dst — source speech
                                # is legitimate new input, collector is staging it
                                log.info(f"[{label}] speech while TTS playing — buffering (not cancelling)")
                            else:
                                log.info(f"[{label}] speech detected")

                        elif etype == "conversation.item.created":
                            # Track every conversation item for cleanup
                            item_id = event.get("item", {}).get("id")
                            if item_id:
                                pending_item_ids.append(item_id)

                        elif etype == "input_audio_buffer.committed":
                            item_id = event.get("item_id")
                            if item_id:
                                committed_input_items.append(item_id)
                                committed_input_item_ready.set()

                        elif etype == "conversation.item.input_audio_transcription.completed":
                            item_id = event.get("item_id")
                            src_text = event.get('transcript','').strip()
                            if item_id:
                                transcripts_by_item[item_id] = src_text
                                waiter = transcript_waiters.get(item_id)
                                if waiter:
                                    waiter.set()
                            last_original_text[0] = src_text
                            log.info(f"[{label}] ORIGINAL : {src_text}")
                            if src_text:
                                emit_dashboard_event(
                                    "utterance.original",
                                    callId=call_id,
                                    cid=cid,
                                    dest=dest,
                                    direction=label,
                                    srcLang=src_lang,
                                    dstLang=dst_lang,
                                    text=src_text,
                                )

                        elif etype == "response.audio_transcript.done":
                            text = event.get('transcript', '').strip()
                            now = asyncio.get_event_loop().time()
                            original = last_original_text[0]
                            blocked, reason = post_filter(text, original, now, last_tx_text, last_tx_time)
                            current_response_blocked[0] = blocked
                            current_response_reason[0] = reason
                            transcript_decided.set()
                            if blocked:
                                current_response_text[0] = ""
                                log.warning(f"[{label}] FILTERED [{reason}]: {text!r}")
                                metrics.filtered += 1
                                emit_dashboard_event(
                                    "utterance.filtered",
                                    callId=call_id,
                                    cid=cid,
                                    dest=dest,
                                    direction=label,
                                    srcLang=src_lang,
                                    dstLang=dst_lang,
                                    responseId=current_response_id[0],
                                    text=text,
                                    reason=reason,
                                    original=original,
                                )
                            else:
                                current_response_text[0] = text
                                log.info(f"[{label}] TRANSLATED: {text}")
                                last_tx_text[0] = text
                                last_tx_time[0] = now
                                emit_dashboard_event(
                                    "utterance.translated",
                                    callId=call_id,
                                    cid=cid,
                                    dest=dest,
                                    direction=label,
                                    srcLang=src_lang,
                                    dstLang=dst_lang,
                                    responseId=current_response_id[0],
                                    text=text,
                                    original=original,
                                )
                            await maybe_finalize_response()

                        elif etype == "response.audio_transcript.delta":
                            pass

                        elif etype == "response.created":
                            response_serial += 1
                            current_response_id[0] = f"{call_id}:{direction_key}:{response_serial}"
                            current_response_text[0] = ""
                            response_audio_seen[0] = False
                            response_audio_done_seen[0] = False
                            response_done_seen[0] = False
                            response_finalized[0] = False
                            current_response_blocked[0] = True
                            current_response_reason[0] = "pending"
                            transcript_decided.clear()
                            pending_audio_chunks.clear()
                            if response_done_fallback_task and not response_done_fallback_task.done():
                                response_done_fallback_task.cancel()
                            if response_finish_task and not response_finish_task.done():
                                response_finish_task.cancel()
                            # Watchdog: if OpenAI goes silent for 8s after response.created,
                            # force-finalize so the bridge doesn't hang indefinitely.
                            if response_watchdog_task and not response_watchdog_task.done():
                                response_watchdog_task.cancel()
                            async def _response_watchdog():
                                try:
                                    await asyncio.sleep(8.0)
                                    if not response_finalized[0]:
                                        log.warning(f"[{label}] response watchdog: no completion after 8s, forcing finalize")
                                        await finalize_response(no_audio=not pending_audio_chunks)
                                except asyncio.CancelledError:
                                    pass
                            response_watchdog_task = asyncio.create_task(_response_watchdog())
                            # Suppress collector EARLY — as soon as OpenAI starts generating
                            # a response, switch to staging so no new audio leaks into the
                            # input buffer and gets lost when we clear it later.
                            if not speaking_flag[0]:
                                utterance_done.clear()
                                try:
                                    await ws.send(json.dumps({"type": "input_audio_buffer.clear"}))
                                except: pass

                        elif etype == "response.audio.delta":
                            chunk = base64.b64decode(event.get("delta", ""))
                            if chunk and dst_alive_fn():
                                response_audio_seen[0] = True
                                if response_done_fallback_task and not response_done_fallback_task.done():
                                    response_done_fallback_task.cancel()
                                pending_audio_chunks.append(chunk)

                        elif etype == "response.audio.done":
                            response_audio_seen[0] = True
                            response_audio_done_seen[0] = True
                            if response_finish_task and not response_finish_task.done():
                                response_finish_task.cancel()
                            response_finish_task = asyncio.create_task(finish_after_timeout())
                            await maybe_finalize_response()

                        elif etype == "response.done":
                            response_done_seen[0] = True
                            if response_done_fallback_task and not response_done_fallback_task.done():
                                response_done_fallback_task.cancel()
                            async def delayed_response_done_fallback():
                                nonlocal response_finish_task
                                try:
                                    await asyncio.sleep(0.4)
                                    await maybe_finalize_response()
                                    if response_finalized[0]:
                                        return
                                    if response_finish_task and not response_finish_task.done():
                                        response_finish_task.cancel()
                                    response_finish_task = asyncio.create_task(finish_after_timeout())
                                except asyncio.CancelledError:
                                    pass

                            response_done_fallback_task = asyncio.create_task(delayed_response_done_fallback())

                        elif etype == "error":
                            err = event.get("error", {})
                            err_code = err.get("code")
                            if err_code == "input_audio_buffer_commit_empty":
                                log.warning(f"[{label}] OpenAI commit empty (ignored): {err.get('message')}")
                                continue
                            if err_code == "conversation_already_has_active_response":
                                log.warning(f"[{label}] OpenAI active response still running; waiting for completion")
                                continue

                            log.error(f"[{label}] OpenAI error: {event}")
                            response_active.set()
                            # Clean up any tracked items on error
                            await delete_pending_items("error")
                            speaking_flag[0] = False
                            utterance_done.set()  # unblock queue on error

                    except Exception as e:
                        log.error(f"[{label}] pipe_out: {e}")

            await asyncio.gather(collector(), sender(), pipe_out())
    except Exception as e:
        log.error(f"[{label}] bridge error: {e}")

async def run_bridge(caller_uuid):
    call = calls.get(caller_uuid)
    if not call: return
    ci = call["caller"]
    ce = call["callee"]
    if not ci or not ce: log.error("Bridge aborted - missing legs"); return

    lang = call["lang"]
    cid = call.get("cid", "")
    dest = call.get("dest", "")
    log.info(f"=== BRIDGE ACTIVE EN <-> {lang} ===")
    emit_dashboard_event(
        "call.ready",
        callId=caller_uuid,
        callerUuid=call.get("caller_uuid", caller_uuid),
        calleeUuid=call.get("callee_uuid", ""),
        cid=cid,
        dest=dest,
        lang=lang,
    )

    caller_lock = ci["lock"]
    callee_lock = ce["lock"]
    callee_speaking = [False]
    caller_speaking = [False]

    async def keepalive_leg(conn, writer_lock):
        while conn.get("alive"):
            try:
                async with writer_lock:
                    conn["writer"].write(SILENCE)
                    await conn["writer"].drain()
            except:
                return
            await asyncio.sleep(0.5)

    await asyncio.gather(
        one_way_bridge("caller→callee", caller_uuid, cid, dest, ci["queue"], ce["writer"], callee_lock,
                       lambda: ci.get("alive", False), lambda: ce.get("alive", False),
                       "English", lang, callee_speaking, caller_speaking),
        one_way_bridge("callee→caller", caller_uuid, cid, dest, ce["queue"], ci["writer"], caller_lock,
                       lambda: ce.get("alive", False), lambda: ci.get("alive", False),
                       lang, "English", caller_speaking, callee_speaking),
        keepalive_leg(ci, caller_lock),
        keepalive_leg(ce, callee_lock),
    )

async def handle_connection(reader, writer):
    peer = writer.get_extra_info("peername")
    log.info(f"New connection from {peer}")
    disconnect_reason = "disconnect"
    try:
        t, p = await read_frame(reader, timeout=10)
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

    queue = asyncio.Queue()
    writer_lock = asyncio.Lock()
    stop_ka = asyncio.Event()
    conn = {"queue": queue, "writer": writer, "lock": writer_lock, "alive": True}

    # setdefault: never overwrite existing entry (fixes race where callee
    # arrives before caller and caller's init would wipe the callee conn)
    calls.setdefault(caller_uuid, {
        "caller": None,
        "callee": None,
        "lang": lang,
        "cid": cid,
        "dest": dest,
        "caller_uuid": caller_uuid,
        "callee_uuid": callee_uuid,
        "ended_emitted": False,
    })
    calls[caller_uuid]["lang"] = lang
    calls[caller_uuid]["cid"] = cid
    calls[caller_uuid]["dest"] = dest
    calls[caller_uuid]["caller_uuid"] = caller_uuid
    calls[caller_uuid]["callee_uuid"] = callee_uuid
    calls[caller_uuid][role] = conn
    emit_dashboard_event(
        "call.upsert",
        callId=caller_uuid,
        callerUuid=caller_uuid,
        calleeUuid=callee_uuid,
        cid=cid,
        dest=dest,
        lang=lang,
        role=role,
        active=True,
    )

    if role == "caller":
        log.info(f"Caller connected - playing ringback, originating to {dest}")
        asyncio.create_task(ami_originate(callee_uuid, dest, cid))
        ka_task = asyncio.create_task(ringback_loop(writer, writer_lock, stop_ka))
    else:
        ka_task = asyncio.create_task(keepalive(writer, writer_lock, stop_ka))

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
            callee_stop = conn.get("stop_ka_event")
            if callee_stop:
                callee_stop.set()
            asyncio.create_task(run_bridge(caller_uuid))
        else:
            log.info("Callee arrived before caller - waiting")

    elif role == "caller":
        # Late-bridge: callee may have arrived before us (fast answer / retry)
        if calls[caller_uuid]["callee"]:
            log.info("Both legs ready - starting bridge (caller arrived second)")
            stop_ka.set()  # stop ringback immediately
            callee_stop = calls[caller_uuid]["callee"].get("stop_ka_event")
            if callee_stop:
                callee_stop.set()
            asyncio.create_task(run_bridge(caller_uuid))

    try:
        while True:
            t, p = await read_frame(reader, timeout=None)
            if t == MSG_HANGUP:
                log.info(f"{role} hung up")
                disconnect_reason = "hangup"
                break
            if t == MSG_AUDIO and p:
                await queue.put(p)
    except Exception as e:
        disconnect_reason = str(e) or "disconnect"
        log.info(f"{role} ended: {e}")
    finally:
        stop_ka.set()
        conn["alive"] = False
        ka_task.cancel()

        # Hang up the other party
        call = calls.get(caller_uuid, {})
        if call and not call.get("ended_emitted"):
            call["ended_emitted"] = True
            emit_dashboard_event(
                "call.ended",
                callId=caller_uuid,
                callerUuid=call.get("caller_uuid", caller_uuid),
                calleeUuid=call.get("callee_uuid", ""),
                cid=call.get("cid", cid),
                dest=call.get("dest", dest),
                lang=call.get("lang", lang),
                role=role,
                reason=disconnect_reason,
            )
        other_role = "callee" if role == "caller" else "caller"
        other = call.get(other_role)
        if other and other.get("alive"):
            log.info(f"{role} disconnected - hanging up {other_role}")
            other["alive"] = False
            await send_hangup(other["writer"], other.get("lock"))
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
    """Periodically clean up stale call entries."""
    while True:
        await asyncio.sleep(60)
        stale = [k for k, v in list(calls.items())
                 if not v.get("caller") and not v.get("callee")]
        for k in stale:
            calls.pop(k, None)
            try:
                os.remove(f"/tmp/call_{k}.txt")
            except:
                pass
            log.info(f"[cleanup] removed stale call {k}")

async def metrics_loop():
    """Periodically log metrics."""
    while True:
        await asyncio.sleep(60)
        metrics.tick()

async def main():
    if not OPENAI_API_KEY: log.error("OPENAI_API_KEY not set!"); return
    server = await asyncio.start_server(handle_connection, HOST, PORT)
    log.info(f"Listening on {HOST}:{PORT}")
    asyncio.create_task(cleanup_calls())
    asyncio.create_task(metrics_loop())
    async with server: await server.serve_forever()

if __name__ == "__main__":
    asyncio.run(main())

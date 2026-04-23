import asyncio, websockets, json, os, base64, logging, glob, struct, uuid as uuid_mod, re
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

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
HOST, PORT              = "127.0.0.1", 5001
AMI_HOST, AMI_PORT      = "127.0.0.1", 5038
AMI_USER, AMI_SECRET="translation", "TrServer2024!"

OPENAI_MODEL  = "gpt-4o-realtime-preview-2024-12-17"
OPENAI_WS_URL = f"wss://api.openai.com/v1/realtime?model={OPENAI_MODEL}"

MSG_UUID, MSG_AUDIO, MSG_HANGUP = 0x01, 0x10, 0xff
SILENCE_PAYLOAD = bytes(320)
SILENCE         = struct.pack(">BH", MSG_AUDIO, 320) + SILENCE_PAYLOAD
HANGUP_FRAME    = struct.pack(">BH", MSG_HANGUP, 0)

calls_lock = asyncio.Lock()
calls: dict = {}

# ── Simultaneous Interpretation Tuning ─────────────────────────────────────────
# Clause trigger (1.5s + punctuation) is PRIMARY. Time trigger is FALLBACK for:
#   - First utterance (no transcript data yet)
#   - Long speech without natural clause boundaries
# 3s ensures callee's first "Halo?" commits within ~3s of VAD detection.
ROLLING_WINDOW_SECONDS = 3.0
MIN_AUDIO_SECONDS      = 1.5
PREFILL_SECONDS        = 0.5
CLAUSE_STABILITY_MS    = 400
MIN_WORDS_FOR_CLAUSE   = 3
POSTFILTER_MIN_CHARS   = 6

SESSION_TEMPERATURE = 0.6

# ── Audio helpers ────────────────────────────────────────────────────────────────

def build_frame(audio: bytes) -> bytes:
    return struct.pack(">BH", MSG_AUDIO, len(audio)) + audio

def parse_uuid(p: bytes) -> str:
    return (str(uuid_mod.UUID(bytes=p)) if len(p) == 16
            else p.decode("utf-8", "ignore").strip("\x00").strip())

def resample_up(audio: bytes) -> bytes:
    s = np.frombuffer(audio, dtype=np.int16).astype(np.float32)
    return soxr.resample(s, 8000, 24000, quality="VHQ").astype(np.int16).tobytes()

def resample_down(audio: bytes) -> bytes:
    s = np.frombuffer(audio, dtype=np.int16).astype(np.float32)
    return soxr.resample(s, 24000, 8000, quality="VHQ").astype(np.int16).tobytes()

def fast_rms(audio: bytes) -> int:
    s = np.frombuffer(audio, dtype=np.int16)
    return int(np.sqrt((s.astype(np.int32) ** 2).mean()))

# ── Prompt ─────────────────────────────────────────────────────────────────────

PROMPT_CACHE: dict = {}
BASE_PROMPT: str   = ""

LANGUAGE_HINTS = {
    "en":    "Use natural spoken English.",
    "id":    "Use natural conversational Indonesian. Understand colloquial forms: enggak/nggak=tidak, gimana=bagaimana, gue/gw=saya, lu/lo=kamu, dong/sih/deh=discourse particles.",
    "es":    "Use neutral Latin American Spanish.",
    "fr":    "Use natural spoken French, not formal or literary.",
    "de":    "Use natural spoken German, not formal or literary.",
    "pt":    "Use natural spoken Portuguese, not formal or literary.",
    "zh":    "Use concise Simplified Chinese.",
    "ja":    "Use natural spoken Japanese. Avoid overly formal keigo.",
    "ko":    "Use natural spoken Korean.",
    "th":    "Use natural spoken Thai.",
    "vi":    "Use natural spoken Vietnamese.",
    "ar":    "Use natural conversational Arabic, not classical.",
    "hi":    "Use natural spoken Hindustani.",
    "ru":    "Use natural spoken Russian.",
    "tr":    "Use natural spoken Turkish.",
    "pl":    "Use natural spoken Polish.",
    "nl":    "Use natural spoken Dutch.",
    "it":    "Use natural spoken Italian.",
    "uk":    "Use natural spoken Ukrainian.",
    "ro":    "Use natural spoken Romanian.",
    "tl":    "Use natural spoken Filipino.",
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

# ── Infrastructure helpers ──────────────────────────────────────────────────────

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

# ── Language codes ──────────────────────────────────────────────────────────────

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

# ── Metrics ────────────────────────────────────────────────────────────────────

class Metrics:
    def __init__(self):
        self.played           = 0
        self.commits          = 0
        self.filtered         = 0
        self.garbage          = 0
        self.sentinel         = 0
        self.dupes            = 0
        self.dropped          = 0
        self.time_triggers    = 0
        self.clause_triggers  = 0
        self.queue_overflows  = 0
        self.stream_filters   = 0
        self._t               = None

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
                f"filtered={self.filtered} garbage={self.garbage} "
                f"sentinel={self.sentinel} dupes={self.dupes} "
                f"time_triggers={self.time_triggers} "
                f"clause_triggers={self.clause_triggers} "
                f"stream_filters={self.stream_filters}"
            )
            self._t = now

metrics = Metrics()

# ── Garbage / wrong-script detection ──────────────────────────────────────────

_LATIN_SCRIPT_CODES = frozenset({
    'en', 'id', 'ms', 'es', 'fr', 'de', 'pt', 'it', 'nl', 'pl', 'ro',
    'cs', 'sk', 'hr', 'bs', 'sl', 'tr', 'vi', 'tl', 'sw', 'af', 'no',
    'sv', 'da', 'fi', 'et', 'lv', 'lt', 'hu', 'is', 'mt', 'sq', 'ca',
    'cy', 'lb', 'mg', 'so', 'ss', 'st', 'fj', 'rw', 'dv',
})

_NON_LATIN_RANGES = [
    (0x4E00, 0x9FFF,  'cjk'),
    (0x3040, 0x30FF,  'japanese'),
    (0xAC00, 0xD7AF,  'hangul'),
    (0x1100, 0x11FF,  'hangul_jamo'),
    (0x0600, 0x06FF,  'arabic'),
    (0x0590, 0x05FF,  'hebrew'),
    (0x0900, 0x097F,  'devanagari'),
    (0x0980, 0x09FF,  'bengali'),
    (0x0A00, 0x0A7F,  'gujarati'),
    (0x0B80, 0x0BFF,  'tamil'),
    (0x0C00, 0x0C7F,  'telugu'),
    (0x0D00, 0x0D7F,  'malayalam'),
    (0x0E00, 0x0E7F,  'thai'),
    (0x0E80, 0x0EFF,  'lao'),
    (0x1000, 0x109F,  'myanmar'),
    (0x1200, 0x137F,  'ethiopic'),
    (0x10D0, 0x10FF,  'georgian'),
    (0x0530, 0x058F,  'armenian'),
    (0x0400, 0x04FF,  'cyrillic'),
]

_IPA_CHARS = frozenset(
    'əɪʊɛæɔʌɑðθʃʒŋɹɫʔɡɦʤʧɾɬɮʏɐɒɓɕɖɗɘɚɜɝɞɟɠɣɤɥɨɩɪɫɬɭɮɯɰɱɲɳɴɵɶɷɸɹɺɻɼɽɾɿʀʁʂʃʄʅʆʇʈʉʊʋʌʍʎʏʐʑʒʓʙʚʛʜʝʞʟʠʡʢ'
)

def is_garbage_transcript(text: str, src_lang_code: str) -> tuple[bool, str]:
    if not text or not text.strip():
        return False, ""
    alpha_chars = [c for c in text if c.isalpha()]
    if not alpha_chars:
        return False, ""

    if src_lang_code in _LATIN_SCRIPT_CODES:
        for c in text:
            cp = ord(c)
            for lo, hi, name in _NON_LATIN_RANGES:
                if lo <= cp <= hi:
                    return True, f"wrong_script:{name}"
        ipa_count = sum(1 for c in alpha_chars if c in _IPA_CHARS)
        if ipa_count > 0:
            return True, f"ipa_chars ({ipa_count}/{len(alpha_chars)})"
        threshold = 0.05 if src_lang_code == 'en' else 0.20
        unusual = sum(1 for c in alpha_chars if ord(c) > 0x024F)
        if unusual / len(alpha_chars) > threshold:
            return True, f"unusual_diacritics ({unusual}/{len(alpha_chars)})"
    else:
        ipa_count = sum(1 for c in alpha_chars if c in _IPA_CHARS)
        if len(alpha_chars) > 0 and ipa_count / len(alpha_chars) > 0.3:
            return True, f"ipa_contamination ({ipa_count}/{len(alpha_chars)})"

    return False, ""


# ── Pre-warm ──────────────────────────────────────────────────────────────────

async def prewarm_openai_session(
    src_lang: str, dst_lang: str, src_lang_code: str,
    ready_event: asyncio.Event, ws_holder: list, stop_event: asyncio.Event,
):
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
                "temperature": SESSION_TEMPERATURE,
            },
        }))
        log.info(f"[prewarm {src_lang}->{dst_lang}] session.update sent, waiting for ready...")
        async for msg in ws:
            if stop_event.is_set():
                return
            try:
                event = json.loads(msg)
                etype = event.get("type", "")
                if etype == "session.created":
                    log.info(f"[prewarm {src_lang}->{dst_lang}] session created")
                elif etype == "session.updated":
                    log.info(f"[prewarm {src_lang}->{dst_lang}] session ready")
                    ready_event.set()
                    return
                elif etype == "error":
                    log.error(f"[prewarm {src_lang}->{dst_lang}] error: {event}")
                    return
            except Exception as e:
                log.error(f"[prewarm {src_lang}->{dst_lang}] parse error: {e}")
                return
    except asyncio.CancelledError:
        pass
    except Exception as e:
        log.error(f"[prewarm {src_lang}->{dst_lang}] connect failed: {e}")
    finally:
        if ws is not None and ws_holder[0] is ws and not ready_event.is_set():
            try:
                await ws.close()
            except Exception:
                pass
            ws_holder[0] = None

# ── AudioSequencer ────────────────────────────────────────────────────────────

class AudioSequencer:
    def __init__(self, dst_writer, dst_lock, abort_event: asyncio.Event, label: str):
        self.queue = asyncio.Queue(maxsize=50)
        self.writer = dst_writer
        self.lock = dst_lock
        self.abort = abort_event
        self.label = label
        self.playing = False
        self.play_task = None
        self._resp_id = [None]

    def bind_response(self, resp_id: str):
        self._resp_id[0] = resp_id

    async def enqueue(self, audio8: bytes, resp_id: str):
        if self._resp_id[0] != resp_id:
            return
        if self.queue.full():
            try:
                self.queue.get_nowait()
                metrics.dropped += 1
            except asyncio.QueueEmpty:
                pass
        self.queue.put_nowait(audio8)
        if not self.playing:
            self.playing = True
            self.play_task = asyncio.create_task(self._play_loop())

    async def abort_current(self):
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._resp_id[0] = None
        self.abort.set()

    async def _play_loop(self):
        frames_played = 0
        bytes_played = 0
        while True:
            try:
                audio = await asyncio.wait_for(self.queue.get(), timeout=0.3)
            except asyncio.TimeoutError:
                if frames_played > 0:
                    log.info(
                        f"[{self.label}] PLAYBACK_DONE frames={frames_played} "
                        f"bytes={bytes_played} duration={frames_played * 0.02:.2f}s"
                    )
                    metrics.played += frames_played
                self.playing = False
                return

            async with self.lock:
                for i in range(0, len(audio), 320):
                    if self.abort.is_set():
                        self.abort.clear()
                        if frames_played > 0:
                            log.info(f"[{self.label}] PLAYBACK_ABORT after {frames_played} frames")
                        self.playing = False
                        return
                    frame = audio[i:i + 320]
                    if len(frame) < 320:
                        frame += b"\x00" * (320 - len(frame))
                    self.writer.write(build_frame(frame))
                    await self.writer.drain()
                    frames_played += 1
                    bytes_played += 320
                    await asyncio.sleep(0.019)

# ── Streaming Post-Filter ──────────────────────────────────────────────────────

HALLUCINATED_NUMBER_RE = re.compile(r"\$[\d,]+|[\d,]+\s?(million|billion|trillion|percent)")
SKIP_SENTINEL = "[skip]"

ASSISTANT_PHRASES = [
    "how can i help", "how may i help", "how can i assist",
    "how may i assist", "is there anything else", "i'd be happy to",
    "i would be happy to", "i'm here to help", "i am here to help",
    "thank you for calling", "thank you for contacting",
    "have a great day", "have a nice day",
    "let me know if you need", "don't hesitate to",
    "ada yang bisa saya bantu", "ada yang bisa saya",
    "terima kasih telah menghubungi", "terima kasih sudah menghubungi",
    "semoga harimu menyenangkan", "jangan ragu untuk",
    "dengan senang hati saya",
]

HALLUCINATED = [
    "[skip]", "[x]", "[none]", "[no output]", "[no translation]",
    "(there is no output", "(no output", "(nothing to translate",
    "no translation", "nothing to translate", "no output",
    "i cannot translate", "i can't translate", "i cannot hear",
    "there is nothing", "nothing meaningful", "no meaningful",
    "i don't understand", "i can't hear", "i didn't hear",
    "i couldn't hear", "unclear audio", "inaudible",
    "the audio is", "audio quality",
    "tidak ada terjemahan", "tidak ada yang perlu",
    "saya tidak bisa mendengar", "saya tidak mendengar",
    "tidak jelas", "audio tidak jelas", "kualitas audio",
    "maaf saya tidak", "saya tidak dapat menerjemahkan",
    "tidak ada input", "tidak ada ucapan",
    "saya tidak bisa menerjemahkan",
    "maaf, saya tidak bisa", "sorry, i cannot",
    "i apologize", "mohon maaf",
]

def streaming_post_filter(txt: str) -> tuple[bool, str]:
    tl = txt.lower().strip()
    if SKIP_SENTINEL in tl:
        return True, "sentinel"
    for phrase in ASSISTANT_PHRASES:
        if phrase in tl:
            return True, "assistant_phrase"
    if HALLUCINATED_NUMBER_RE.search(txt):
        return True, "hallucinated_number"
    return False, ""

# ── Filler words (for SRC filter) ────────────────────────────────────────────
# Only pure fillers — NOT valid words like "yes", "no", "hello", "hi", "ok"
FILLER_WORDS = {
    "uh", "um", "erm", "hmm", "mm", "mmhm", "mhm", "uhh", "umm",
    "ah", "eh", "uh-huh", "uh huh", "mm-hmm", "mm hmm", "hm", "huh",
    "euh", "ehm",
}

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
    speaking_flag: list,
    peer_speaking_flag: list,
    prewarmed_ws=None,
    stop_event: asyncio.Event = None,
):
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta": "realtime=v1",
    }
    src_lang_code = LANG_CODES.get(src_lang.lower(), "en")

    # Compromise: 30 lets through quiet callee mics on Indonesian mobile,
    # while still filtering most carrier hiss (typically RMS 5-20).
    RMS_SILENCE_FLOOR = 30

    CLAUSE_PUNCTUATION = set('.?!;:,، 。、！？')

    session_ready    = asyncio.Event()
    abort_playback   = asyncio.Event()
    commit_time      = [0.0]

    current_resp_id  = [None]
    user_item_ids    = []
    vad_speech_active = [False]

    resp = {
        "id":               None,
        "audio":            [],
        "pending_audio8":   [],
        "audio_done":       False,
        "transcript":       "",
        "transcript_done":  False,
        "blocked":          False,
        "original":         None,
        "src_ok":           False,
    }

    def reset_resp():
        resp["id"]              = None
        resp["audio"]           = []
        resp["pending_audio8"]  = []
        resp["audio_done"]      = False
        resp["transcript"]      = ""
        resp["transcript_done"] = False
        resp["blocked"]         = False
        resp["original"]        = None
        resp["src_ok"]          = False

    input_transcript      = [""]
    input_transcript_lock = asyncio.Lock()
    clause_candidate_time = [0.0]

    dst_partial_transcript = [""]
    last_delta_time = [0.0]

    last_tx_text = [""]
    last_tx_time = [0.0]

    def _normalize(t: str) -> str:
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

    def post_filter(text: str, original: str, now: float):
        if not text:
            return True, "empty"
        tl = text.lower().strip()
        if SKIP_SENTINEL in tl:
            metrics.sentinel += 1
            return True, "sentinel"
        if original is not None and not original.strip():
            return True, "empty_src"
        ol = original.lower() if original else ""
        if any(p in tl for p in HALLUCINATED):
            return True, "hallucination"
        if (any(p in tl for p in ASSISTANT_PHRASES)
                and not any(p in ol for p in ASSISTANT_PHRASES)):
            return True, "assistant_phrase"
        if is_duplicate(text, now):
            return True, "duplicate"
        return False, ""

    audio_sequencer = AudioSequencer(dst_writer, dst_lock, abort_playback, label)

    async def _run_with_ws(ws):
        _loop = asyncio.get_running_loop()
        _inner_stop = asyncio.Event()

        log.info(f"[{label}] Starting bridge (prewarmed={prewarmed_ws is not None})")

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

        def delete_old_user_items():
            nonlocal user_item_ids
            for uid in user_item_ids:
                ws_send({"type": "conversation.item.delete", "item_id": uid})
            user_item_ids.clear()

        if prewarmed_ws is None:
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
                    "temperature": SESSION_TEMPERATURE,
                },
            }))
            log.info(f"[{label}] session.update sent")
        else:
            log.info(f"[{label}] reusing pre-warmed session ({src_lang}->{dst_lang})")
            ws_send({"type": "input_audio_buffer.clear"})
            delete_old_user_items()
            session_ready.set()
            commit_time[0] = _loop.time()

        async def streamer():
            log.info(f"[{label}] streamer started")
            drained = 0
            while not src_queue.empty():
                try:
                    src_queue.get_nowait()
                    drained += 1
                except asyncio.QueueEmpty:
                    break
            if drained:
                log.info(f"[{label}] drained {drained} pre-bridge audio frames")

            rolling_audio_buffer = bytearray()
            rolling_start_time = [_loop.time()]
            audio_since_commit = 0
            first_audio_time = [None]

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
                    rolling_audio_buffer.clear()
                    audio_since_commit = 0
                    rolling_start_time[0] = _loop.time()
                    first_audio_time[0] = None
                    await asyncio.sleep(0.05)
                    continue

                if first_audio_time[0] is None and rolling_audio_buffer:
                    first_audio_time[0] = _loop.time()

                elapsed = _loop.time() - rolling_start_time[0]
                prefill_elapsed = _loop.time() - (first_audio_time[0] or _loop.time())
                buffer_seconds = audio_since_commit / 16000.0

                time_trigger_ready = (
                    elapsed >= ROLLING_WINDOW_SECONDS
                    and buffer_seconds >= MIN_AUDIO_SECONDS
                    and prefill_elapsed >= PREFILL_SECONDS
                    and vad_speech_active[0]
                )

                clause_trigger_ready = False
                async with input_transcript_lock:
                    transcript = input_transcript[0]
                    cand_time  = clause_candidate_time[0]

                punct_stability = (_loop.time() - cand_time) if cand_time > 0 else 0.0
                has_punctuation = transcript and transcript[-1] in CLAUSE_PUNCTUATION
                word_count = len(transcript.split())

                clause_trigger_ready = (
                    has_punctuation
                    and buffer_seconds >= 1.5
                    and word_count >= MIN_WORDS_FOR_CLAUSE
                    and punct_stability >= (CLAUSE_STABILITY_MS / 1000.0)
                    and elapsed >= 1.0
                    and vad_speech_active[0]
                )

                if (elapsed >= ROLLING_WINDOW_SECONDS
                        and buffer_seconds >= MIN_AUDIO_SECONDS
                        and not vad_speech_active[0]):
                    ws_send({"type": "input_audio_buffer.clear"})
                    rolling_audio_buffer.clear()
                    audio_since_commit = 0
                    rolling_start_time[0] = _loop.time()
                    first_audio_time[0] = None
                    await asyncio.sleep(0.05)
                    continue

                commit_fired = False
                trigger_type = None

                if time_trigger_ready:
                    commit_fired = True
                    trigger_type = "time"
                    metrics.time_triggers += 1
                elif clause_trigger_ready:
                    commit_fired = True
                    trigger_type = "clause"
                    metrics.clause_triggers += 1

                if commit_fired:
                    log.info(
                        f"[{label}] LATENCY_MARK commit_fired "
                        f"trigger={trigger_type} "
                        f"audio_seconds={buffer_seconds:.2f} "
                        f"elapsed={elapsed:.2f} "
                        f"transcript={transcript[-40:]!r}"
                    )
                    delete_old_user_items()
                    ws_send({"type": "input_audio_buffer.commit"})
                    commit_time[0] = _loop.time()

                    rolling_audio_buffer.clear()
                    audio_since_commit = 0
                    rolling_start_time[0] = _loop.time()
                    first_audio_time[0] = None
                    vad_speech_active[0] = False

                    async with input_transcript_lock:
                        clause_candidate_time[0] = 0.0
                        input_transcript[0] = ""

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

                rolling_audio_buffer.extend(audio)
                audio_since_commit += len(audio)

                try:
                    audio24 = await _loop.run_in_executor(None, resample_up, audio)
                    ws_send({
                        "type":  "input_audio_buffer.append",
                        "audio": base64.b64encode(audio24).decode(),
                    })
                except Exception as e:
                    log.error(f"[{label}] streamer resample/append: {e}")
                    return

        async def pipe_out():
            log.info(f"[{label}] pipe_out started")
            try:
                async for raw in ws:
                    if _inner_stop.is_set() or (stop_event and stop_event.is_set()):
                        break
                    try:
                        event = json.loads(raw)
                        etype = event.get("type", "")

                        if etype == "session.updated":
                            log.info(f"[{label}] session.updated - ready")
                            session_ready.set()
                            commit_time[0] = _loop.time()

                        elif etype == "conversation.item.created":
                            item = event.get("item", {})
                            if item.get("type") == "message" and item.get("role") == "user":
                                uid = item.get("id")
                                if uid:
                                    user_item_ids.append(uid)

                        elif etype == "input_audio_buffer.committed":
                            reset_resp()
                            abort_playback.clear()

                            if current_resp_id[0] is not None:
                                ws_send({"type": "response.cancel"})

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
                            resp["id"] = rid
                            audio_sequencer.bind_response(rid)
                            dst_partial_transcript[0] = ""
                            log.info(f"[{label}] response created: {rid}")

                            if resp.get("src_ok") and resp["pending_audio8"]:
                                log.info(f"[{label}] flushing {len(resp['pending_audio8'])} pending audio chunks")
                                for audio8 in resp["pending_audio8"]:
                                    await audio_sequencer.enqueue(audio8, current_resp_id[0])
                                resp["pending_audio8"] = []

                        elif etype == "response.audio.delta":
                            if event.get("response_id") == current_resp_id[0]:
                                chunk = base64.b64decode(event.get("delta", ""))
                                if chunk and not resp.get("blocked"):
                                    resp["audio"].append(chunk)
                                    audio8 = await _loop.run_in_executor(
                                        None, resample_down, chunk
                                    )
                                    if resp.get("src_ok"):
                                        await audio_sequencer.enqueue(
                                            audio8, current_resp_id[0]
                                        )
                                    else:
                                        if len(resp["pending_audio8"]) >= 200:
                                            resp["pending_audio8"].pop(0)
                                        resp["pending_audio8"].append(audio8)

                                    if len(resp["audio"]) == 1:
                                        latency = (
                                            _loop.time() - commit_time[0]
                                            if commit_time[0] else 0
                                        )
                                        log.info(
                                            f"[{label}] LATENCY_MARK first_byte "
                                            f"resp_id={current_resp_id[0]} "
                                            f"commit_to_first_byte={latency:.3f}s "
                                            f"src_ok={resp.get('src_ok')}"
                                        )

                        elif etype == "response.audio.done":
                            if event.get("response_id") == current_resp_id[0]:
                                resp["audio_done"] = True
                                pending = len(resp["pending_audio8"])
                                if pending > 0:
                                    log.warning(
                                        f"[{label}] audio.done with {pending} chunks "
                                        f"still pending (src_ok={resp.get('src_ok')})"
                                    )

                        elif etype == "response.audio_transcript.delta":
                            if event.get("response_id") == current_resp_id[0]:
                                delta = event.get("delta", "")
                                dst_partial_transcript[0] += delta
                                last_delta_time[0] = _loop.time()

                                current_text = dst_partial_transcript[0]
                                if len(current_text) >= POSTFILTER_MIN_CHARS:
                                    blocked, reason = streaming_post_filter(current_text)
                                    if blocked:
                                        resp["blocked"] = True
                                        resp["pending_audio8"] = []
                                        ws_send({"type": "response.cancel"})
                                        await audio_sequencer.abort_current()
                                        metrics.stream_filters += 1
                                        if reason == "sentinel":
                                            metrics.sentinel += 1
                                        log.info(
                                            f"[{label}] STREAM_FILTER [{reason}]: "
                                            f"{current_text!r}"
                                        )

                        elif etype == "conversation.item.input_audio_transcription.completed":
                            src_text = event.get("transcript", "").strip()
                            resp["original"] = src_text
                            log.info(f"[{label}] SRC : {src_text!r}")

                            if not resp.get("blocked"):
                                garbage, garbage_reason = is_garbage_transcript(
                                    src_text, src_lang_code
                                )
                                if garbage:
                                    resp["blocked"] = True
                                    resp["pending_audio8"] = []
                                    ws_send({"type": "response.cancel"})
                                    await audio_sequencer.abort_current()
                                    metrics.garbage += 1
                                    metrics.filtered += 1
                                    log.warning(
                                        f"[{label}] FILTER [garbage:{garbage_reason}]: "
                                        f"{src_text!r}"
                                    )
                                    continue

                            # Filler filter: only catch pure fillers and empty,
                            # NOT valid words like "yes", "hello", "no"
                            norm = re.sub(
                                r"\s+", " ",
                                re.sub(r"[^\w\s-]", "", src_text.lower())
                            ).strip()
                            is_filler = (not norm) or (norm in FILLER_WORDS)
                            if is_filler and not resp.get("blocked"):
                                resp["blocked"] = True
                                resp["pending_audio8"] = []
                                ws_send({"type": "response.cancel"})
                                await audio_sequencer.abort_current()
                                metrics.filtered += 1
                                log.warning(
                                    f"[{label}] FILTER [filler_src]: {norm!r}"
                                )
                                continue

                            if not resp.get("src_ok") and not resp.get("blocked"):
                                resp["src_ok"] = True
                                if current_resp_id[0] is not None and resp["pending_audio8"]:
                                    log.info(f"[{label}] SRC confirmed — releasing {len(resp['pending_audio8'])} pending chunks")
                                    for audio8 in resp["pending_audio8"]:
                                        await audio_sequencer.enqueue(audio8, current_resp_id[0])
                                    resp["pending_audio8"] = []

                            async with input_transcript_lock:
                                input_transcript[0] = src_text
                                if src_text and src_text[-1] in CLAUSE_PUNCTUATION:
                                    if clause_candidate_time[0] == 0.0:
                                        clause_candidate_time[0] = _loop.time()

                        elif etype == "response.audio_transcript.done":
                            if event.get("response_id") == current_resp_id[0]:
                                text = event.get("transcript", "").strip()
                                now  = _loop.time()
                                if resp.get("blocked"):
                                    reset_resp()
                                    continue
                                log.info(f"[{label}] DST : {text!r}")

                                blocked, reason = post_filter(text, resp["original"], now)
                                if blocked:
                                    log.warning(f"[{label}] FILTER [{reason}]: {text!r}")
                                    resp["blocked"] = True
                                    metrics.filtered += 1
                                else:
                                    resp["transcript"] = text
                                    last_tx_text[0]    = text
                                    last_tx_time[0]    = now
                                resp["transcript_done"] = True

                        elif etype == "input_audio_buffer.speech_started":
                            vad_speech_active[0] = True
                            if speaking_flag[0]:
                                log.info(f"[{label}] barge-in -> abort + drain queue")
                                abort_playback.set()
                                speaking_flag[0] = False
                                await audio_sequencer.abort_current()
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
                log.warning(f"[{label}] WebSocket closed: {e}")
            except Exception as e:
                log.error(f"[{label}] pipe_out error: {e}")
            finally:
                log.info(f"[{label}] pipe_out ended")
                _inner_stop.set()

        ws_writer_task = asyncio.create_task(_ws_writer())
        try:
            await asyncio.gather(streamer(), pipe_out())
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

    try:
        if prewarmed_ws is not None:
            log.info(f"[{label}] Using pre-warmed WebSocket")
            await _run_with_ws(prewarmed_ws)
        else:
            log.info(f"[{label}] Opening fresh WebSocket")
            async with websockets.connect(OPENAI_WS_URL, additional_headers=headers) as ws:
                await _run_with_ws(ws)
    except asyncio.CancelledError:
        pass
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
        log.error("[run_bridge] missing legs")
        return

    lang = call["lang"]
    log.info(f"=== BRIDGE ACTIVE EN <-> {lang} ===")

    caller_lock = asyncio.Lock()
    callee_lock = asyncio.Lock()
    callee_speaking = [False]
    caller_speaking = [False]
    stop_caller = asyncio.Event()
    stop_callee = asyncio.Event()

    async with calls_lock:
        entry = calls.get(caller_uuid, {})
        prewarm_stop = entry.get("prewarm_stop")
        cw_holder = entry.get("prewarm_caller_ws", [None])
        ew_holder = entry.get("prewarm_callee_ws", [None])
        pw_caller = entry.get("prewarm_caller_task")
        pw_callee = entry.get("prewarm_callee_task")
        caller_ready = entry.get("prewarm_caller_ready")
        callee_ready = entry.get("prewarm_callee_ready")

    if prewarm_stop:
        prewarm_stop.set()

    for task_name, task in [("caller", pw_caller), ("callee", pw_callee)]:
        if task and not task.done():
            try:
                await asyncio.wait_for(task, timeout=2.0)
            except asyncio.TimeoutError:
                task.cancel()
                try:
                    await task
                except Exception:
                    pass
            except Exception:
                pass

    caller_ws = cw_holder[0] if cw_holder else None
    callee_ws = ew_holder[0] if ew_holder else None

    if caller_ws and caller_ready and not caller_ready.is_set():
        try:
            await caller_ws.close()
        except Exception:
            pass
        caller_ws = None
    if callee_ws and callee_ready and not callee_ready.is_set():
        try:
            await callee_ws.close()
        except Exception:
            pass
        callee_ws = None

    if cw_holder:
        cw_holder[0] = None
    if ew_holder:
        ew_holder[0] = None

    log.info(
        f"[run_bridge] caller_ws={'prewarmed' if caller_ws else 'fresh'} "
        f"callee_ws={'prewarmed' if callee_ws else 'fresh'}"
    )

    bridge_caller = asyncio.create_task(one_way_bridge(
        "caller->callee",
        ci["queue"], ce["writer"], callee_lock,
        lambda: ci.get("alive", False),
        lambda: ce.get("alive", False),
        "English", lang,
        callee_speaking, caller_speaking,
        prewarmed_ws=caller_ws, stop_event=stop_caller,
    ))
    bridge_callee = asyncio.create_task(one_way_bridge(
        "callee->caller",
        ce["queue"], ci["writer"], caller_lock,
        lambda: ce.get("alive", False),
        lambda: ci.get("alive", False),
        lang, "English",
        caller_speaking, callee_speaking,
        prewarmed_ws=callee_ws, stop_event=stop_callee,
    ))

    async with calls_lock:
        if caller_uuid in calls:
            calls[caller_uuid]["bridge_tasks"] = (bridge_caller, bridge_callee)

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
        pass
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
        for ws_obj in (caller_ws, callee_ws):
            if ws_obj:
                try:
                    await ws_obj.close()
                    log.info("[run_bridge] Closed prewarmed WebSocket")
                except Exception:
                    pass
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

        lang_code = LANG_CODES.get(lang.lower(), LANG_CODES.get("english", "en"))

        pw_caller = asyncio.create_task(prewarm_openai_session(
            "English", lang,
            LANG_CODES.get("english", "en"),
            caller_ready, cw_holder, prewarm_stop,
        ))
        pw_callee = asyncio.create_task(prewarm_openai_session(
            lang, "English",
            lang_code,
            callee_ready, ew_holder, prewarm_stop,
        ))

        async with calls_lock:
            calls[caller_uuid].update({
                "prewarm_caller_ws":    cw_holder,
                "prewarm_callee_ws":    ew_holder,
                "prewarm_caller_task":  pw_caller,
                "prewarm_callee_task":  pw_callee,
                "prewarm_stop":         prewarm_stop,
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
            log.info("Both legs ready - starting bridge")
            if caller_conn.get("stop_ka_event"):
                caller_conn["stop_ka_event"].set()
            asyncio.create_task(run_bridge(caller_uuid))
        else:
            log.info("Callee arrived before caller - waiting")
    else:
        async with calls_lock:
            callee_conn = calls.get(caller_uuid, {}).get("callee")
        if callee_conn:
            log.info("Both legs ready - starting bridge")
            stop_ka.set()
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

#!/usr/bin/env python3
"""
Translation server smoke test — no SIP required.
Simulates both AudioSocket legs (caller + callee) directly over TCP.

Run on VPS:
  /opt/translation-server/venv/bin/python3 /opt/translation-server/test_bridge.py

What it does:
  1. Generates English speech via OpenAI TTS
  2. Connects as both caller (English) and callee (receives Indonesian)
  3. Sends English audio through the bridge
  4. Captures translated output, saves to /tmp/test_translation_output.wav
  5. Transcribes with Whisper to verify Indonesian content

AMI originate fires to a fake +62 number and fails harmlessly —
our manual callee leg already owns the bridge.
"""
import asyncio, struct, uuid as uuid_mod, os, sys, json, wave, subprocess, urllib.request
import numpy as np
import soxr

# ── load env ──────────────────────────────────────────────────────────────────

def _load_env_file(path="/etc/translation-server.env"):
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass

if not os.environ.get("OPENAI_API_KEY"):
    _load_env_file()

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
if not OPENAI_API_KEY:
    sys.exit("ERROR: OPENAI_API_KEY not set and /etc/translation-server.env not readable")

# ── config ────────────────────────────────────────────────────────────────────

HOST, PORT       = "127.0.0.1", 5001
MSG_UUID         = 0x01
MSG_AUDIO        = 0x10
MSG_HANGUP       = 0xFF
# +62 prefix → Indonesian; AMI will try to dial this and fail (non-fatal)
TEST_DEST        = "+62812000001"
TEST_CID         = "+10000000001"
OUTPUT_WAV       = "/tmp/test_translation_output.wav"
SPEECH_TEXT      = (
    "Hello! Good morning. My name is John. "
    "How are you today? I hope everything is going well. "
    "The weather is very nice. Can you hear me clearly? "
    "Please respond when you are ready."
)
POST_SPEECH_WAIT = 10.0   # seconds to wait for translation after audio ends
RECV_TIMEOUT     = 2.0    # seconds before each frame read times out

# ── AudioSocket helpers ───────────────────────────────────────────────────────

def _frame_uuid(uid_str):
    return struct.pack(">BH", MSG_UUID, 16) + uuid_mod.UUID(uid_str).bytes

def _frame_audio(pcm: bytes) -> bytes:
    return struct.pack(">BH", MSG_AUDIO, len(pcm)) + pcm

async def _read_frame(reader, timeout=5.0):
    h = await asyncio.wait_for(reader.readexactly(3), timeout=timeout)
    t, l = struct.unpack(">BH", h)
    p = await reader.readexactly(l) if l > 0 else b""
    return t, p

async def _connect_leg(uid_str):
    r, w = await asyncio.open_connection(HOST, PORT)
    w.write(_frame_uuid(uid_str))
    await w.drain()
    try:
        await _read_frame(r, timeout=3.0)  # server acks with a silence frame
    except Exception:
        pass
    return r, w

# ── OpenAI TTS → 8 kHz PCM16 ─────────────────────────────────────────────────

def _tts_sync(text: str) -> bytes:
    payload = json.dumps({
        "model": "tts-1",
        "voice": "alloy",
        "input": text,
        "response_format": "pcm",   # 24 kHz signed 16-bit mono
        "speed": 0.9,
    }).encode()
    req = urllib.request.Request(
        "https://api.openai.com/v1/audio/speech",
        data=payload,
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()

async def generate_speech_8k(text: str) -> bytes:
    print(f"[test] TTS: '{text[:50]}...'")
    pcm24 = await asyncio.to_thread(_tts_sync, text)
    print(f"[test] TTS → {len(pcm24)} bytes at 24 kHz")
    samples = np.frombuffer(pcm24, dtype=np.int16).astype(np.float32)
    out = soxr.resample(samples, 24000, 8000, quality="VHQ").astype(np.int16).tobytes()
    print(f"[test] resampled → {len(out)} bytes at 8 kHz ({len(out)/2/8000:.1f}s)")
    return out

# ── Whisper transcription ─────────────────────────────────────────────────────

def _whisper_sync(wav_path: str, language: str = "id") -> str:
    result = subprocess.run(
        [
            "curl", "-s",
            "https://api.openai.com/v1/audio/transcriptions",
            "-H", f"Authorization: Bearer {OPENAI_API_KEY}",
            "-F", "model=whisper-1",
            "-F", f"language={language}",
            "-F", f"file=@{wav_path};type=audio/wav",
        ],
        capture_output=True, text=True, timeout=30,
    )
    try:
        return json.loads(result.stdout).get("text", "")
    except Exception:
        return f"[parse error: {result.stdout[:200]}]"

# ── main ──────────────────────────────────────────────────────────────────────

async def main():
    caller_uuid = str(uuid_mod.uuid4())
    callee_uuid = str(uuid_mod.uuid4())
    meta_path   = f"/tmp/call_{caller_uuid}.txt"

    # Write call metadata — same format Asterisk dialplan writes
    with open(meta_path, "w") as f:
        f.write(f"{caller_uuid} {callee_uuid} {TEST_DEST} {TEST_CID}\n")
    print(f"[test] caller={caller_uuid[:8]}  callee={callee_uuid[:8]}")
    print(f"[test] dest={TEST_DEST}  →  language=Indonesian")

    english_pcm8 = await generate_speech_8k(SPEECH_TEXT)

    # ── connect callee first so bridge starts the moment caller arrives ──────
    print("\n[test] connecting callee leg...")
    callee_r, callee_w = await _connect_leg(callee_uuid)
    print("[test] callee ready (waiting for caller)")
    await asyncio.sleep(0.2)

    print("[test] connecting caller leg...")
    caller_r, caller_w = await _connect_leg(caller_uuid)
    print("[test] caller ready → bridge starting")
    print("[test]   (AMI will attempt fake number, failure is non-fatal)")
    await asyncio.sleep(1.0)   # let bridge open OpenAI WebSocket

    # ── run send and receive concurrently ─────────────────────────────────────
    collected  = bytearray()
    done_event = asyncio.Event()
    loop       = asyncio.get_running_loop()

    async def send_audio():
        n = len(english_pcm8) // 320
        print(f"\n[test] sending {n} frames of English speech to caller leg...")
        for i in range(0, len(english_pcm8), 320):
            chunk = english_pcm8[i:i+320]
            if len(chunk) < 320:
                chunk = chunk + b"\x00" * (320 - len(chunk))
            caller_w.write(_frame_audio(chunk))
            await asyncio.sleep(0.020)
        await caller_w.drain()
        print(f"[test] all caller audio sent; waiting {POST_SPEECH_WAIT}s for translation...")
        await asyncio.sleep(POST_SPEECH_WAIT)
        caller_w.write(struct.pack(">BH", MSG_HANGUP, 0))
        await caller_w.drain()
        print("[test] caller hung up")
        await asyncio.sleep(2.0)
        done_event.set()

    async def recv_audio():
        print("[test] receiving callee output (expect Indonesian)...")
        last_audio = loop.time()
        silence_ms = 0
        speech_ms  = 0

        while not done_event.is_set():
            try:
                t, p = await _read_frame(callee_r, timeout=RECV_TIMEOUT)
            except asyncio.TimeoutError:
                if done_event.is_set():
                    break
                continue
            except Exception as e:
                print(f"[test] callee recv error: {e}")
                break

            if t == MSG_AUDIO and p:
                collected.extend(p)
                samples = np.frombuffer(p[:min(len(p), 320)], dtype=np.int16)
                if np.any(samples):
                    speech_ms  += 20
                    last_audio  = loop.time()
                    if speech_ms % 200 == 0:
                        print(f"[test]   received {speech_ms}ms speech so far")
                else:
                    silence_ms += 20
            elif t == MSG_HANGUP:
                print("[test] callee received hangup")
                break

        total_s = len(collected) / 2 / 8000
        print(f"[test] recv done: {len(collected)} bytes | "
              f"{speech_ms}ms speech | {silence_ms}ms silence | {total_s:.1f}s total")

    await asyncio.gather(send_audio(), recv_audio())

    # ── results ───────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    non_silent_frames = sum(
        1 for i in range(0, len(collected), 320)
        if len(collected) - i >= 2 and
           np.any(np.frombuffer(collected[i:i+320][:len(collected)-i & ~1 or 320], dtype=np.int16))
    )

    if len(collected) > 3200:
        with wave.open(OUTPUT_WAV, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(8000)
            wf.writeframes(bytes(collected))
        print(f"[RESULT] saved → {OUTPUT_WAV}")
        print(f"[RESULT] non-silent frames: {non_silent_frames} / {len(collected)//320}")

        if non_silent_frames > 20:
            print("[RESULT] transcribing with Whisper (language=id)...")
            text = await asyncio.to_thread(_whisper_sync, OUTPUT_WAV, "id")
            print(f"[RESULT] Whisper (id): {text!r}")
            if text.strip():
                print("[RESULT] PASS — translation working")
            else:
                print("[RESULT] FAIL — Whisper got empty; audio may be English or silence")
                print("[RESULT]   retry: rerun with language='en' to check if English passed through")
        else:
            print("[RESULT] FAIL — output mostly silent; translation not delivering audio")
    else:
        print("[RESULT] FAIL — no meaningful audio on callee side")
        print("[RESULT]   check: journalctl -fu translation-server")

    # ── cleanup ───────────────────────────────────────────────────────────────
    try:
        os.remove(meta_path)
    except Exception:
        pass
    for w in [caller_w, callee_w]:
        try:
            w.close()
        except Exception:
            pass

asyncio.run(main())

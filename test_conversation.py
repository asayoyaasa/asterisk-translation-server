#!/usr/bin/env python3
"""
Egg-buying conversation test — English caller ↔ Indonesian callee.
Runs a fully scripted 12-turn conversation through the translation bridge.
Both sides use OpenAI TTS to generate speech; the bridge translates in real-time.

Run on VPS:
  /opt/translation-server/venv/bin/python3 /opt/translation-server/test_conversation.py

Output (on VPS):
  /tmp/conv_callee_hears.wav   — Indonesian caller hears (English→Indonesian translations)
  /tmp/conv_caller_hears.wav   — English caller hears (Indonesian→English translations)
  /tmp/conv_combined.wav       — both mixed into a single mono track for easy listening

Estimated runtime: ~3 minutes (TTS generation + ~2 min conversation).
"""
import asyncio, struct, uuid as uuid_mod, os, sys, json, wave, subprocess, urllib.request
import numpy as np
import soxr

# ── env ──────────────────────────────────────────────────────────────────────

def _load_env():
    try:
        with open("/etc/translation-server.env") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass

if not os.environ.get("OPENAI_API_KEY"):
    _load_env()

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
if not OPENAI_API_KEY:
    sys.exit("ERROR: OPENAI_API_KEY not set")

# ── config ────────────────────────────────────────────────────────────────────

HOST, PORT     = "127.0.0.1", 5001
MSG_UUID       = 0x01
MSG_AUDIO      = 0x10
MSG_HANGUP     = 0xFF
TEST_DEST      = "+62812000001"   # +62 → Indonesian; AMI will fail, non-fatal
TEST_CID       = "+10000000001"
PAUSE_AFTER    = 5.0              # seconds to wait after each turn for translation to arrive

CALLER_HEARS   = "/tmp/conv_caller_hears.wav"
CALLEE_HEARS   = "/tmp/conv_callee_hears.wav"
COMBINED       = "/tmp/conv_combined.wav"

# ── scripted conversation ─────────────────────────────────────────────────────
# (speaker, language_hint_for_whisper, text)
CONVERSATION = [
    ("caller", "en",
     "Hello, good morning! I am calling because I would like to buy some fresh eggs "
     "from your farm. Do you have eggs available today?"),

    ("callee", "id",
     "Halo, selamat pagi! Ya, tentu saja kami punya telur segar setiap hari. "
     "Berapa banyak telur yang Anda butuhkan?"),

    ("caller", "en",
     "I need about five dozen eggs. Are they chicken eggs or duck eggs? "
     "And how much do they cost?"),

    ("callee", "id",
     "Kami menjual telur ayam kampung organik. Harganya enam puluh ribu rupiah per lusin. "
     "Untuk lima lusin totalnya tiga ratus ribu rupiah, dan kami bisa kasih diskon sedikit."),

    ("caller", "en",
     "That is a very reasonable price. How fresh are the eggs? "
     "I want to make sure they were collected recently."),

    ("callee", "id",
     "Telur kami sangat segar, dipanen setiap pagi jam lima. "
     "Jadi kalau Anda beli sekarang, telurnya baru beberapa jam dipanen. "
     "Ayam-ayam kami sehat dan diberi pakan alami tanpa kimia."),

    ("caller", "en",
     "That is wonderful. I will definitely take five dozen. "
     "Can I come to pick them up tomorrow morning? What time do you open?"),

    ("callee", "id",
     "Kami buka setiap hari dari jam enam pagi sampai jam dua belas siang. "
     "Besok pagi silakan datang kapan saja. Kami selalu siap melayani pelanggan."),

    ("caller", "en",
     "Perfect. What is the address of your farm? "
     "I am not very familiar with this area and I need directions."),

    ("callee", "id",
     "Alamat kami di Jalan Raya Desa Sukamaju nomor lima belas, "
     "tepat di sebelah pasar tradisional. "
     "Ada papan nama besar bertuliskan Peternakan Maju Bersama di depan pintu masuk. "
     "Mudah ditemukan, tidak jauh dari jalan utama."),

    ("caller", "en",
     "Excellent, thank you so much. I will be there at eight tomorrow morning. "
     "Should I call ahead to confirm the order or is that necessary?"),

    ("callee", "id",
     "Tidak perlu menelepon lagi, Pak. Kami selalu punya stok yang cukup. "
     "Kami tunggu kedatangan Anda besok pagi jam delapan. "
     "Terima kasih sudah mau membeli dari peternakan kami. Sampai jumpa besok!"),
]

# ── AudioSocket ───────────────────────────────────────────────────────────────

SILENCE_320 = bytes(320)

def _frame_audio(pcm: bytes) -> bytes:
    return struct.pack(">BH", MSG_AUDIO, len(pcm)) + pcm

async def _read_frame(reader, timeout=2.0):
    h = await asyncio.wait_for(reader.readexactly(3), timeout=timeout)
    t, l = struct.unpack(">BH", h)
    p = await reader.readexactly(l) if l > 0 else b""
    return t, p

async def _connect_leg(uid_str):
    r, w = await asyncio.open_connection(HOST, PORT)
    w.write(struct.pack(">BH", MSG_UUID, 16) + uuid_mod.UUID(uid_str).bytes)
    await w.drain()
    try:
        await _read_frame(r, timeout=3.0)
    except Exception:
        pass
    return r, w

# ── TTS ───────────────────────────────────────────────────────────────────────

def _tts_sync(text: str) -> bytes:
    payload = json.dumps({
        "model": "tts-1",
        "voice": "alloy",
        "input": text,
        "response_format": "pcm",   # 24 kHz PCM16 mono
        "speed": 0.85,
    }).encode()
    req = urllib.request.Request(
        "https://api.openai.com/v1/audio/speech",
        data=payload,
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()

def _to_8k(pcm24: bytes) -> bytes:
    s = np.frombuffer(pcm24, dtype=np.int16).astype(np.float32)
    return soxr.resample(s, 24000, 8000, quality="VHQ").astype(np.int16).tobytes()

async def gen_tts(text: str) -> bytes:
    pcm24 = await asyncio.to_thread(_tts_sync, text)
    return await asyncio.to_thread(_to_8k, pcm24)

# ── Whisper ───────────────────────────────────────────────────────────────────

def _whisper_sync(wav_path: str, lang: str) -> str:
    r = subprocess.run([
        "curl", "-s",
        "https://api.openai.com/v1/audio/transcriptions",
        "-H", f"Authorization: Bearer {OPENAI_API_KEY}",
        "-F", "model=whisper-1",
        "-F", f"language={lang}",
        "-F", f"file=@{wav_path};type=audio/wav",
    ], capture_output=True, text=True, timeout=60)
    try:
        return json.loads(r.stdout).get("text", "")
    except Exception:
        return f"[error: {r.stdout[:200]}]"

# ── WAV helpers ───────────────────────────────────────────────────────────────

def save_wav(path: str, data: bytes, rate: int = 8000):
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(data)

def mix_wav(a: bytes, b: bytes) -> bytes:
    """Mix two PCM16 streams by averaging, padding shorter one with silence."""
    n = max(len(a), len(b))
    a = a + b"\x00" * (n - len(a))
    b = b + b"\x00" * (n - len(b))
    sa = np.frombuffer(a, dtype=np.int16).astype(np.int32)
    sb = np.frombuffer(b, dtype=np.int16).astype(np.int32)
    mixed = ((sa + sb) // 2).astype(np.int16)
    return mixed.tobytes()

# ── main ──────────────────────────────────────────────────────────────────────

async def main():
    # Step 1: pre-generate all TTS in parallel
    print(f"[conv] pre-generating TTS for {len(CONVERSATION)} turns (parallel)...")
    audio_list = await asyncio.gather(*[gen_tts(text) for _, _, text in CONVERSATION])

    turns = [(spk, lang, text, audio)
             for (spk, lang, text), audio in zip(CONVERSATION, audio_list)]

    total_speech = sum(len(a) / 2 / 8000 for a in audio_list)
    est_total    = total_speech + PAUSE_AFTER * len(turns)
    print(f"[conv] speech={total_speech:.1f}s  est_call_duration~={est_total:.0f}s")

    # Step 2: write call metadata
    caller_uuid = str(uuid_mod.uuid4())
    callee_uuid = str(uuid_mod.uuid4())
    meta_path   = f"/tmp/call_{caller_uuid}.txt"
    with open(meta_path, "w") as f:
        f.write(f"{caller_uuid} {callee_uuid} {TEST_DEST} {TEST_CID}\n")
    print(f"[conv] caller={caller_uuid[:8]}  callee={callee_uuid[:8]}\n")

    # Step 3: connect both legs
    callee_r, callee_w = await _connect_leg(callee_uuid)
    await asyncio.sleep(0.2)
    caller_r, caller_w = await _connect_leg(caller_uuid)
    print("[conv] bridge active (AMI to fake number — non-fatal)")
    await asyncio.sleep(1.0)   # wait for OpenAI WebSocket handshake

    # Step 4: queues + capture buffers
    caller_q  = asyncio.Queue()   # audio to send on caller leg (caller's voice → bridge)
    callee_q  = asyncio.Queue()   # audio to send on callee leg (callee's voice → bridge)
    caller_rcv = bytearray()      # what caller receives (Indonesian→English translations)
    callee_rcv = bytearray()      # what callee receives (English→Indonesian translations)
    done       = asyncio.Event()

    # Background sender: drains queue, fills gaps with silence at 20ms
    async def sender(writer, queue, label):
        while not done.is_set():
            try:
                chunk = queue.get_nowait()
            except asyncio.QueueEmpty:
                chunk = SILENCE_320
            try:
                writer.write(_frame_audio(chunk))
                await asyncio.sleep(0.020)
            except Exception:
                break
        try:
            writer.write(struct.pack(">BH", MSG_HANGUP, 0))
            await writer.drain()
        except Exception:
            pass

    # Background receiver: captures what this side hears from translation server
    async def receiver(reader, buf, label):
        while not done.is_set():
            try:
                t, p = await _read_frame(reader, timeout=1.0)
                if t == MSG_AUDIO and p:
                    buf.extend(p)
                elif t == MSG_HANGUP:
                    break
            except asyncio.TimeoutError:
                continue
            except Exception:
                break

    # Coordinator: push audio turns in sequence
    async def coordinator():
        for i, (spk, lang, text, audio) in enumerate(turns):
            dur = len(audio) / 2 / 8000
            q   = caller_q if spk == "caller" else callee_q
            print(f"[turn {i+1:02d}/{len(turns)}] {spk:6s} ({dur:.1f}s) | {text[:70]}")

            # Push all frames for this turn into the queue
            for j in range(0, len(audio), 320):
                chunk = audio[j:j+320]
                if len(chunk) < 320:
                    chunk = chunk + b"\x00" * (320 - len(chunk))
                q.put_nowait(chunk)

            # Wait for audio to play + translation to arrive on other side
            await asyncio.sleep(dur + PAUSE_AFTER)

        print("\n[conv] all turns sent — waiting 6s for final flush...")
        await asyncio.sleep(6.0)
        done.set()

    await asyncio.gather(
        sender(caller_w, caller_q, "caller"),
        sender(callee_w, callee_q, "callee"),
        receiver(caller_r, caller_rcv, "caller"),
        receiver(callee_r, callee_rcv, "callee"),
        coordinator(),
    )

    # Step 5: save WAVs
    print("\n" + "=" * 60)
    if callee_rcv:
        save_wav(CALLEE_HEARS, bytes(callee_rcv))
        print(f"[RESULT] callee_hears → {CALLEE_HEARS}  ({len(callee_rcv)/2/8000:.1f}s)")
    if caller_rcv:
        save_wav(CALLER_HEARS, bytes(caller_rcv))
        print(f"[RESULT] caller_hears → {CALLER_HEARS}  ({len(caller_rcv)/2/8000:.1f}s)")
    if callee_rcv and caller_rcv:
        save_wav(COMBINED, mix_wav(bytes(caller_rcv), bytes(callee_rcv)))
        print(f"[RESULT] combined     → {COMBINED}")

    # Step 6: Whisper transcription
    if callee_rcv and len(callee_rcv) > 3200:
        print("\n[RESULT] transcribing callee_hears (id — should be Indonesian)...")
        t = await asyncio.to_thread(_whisper_sync, CALLEE_HEARS, "id")
        print(f"  callee heard: {t!r}")

    if caller_rcv and len(caller_rcv) > 3200:
        print("\n[RESULT] transcribing caller_hears (en — should be English)...")
        t = await asyncio.to_thread(_whisper_sync, CALLER_HEARS, "en")
        print(f"  caller heard: {t!r}")

    # Copy instructions
    print("\n[RESULT] to copy WAVs to local machine:")
    print(f"  scp -i ~/.ssh/vps_voipagent root@188.116.36.83:{COMBINED} ~/conv_combined.wav")
    print(f"  scp -i ~/.ssh/vps_voipagent root@188.116.36.83:{CALLEE_HEARS} ~/conv_callee_hears.wav")
    print(f"  scp -i ~/.ssh/vps_voipagent root@188.116.36.83:{CALLER_HEARS} ~/conv_caller_hears.wav")

    # Cleanup
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

import asyncio, websockets, json, os, base64, logging, glob, struct, uuid as uuid_mod
from datetime import datetime, timezone
import soxr
import numpy as np

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("/var/log/translation-server.log")])
logging.getLogger("websockets").setLevel(logging.WARNING)
log = logging.getLogger(__name__)

from languages import get_language

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
HOST, PORT = "127.0.0.1", 5001
AMI_HOST, AMI_PORT = "127.0.0.1", 5038
AMI_USER   = os.environ.get("AMI_USER", "translation")
AMI_SECRET = os.environ.get("AMI_SECRET", "")

TRANSLATE_MODEL = os.environ.get("TRANSLATE_MODEL", "gpt-realtime-translate")
TRANSLATE_WS_URL = f"wss://api.openai.com/v1/realtime/translations?model={TRANSLATE_MODEL}"

MSG_UUID, MSG_AUDIO, MSG_HANGUP = 0x01, 0x10, 0xff
SILENCE_PAYLOAD = bytes(320)
SILENCE = struct.pack(">BH", MSG_AUDIO, 320) + SILENCE_PAYLOAD
HANGUP_FRAME = struct.pack(">BH", MSG_HANGUP, 0)

# 13 output languages supported by gpt-realtime-translate
SUPPORTED_OUTPUT_CODES = {"es", "pt", "fr", "ja", "ru", "zh", "de", "ko", "hi", "id", "vi", "it", "en"}

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

calls = {}

def build_frame(audio): return struct.pack(">BH", MSG_AUDIO, len(audio)) + audio
def parse_uuid(p): return str(uuid_mod.UUID(bytes=p)) if len(p)==16 else p.decode("utf-8","ignore").strip("\x00").strip()

def resample_up(audio):
    samples = np.frombuffer(audio, dtype=np.int16).astype(np.float32)
    return soxr.resample(samples, 8000, 24000, quality="VHQ").astype(np.int16).tobytes()

def resample_down(audio):
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
    import math
    samples = [int(8000 * math.sin(2 * math.pi * 425 * i / 8000)) for i in range(8000)]
    return struct.pack(f"<{len(samples)}h", *samples)

RINGBACK_1S = generate_ringback()
RINGBACK_SILENCE_1S = bytes(16000)

async def ringback_loop(writer, writer_lock, stop_event):
    ring_audio = RINGBACK_1S + RINGBACK_SILENCE_1S
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

# ── Dashboard event stream ───────────────────────────────────────────────────

DASHBOARD_EVENT_LOG = os.environ.get(
    "TRANSLATION_DASHBOARD_EVENT_LOG",
    "/opt/voipagent-translate/data/translation-events.jsonl",
)

def emit_dashboard_event(event_type: str, **payload):
    try:
        if not DASHBOARD_EVENT_LOG:
            return
        event = {"type": event_type, "ts": datetime.now(timezone.utc).isoformat(), **payload}
        parent = os.path.dirname(DASHBOARD_EVENT_LOG)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(DASHBOARD_EVENT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning(f"dashboard event write failed: {e}")

# ── Bridge ───────────────────────────────────────────────────────────────────

async def one_way_bridge(label, call_id, cid, dest, src_queue, dst_writer, dst_lock,
                         src_alive_fn, dst_alive_fn, src_lang, dst_lang):
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}

    dst_lang_code = LANG_CODES.get(dst_lang.lower(), "en")
    if dst_lang_code not in SUPPORTED_OUTPUT_CODES:
        log.warning(f"[{label}] {dst_lang} ({dst_lang_code}) not supported as output, falling back to en")
        dst_lang_code = "en"

    try:
        async with websockets.connect(TRANSLATE_WS_URL, additional_headers=headers) as ws:
            await ws.send(json.dumps({
                "type": "session.update",
                "session": {
                    "audio": {
                        "input": {
                            "transcription": {"model": "gpt-realtime-whisper"},
                            "noise_reduction": {"type": "near_field"}
                        },
                        "output": {"language": dst_lang_code}
                    }
                }
            }))
            log.info(f"[{label}] session ready | {src_lang} → {dst_lang} (code: {dst_lang_code}) | model: {TRANSLATE_MODEL}")

            async def pipe_in():
                try:
                    while src_alive_fn():
                        try:
                            audio = await asyncio.wait_for(src_queue.get(), timeout=0.5)
                        except asyncio.TimeoutError:
                            continue
                        audio24 = resample_up(audio)
                        await ws.send(json.dumps({
                            "type": "session.input_audio_buffer.append",
                            "audio": base64.b64encode(audio24).decode()
                        }))
                except Exception as e:
                    log.error(f"[{label}] pipe_in: {e}")
                finally:
                    try:
                        await ws.close()
                    except:
                        pass

            audio_out_queue = asyncio.Queue()

            async def audio_writer():
                """Drain audio_out_queue and write to dst at real-time pace."""
                while True:
                    try:
                        audio8 = await asyncio.wait_for(audio_out_queue.get(), timeout=0.5)
                    except asyncio.TimeoutError:
                        if not src_alive_fn():
                            break
                        continue
                    except Exception:
                        break
                    if not dst_alive_fn():
                        continue
                    log.info(f"[{label}] writing {len(audio8)} bytes audio to dst")
                    try:
                        async with dst_lock:
                            for i in range(0, len(audio8), 320):
                                frame = audio8[i:i+320]
                                if len(frame) < 320:
                                    frame += b"\x00" * (320 - len(frame))
                                dst_writer.write(build_frame(frame))
                                await dst_writer.drain()
                                await asyncio.sleep(0.019)
                    except Exception as e:
                        log.error(f"[{label}] audio_writer: {e}")

            async def pipe_out():
                last_src_text = [""]
                last_dst_text = [""]

                async for msg in ws:
                    try:
                        event = json.loads(msg)
                        etype = event.get("type", "")

                        # Audio output — enqueue immediately; audio_writer fills silence between deltas
                        if etype in ("response.audio.delta", "session.output_audio.delta"):
                            chunk = base64.b64decode(event.get("delta", ""))
                            if not chunk or not dst_alive_fn():
                                continue
                            await audio_out_queue.put(resample_down(chunk))

                        # Source transcript deltas — accumulate
                        elif etype in ("session.input_transcript.delta",
                                       "conversation.item.input_audio_transcription.delta"):
                            last_src_text[0] += event.get("delta", "") or event.get("transcript", "")

                        # Source transcript final
                        elif etype in ("conversation.item.input_audio_transcription.completed",
                                       "session.input_transcript.completed",
                                       "session.input_transcript.done"):
                            text = (event.get("transcript") or event.get("text") or last_src_text[0]).strip()
                            last_src_text[0] = text
                            if text:
                                log.info(f"[{label}] ORIGINAL  ({src_lang}): {text}")
                                emit_dashboard_event(
                                    "utterance.original",
                                    callId=call_id, cid=cid, dest=dest,
                                    direction=label, srcLang=src_lang, dstLang=dst_lang,
                                    text=text,
                                )
                            last_src_text[0] = ""

                        # Translated transcript deltas — accumulate
                        elif etype in ("session.output_transcript.delta",
                                       "response.output_audio_transcript.delta"):
                            last_dst_text[0] += event.get("delta", "") or event.get("transcript", "")

                        # Translated transcript final
                        elif etype in ("session.output_transcript.completed",
                                       "session.output_transcript.done",
                                       "response.output_audio_transcript.done",
                                       "response.audio_transcript.done"):
                            text = (event.get("transcript") or event.get("text") or last_dst_text[0]).strip()
                            last_dst_text[0] = ""
                            if text:
                                log.info(f"[{label}] TRANSLATED ({src_lang}→{dst_lang}): {text}")
                                emit_dashboard_event(
                                    "utterance.translated",
                                    callId=call_id, cid=cid, dest=dest,
                                    direction=label, srcLang=src_lang, dstLang=dst_lang,
                                    text=text,
                                    original=last_src_text[0],
                                )

                        elif etype == "error":
                            log.error(f"[{label}] OpenAI error: {event}")

                        else:
                            log.info(f"[{label}] event: {etype}")

                    except Exception as e:
                        log.error(f"[{label}] pipe_out: {e}")

            await asyncio.gather(pipe_in(), pipe_out(), audio_writer())

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
    log.info(f"=== BRIDGE ACTIVE EN <-> {lang} (model: {TRANSLATE_MODEL}) ===")
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
        one_way_bridge("caller→callee", caller_uuid, cid, dest,
                       ci["queue"], ce["writer"], callee_lock,
                       lambda: ci.get("alive", False), lambda: ce.get("alive", False),
                       "English", lang),
        one_way_bridge("callee→caller", caller_uuid, cid, dest,
                       ce["queue"], ci["writer"], caller_lock,
                       lambda: ce.get("alive", False), lambda: ci.get("alive", False),
                       lang, "English"),
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
        if calls[caller_uuid]["callee"]:
            log.info("Both legs ready - starting bridge (caller arrived second)")
            stop_ka.set()
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

async def main():
    if not OPENAI_API_KEY: log.error("OPENAI_API_KEY not set!"); return
    if not AMI_SECRET: log.error("AMI_SECRET not set!"); return
    log.info(f"Starting translation server — model: {TRANSLATE_MODEL}")
    server = await asyncio.start_server(handle_connection, HOST, PORT)
    log.info(f"Listening on {HOST}:{PORT}")
    asyncio.create_task(cleanup_calls())
    async with server: await server.serve_forever()

if __name__ == "__main__":
    asyncio.run(main())

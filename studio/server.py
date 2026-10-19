"""GL310 Studio : serveur web local pour piloter le LGP Lite (aperçu, enregistrement, réglages, état).

Lancement : python3 -m studio   (puis http://127.0.0.1:4848 s'ouvre dans le navigateur)
"""
from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from collections import deque
from dataclasses import asdict, replace
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import usb.core

from .device import Gl310Device
from .model import (AUDIO_KBPS_CHOICES, KEYFRAME_CHOICES, VIDEO_KBPS_CHOICES, Absent, Err, Failed, Idle, Recording,
                    Settings, Starting, Streaming, status_to_json, validate)

ROOT = Path(__file__).resolve().parent.parent
WEB = Path(__file__).resolve().parent / "web"
SETTINGS_FILE = Path(__file__).resolve().parent / "settings.json"
RECORDINGS = ROOT / "recordings"
TS_PACKET = 188


# --- MPEG-TS : réalignement, tables PAT/PMT, diffusion ----------------------------------------
def ts_payload(packet: bytes) -> bytes:
    afc = (packet[3] >> 4) & 3
    start = 4 + (1 + packet[4] if afc & 2 else 0)
    return packet[start:] if afc & 1 else b""


def pmt_pid_from_pat(packet: bytes) -> int | None:
    payload = ts_payload(packet)
    if not payload:
        return None
    section = payload[1 + payload[0]:]
    length = ((section[1] & 0x0F) << 8) | section[2]
    entries = section[8:3 + length - 4]
    for i in range(0, len(entries) - 3, 4):
        program = (entries[i] << 8) | entries[i + 1]
        if program != 0:
            return ((entries[i + 2] & 0x1F) << 8) | entries[i + 3]
    return None


class TsHub:
    """Reçoit des morceaux de TS, les réaligne et les diffuse aux abonnés (aperçu) et à l'enregistreur."""

    def __init__(self):
        self.lock = threading.Lock()
        self.buf = bytearray()
        self.pat = self.pmt = None
        self.pmt_pid = None
        self.subscribers: set[queue.Queue] = set()
        self.recorder: Recorder | None = None

    def feed(self, data: bytes):
        self.buf += data
        out = bytearray()
        while len(self.buf) >= TS_PACKET:
            if self.buf[0] != 0x47:
                nxt = self.buf.find(b"\x47", 1)
                del self.buf[:nxt if nxt > 0 else len(self.buf)]
                continue
            packet = bytes(self.buf[:TS_PACKET])
            del self.buf[:TS_PACKET]
            pid = ((packet[1] & 0x1F) << 8) | packet[2]
            if pid == 0:
                self.pat = packet
                self.pmt_pid = pmt_pid_from_pat(packet) or self.pmt_pid
            elif pid == self.pmt_pid:
                self.pmt = packet
            out += packet
        if not out:
            return
        chunk = bytes(out)
        with self.lock:
            for q in list(self.subscribers):
                try:
                    q.put_nowait(chunk)
                except queue.Full:          # aperçu trop lent : on le débranche plutôt que de bloquer
                    self.subscribers.discard(q)
                    q.put_nowait(None) if q.empty() else None
            if self.recorder:
                self.recorder.write(chunk)

    def header(self) -> bytes:
        return (self.pat or b"") + (self.pmt or b"")

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=512)
        with self.lock:
            if self.header():
                q.put_nowait(self.header())
            self.subscribers.add(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.subscribers.discard(q)

    def reset(self):
        self.buf.clear()


class Recorder:
    def __init__(self, header: bytes):
        RECORDINGS.mkdir(exist_ok=True)
        self.path = RECORDINGS / f"gl310-{datetime.now():%Y%m%d-%H%M%S}.ts"
        self.file = open(self.path, "wb")
        self.file.write(header)
        self.bytes = len(header)
        self.started = time.time()

    def write(self, chunk: bytes):
        self.file.write(chunk)
        self.bytes += len(chunk)

    def info(self) -> Recording:
        return Recording(path=str(self.path.relative_to(ROOT)), seconds=round(time.time() - self.started, 1), bytes=self.bytes)

    def finish(self):
        """Ferme le .ts puis le convertit en .mp4 (copie sans réencodage) ; le .ts est supprimé si tout va bien."""
        self.file.close()
        mp4 = self.path.with_suffix(".mp4")
        done = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(self.path),
                               "-map", "0", "-c", "copy", "-movflags", "+faststart", str(mp4)])
        if done.returncode == 0 and mp4.exists():
            self.path.unlink()


# --- Moteur : le seul fil qui parle au boîtier --------------------------------------------------
class Engine(threading.Thread):
    def __init__(self, hub: TsHub):
        super().__init__(daemon=True)
        self.hub = hub
        self.commands: queue.Queue = queue.Queue()
        self.logs: deque[str] = deque(maxlen=200)
        self.device = Gl310Device(log=self.log)
        self.connected = False
        self.streaming = False
        self.firmware = False
        self.signal = None
        self.step: str | None = None
        self.error: str | None = None
        self.settings = load_settings()
        self.window: deque[tuple[float, int]] = deque()
        self.lock = threading.Lock()

    def log(self, text):
        self.logs.append(f"{datetime.now():%H:%M:%S} {text}")

    # Commandes envoyées par le serveur web (exécutées dans le fil du moteur).
    def submit(self, name, arg=None):
        self.commands.put((name, arg))

    def status(self):
        with self.lock:
            if self.error:
                return Failed(self.error)
            if not self.connected:
                return Absent()
            if self.step:
                return Starting(self.step)
            if self.streaming:
                now = time.time()
                recent = [(t, n) for t, n in self.window if now - t <= 2.0]
                mbps = sum(n for _, n in recent) * 8 / 2.0 / 1e6
                rec = self.hub.recorder.info() if self.hub.recorder else None
                return Streaming(self.signal, self.settings, round(mbps, 1), round(len(recent) / 2.0, 1), rec)
            return Idle(self.signal, self.firmware)

    def run(self):
        last_poll = 0.0
        while True:
            try:
                self.handle_commands()
                if not self.connected:
                    self.connected = self.device.open()
                    if not self.connected:
                        time.sleep(1)
                        continue
                    self.firmware = self.device.firmware_loaded()
                    self.log(f"boîtier branché (firmware {'présent' if self.firmware else 'absent'})")
                if time.time() - last_poll > 1.0:
                    self.signal = self.device.signal()
                    last_poll = time.time()
                if self.streaming:
                    data = self.device.poll_ts()
                    if data:
                        self.hub.feed(data)
                        self.window.append((time.time(), len(data)))
                        while self.window and time.time() - self.window[0][0] > 2.0:
                            self.window.popleft()
                    else:
                        time.sleep(0.002)
                else:
                    time.sleep(0.05)
            except (usb.core.USBError, OSError) as e:
                self.lost(f"liaison USB perdue : {e}")
            except Exception as e:  # noqa: BLE001 — on affiche l'erreur dans la page plutôt que de mourir
                self.lost(f"erreur : {e}")

    def lost(self, message):
        self.log(message)
        self.stop_recording()
        self.device.close()
        with self.lock:
            self.connected = self.streaming = self.firmware = False
            self.step = None
        time.sleep(1)

    def handle_commands(self):
        while not self.commands.empty():
            name, arg = self.commands.get_nowait()
            self.error = None
            if name == "start":
                self.start_stream()
            elif name == "stop":
                self.stop_stream()
            elif name == "settings":
                self.apply(arg)
            elif name == "record":
                self.start_recording() if arg else self.stop_recording()

    def start_stream(self):
        if not self.connected:
            return
        if not self.device.firmware_loaded():
            self.step = "chargement du firmware (environ 10 s)"
            self.log(self.step)
            self.device.boot()
            self.firmware = True
        elif self.streaming or self.firmware:
            self.step = "arrêt de l'encodeur"
            self.device.stop_encoder()
        self.step = "démarrage de l'encodeur"
        self.log(self.step)
        self.hub.reset()
        self.device.start_encoder(self.settings)
        self.streaming = True
        self.step = None
        self.log(f"en direct : {self.settings.video_kbps} kbit/s")

    def stop_stream(self):
        self.stop_recording()
        if self.connected and self.streaming:
            self.device.stop_encoder()
        self.streaming = False
        self.log("encodeur arrêté")

    def apply(self, new: Settings):
        old, self.settings = self.settings, new
        save_settings(new)
        if not self.connected:
            return
        image_only = replace(old, brightness=new.brightness, contrast=new.contrast) == new
        if image_only:
            self.device.apply_image(new)
            self.log(f"image : luminosité {new.brightness}, contraste {new.contrast}")
        elif self.streaming:
            self.log("nouveaux réglages : relance de l'encodeur")
            self.start_stream()

    def start_recording(self):
        if self.streaming and not self.hub.recorder:
            self.hub.recorder = Recorder(self.hub.header())
            self.log(f"enregistrement : {self.hub.recorder.path.name}")

    def stop_recording(self):
        rec, self.hub.recorder = self.hub.recorder, None
        if rec:
            self.log(f"fin d'enregistrement : {rec.path.name}, conversion en MP4")
            threading.Thread(target=rec.finish, daemon=True).start()


def load_settings() -> Settings:
    try:
        candidate = Settings(**json.loads(SETTINGS_FILE.read_text()))
        return candidate if not isinstance(validate(candidate), Err) else Settings()
    except (OSError, ValueError, TypeError):
        return Settings()


def save_settings(settings: Settings):
    SETTINGS_FILE.write_text(json.dumps(asdict(settings), indent=2))


# --- HTTP ------------------------------------------------------------------------------------------
def make_handler(engine: Engine, hub: TsHub):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send_json(self, data, code=200):
            body = json.dumps(data).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def send_file(self, path: Path, ctype: str):
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def state(self):
            recordings = sorted(RECORDINGS.glob("gl310-*.*"), reverse=True)[:8] if RECORDINGS.exists() else []
            return {"status": status_to_json(engine.status()), "settings": asdict(engine.settings),
                    "choices": {"video_kbps": VIDEO_KBPS_CHOICES, "keyframe_interval": KEYFRAME_CHOICES,
                                "audio_kbps": AUDIO_KBPS_CHOICES},
                    "recordings": [{"name": p.name, "mb": round(p.stat().st_size / 1e6, 1)} for p in recordings],
                    "recordings_dir": str(RECORDINGS), "logs": list(engine.logs)[-12:]}

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                return self.send_file(WEB / "index.html", "text/html; charset=utf-8")
            if self.path == "/mpegts.js":
                return self.send_file(WEB / "mpegts.js", "application/javascript")
            if self.path == "/api/status":
                return self.send_json(self.state())
            if self.path == "/stream.ts":
                return self.stream()
            self.send_error(404)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            if self.path in ("/api/start", "/api/stop"):
                engine.submit(self.path.rsplit("/", 1)[1])
            elif self.path == "/api/record":
                engine.submit("record", bool(body.get("on")))
            elif self.path == "/api/settings":
                try:
                    candidate = Settings(**{**asdict(engine.settings), **body})
                except TypeError as e:
                    return self.send_json({"error": str(e)}, 400)
                result = validate(candidate)
                if isinstance(result, Err):
                    return self.send_json({"error": result.error}, 400)
                engine.submit("settings", result.value)
            else:
                return self.send_error(404)
            time.sleep(0.05)
            self.send_json(self.state())

        def stream(self):
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            q = hub.subscribe()
            try:
                while True:
                    chunk = q.get(timeout=10)
                    if chunk is None:
                        break
                    self.wfile.write(chunk)
            except (queue.Empty, BrokenPipeError, ConnectionResetError):
                pass
            finally:
                hub.unsubscribe(q)

    return Handler


def serve(host="127.0.0.1", port=4848, open_browser=True):
    hub = TsHub()
    engine = Engine(hub)
    engine.start()
    server = ThreadingHTTPServer((host, port), make_handler(engine, hub))
    server.daemon_threads = True
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{port}"
    print(f"GL310 Studio : {url}  (Ctrl+C pour quitter)")
    if open_browser:
        subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        engine.stop_recording()
        print("Arrêt de GL310 Studio.")

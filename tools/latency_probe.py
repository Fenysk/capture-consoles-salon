#!/usr/bin/env python3
"""Mesure le délai propre du boîtier GL310, sans dépendre de l'horloge de la console.

Montage : la sortie HDMI du portable branchée sur l'entrée HDMI IN du boîtier (à la place de la console),
GL310 Studio lancé. Le script affiche en plein écran sur cette sortie l'heure du portable, en clair (pour un œil
ou OBS) et en code-barres : deux rangées de 20 blocs, la seconde inversée, qui portent l'heure en ms modulo 2^20.
Il lit le flux de GL310 Studio, décode chaque image et calcule délai = heure de réception − heure affichée.
Mesure : sortie HDMI → encodeur du boîtier → USB → appli → décodage, à 1 ou 2 images près (affichage du portable).

Il sert aussi http://<portable>:4849/ : l'heure du portable (synchronisée par le réseau). Dans OBS sur la tour,
mettre cette page en source navigateur à côté du jeu : l'écart entre les deux heures est le délai du jeu dans OBS.

Usage : latency_probe.py [--output HDMI-1] [--measure 60] [--hold]
        latency_probe.py --output eDP-1 --source x11grab --measure 10   (essai sans boîtier, sur l'écran du portable)
"""
import argparse
import json
import re
import statistics
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRAME_W, FRAME_H = 1920, 1080
BITS = 20
CODE_MOD = 1 << BITS  # ~17 min : bien plus que n'importe quel délai
BLOCK_W = FRAME_W // BITS
ROW1_Y, ROW2_Y, ROW_H = 700, 890, 170
GRAB_W, GRAB_H = 480, 270  # image réduite pour le décodage
WEB_PORT = 4849

# ---- partie pure (testée par test_latency_probe.py) -----------------------------------------


def code_of(epoch_ms):
    return int(epoch_ms) % CODE_MOD


def code_blocks(value):
    """Blocs (x, y, largeur, hauteur, blanc) du code, en coordonnées 1920×1080 : bit de poids fort à gauche,
    rangée 1 = la valeur, rangée 2 = son inverse (la lecture compare les deux, sans seuil)."""
    blocks = []
    for i in range(BITS):
        bit = (value >> (BITS - 1 - i)) & 1 == 1
        blocks.append((i * BLOCK_W, ROW1_Y, BLOCK_W, ROW_H, bit))
        blocks.append((i * BLOCK_W, ROW2_Y, BLOCK_W, ROW_H, not bit))
    return blocks


def _patch_mean(frame, width, height, cx, cy):
    """Moyenne d'un carré au centre d'un bloc (cx, cy en coordonnées 1920×1080)."""
    x, y = int(cx * width / FRAME_W), int(cy * height / FRAME_H)
    r = max(1, width // 160)
    total = 0
    for row in range(y - r, y + r):
        total += sum(frame[row * width + x - r:row * width + x + r])
    return total / (4 * r * r)


def read_code(frame, width, height, margin=60):
    """Valeur portée par l'image grise, ou None si un bloc n'est pas franchement opposé à son inverse
    (pas de code à l'écran, image déchirée entre deux affichages)."""
    value = 0
    for i in range(BITS):
        cx = i * BLOCK_W + BLOCK_W / 2
        a = _patch_mean(frame, width, height, cx, ROW1_Y + ROW_H / 2)
        b = _patch_mean(frame, width, height, cx, ROW2_Y + ROW_H / 2)
        if abs(a - b) < margin:
            return None
        value = (value << 1) | (a > b)
    return value


def delay_ms(code, now_ms):
    """Délai entre l'affichage du code et maintenant, en tenant compte du tour du compteur."""
    return (code_of(now_ms) - code) % CODE_MOD


def summary(delays):
    d = sorted(delays)
    return (f"{len(d)} images : médiane {statistics.median(d):.0f} ms, min {d[0]:.0f}, max {d[-1]:.0f}, "
            f"10–90 % {d[len(d) // 10]:.0f}–{d[(len(d) * 9) // 10]:.0f} ms")


# ---- affichage plein écran (GTK) -------------------------------------------------------------


def output_geometry(name):
    """(x, y, l, h) de la sortie d'écran `name` si elle est active, sinon None."""
    out = subprocess.run(["xrandr", "--query"], capture_output=True, text=True, check=True).stdout
    m = re.search(rf"^{re.escape(name)} connected (?:primary )?(\d+)x(\d+)\+(\d+)\+(\d+)", out, re.M)
    if not m:
        return None
    w, h, x, y = map(int, m.groups())
    return x, y, w, h


def output_connected(name):
    out = subprocess.run(["xrandr", "--query"], capture_output=True, text=True, check=True).stdout
    return re.search(rf"^{re.escape(name)} connected", out, re.M) is not None


def show_clock(geometry):
    import gi
    gi.require_version("Gdk", "3.0")
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gdk, Gtk

    def draw(widget, cr):
        now = time.time()
        alloc = widget.get_allocation()
        cr.scale(alloc.width / FRAME_W, alloc.height / FRAME_H)
        cr.set_source_rgb(0, 0, 0)
        cr.paint()
        cr.set_source_rgb(1, 1, 1)
        for x, y, w, h, white in code_blocks(code_of(now * 1000)):
            if white:
                cr.rectangle(x, y, w, h)
        cr.fill()
        cr.select_font_face("monospace", 0, 1)
        cr.set_font_size(230)
        text = time.strftime("%H:%M:%S", time.localtime(now)) + f".{int(now * 100) % 100:02d}"
        ext = cr.text_extents(text)
        cr.move_to((FRAME_W - ext.width) / 2 - ext.x_bearing, 420)
        cr.show_text(text)
        cr.set_font_size(40)
        cr.move_to(60, 640)
        cr.show_text("Heure du portable (mesure du délai GL310)")
        return False

    win = Gtk.Window(title="Mesure du délai GL310")
    area = Gtk.DrawingArea()
    area.connect("draw", draw)
    win.add(area)
    win.move(geometry[0], geometry[1])
    win.set_default_size(geometry[2], geometry[3])
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    win.fullscreen()
    win.get_window().set_cursor(Gdk.Cursor.new_for_display(Gdk.Display.get_default(), Gdk.CursorType.BLANK_CURSOR))
    area.add_tick_callback(lambda w, clock: (w.queue_draw(), True)[1])
    return Gtk


# ---- page web : l'heure du portable, pour OBS sur la tour ------------------------------------

PAGE = """<!doctype html><meta charset="utf-8"><title>Heure du portable</title>
<style>html,body{margin:0;height:100%;background:#000;color:#fff;font:bold 13vw monospace}
body{display:grid;place-content:center;text-align:center}small{font-size:2.5vw;font-weight:normal;opacity:.7}</style>
<div id="t">--:--:--.--</div><small id="s">synchronisation…</small>
<script>
let offset = 0, tz = 0;
async function sync() {
  let best = Infinity;
  for (let i = 0; i < 8; i++) {
    const a = performance.now();
    const r = await (await fetch("/time", {cache: "no-store"})).json();
    const b = performance.now();
    if (b - a < best) { best = b - a; offset = r.ms - (performance.timeOrigin + (a + b) / 2); tz = r.tz_min; }
  }
  document.getElementById("s").textContent = "Heure du portable (aller-retour " + best.toFixed(1) + " ms)";
}
function tick() {
  const ms = performance.timeOrigin + performance.now() + offset + tz * 60000;
  const d = new Date(ms), p = (n, k = 2) => String(n).padStart(k, "0");
  document.getElementById("t").textContent = p(d.getUTCHours()) + ":" + p(d.getUTCMinutes()) + ":" +
    p(d.getUTCSeconds()) + "." + p(Math.floor(d.getUTCMilliseconds() / 10));
  requestAnimationFrame(tick);
}
sync().then(tick); setInterval(sync, 10000);
</script>
"""


class TimeHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/time"):
            body = json.dumps({"ms": time.time() * 1000, "tz_min": -time.timezone // 60 + 60 * time.localtime().tm_isdst})
            kind = "application/json"
        else:
            body, kind = PAGE, "text/html; charset=utf-8"
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


# ---- lecture du flux et mesure ---------------------------------------------------------------


def grab_command(source, geometry):
    scale = ["-vf", f"scale={GRAB_W}:{GRAB_H},format=gray", "-f", "rawvideo", "-"]
    if source == "x11grab":
        x, y, w, h = geometry
        return ["ffmpeg", "-loglevel", "error", "-f", "x11grab", "-framerate", "30", "-video_size", f"{w}x{h}",
                "-i", f":0.0+{x},{y}", *scale]
    # -threads 1 : pas de décodage par images en parallèle, qui retiendrait plusieurs images.
    return ["ffmpeg", "-loglevel", "error", "-threads", "1", "-probesize", "500000", "-analyzeduration", "500000",
            "-i", source, *scale]


def measure(source, geometry, seconds, log):
    proc = subprocess.Popen(grab_command(source, geometry), stdout=subprocess.PIPE)
    size = GRAB_W * GRAB_H
    delays, unread, start, last_report = [], 0, time.time(), time.time()
    try:
        while time.time() - start < seconds:
            frame = proc.stdout.read(size)
            now = time.time()
            if len(frame) < size:
                log("flux interrompu")
                break
            code = read_code(frame, GRAB_W, GRAB_H)
            if code is None:
                unread += 1
            else:
                delays.append(delay_ms(code, now * 1000))
            if now - last_report >= 5 and delays:
                log(f"{summary(delays[-150:])} (illisibles : {unread})")
                last_report = now
    finally:
        proc.kill()
    return delays, unread


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--output", default="HDMI-1", help="sortie d'écran reliée au boîtier")
    ap.add_argument("--source", default="http://127.0.0.1:4848/stream.ts", help="flux de GL310 Studio, ou x11grab")
    ap.add_argument("--measure", type=float, default=60, help="durée de la mesure en s (0 : affichage seul)")
    ap.add_argument("--hold", action="store_true", help="garder l'affichage et la page après la mesure")
    args = ap.parse_args()

    turned_on = False
    if output_geometry(args.output) is None:
        if not output_connected(args.output):
            sys.exit(f"{args.output} n'est pas branchée : relier la sortie HDMI du portable à l'entrée HDMI IN du boîtier")
        subprocess.run(["xrandr", "--output", args.output, "--mode", "1920x1080", "--rate", "60",
                        "--right-of", "eDP-1"], check=True)
        turned_on = True
        time.sleep(2)
    geometry = output_geometry(args.output)

    logs = ROOT / "logs"
    logs.mkdir(exist_ok=True)
    log_path = logs / time.strftime("latency-%Y%m%d-%H%M%S.txt")

    def log(text):
        line = time.strftime("%H:%M:%S ") + text
        print(line, flush=True)
        with log_path.open("a") as f:
            f.write(line + "\n")

    server = ThreadingHTTPServer(("0.0.0.0", WEB_PORT), TimeHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log(f"sortie {args.output} {geometry}, source {args.source}, page http://<portable>:{WEB_PORT}/")

    gtk = show_clock(geometry)

    def run():
        time.sleep(3)  # laisser la fenêtre s'afficher et l'encodeur suivre le nouveau signal
        if args.measure > 0:
            delays, unread = measure(args.source, geometry, args.measure, log)
            log(f"RÉSULTAT {summary(delays)} ; illisibles : {unread}" if delays else f"aucun code lu ({unread} images)")
        if not args.hold:
            from gi.repository import GLib
            GLib.idle_add(gtk.main_quit)

    threading.Thread(target=run, daemon=True).start()
    try:
        gtk.main()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        if turned_on:
            subprocess.run(["xrandr", "--output", args.output, "--off"])


if __name__ == "__main__":
    main()

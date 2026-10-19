#!/usr/bin/env python3
"""Essai : le firmware du GL310 sait-il sortir du son PCM brut (pour un périphérique ALSA) ?

Le pilote C985 (même firmware QPSOS) démarre une seconde tâche (taskId 1, fonction 0x80000040) dont
AUDIO_CONTROL_PARAM a audio_type = 0 (LPCM) : le firmware envoie alors des messages 0x40 de la tâche 1
contenant du S16_LE stéréo 48 kHz (c985_audio.c). On rejoue cette séquence sur le GL310, en RAM uniquement,
après avoir démarré l'encodeur MPEG-TS habituel (tâche 0), et on regarde ce qui revient.

Variantes de AUDIO_CONTROL_PARAM pour la tâche 1 :
  c985     0x01121080 (valeur du C985)
  gl310in  0x01161080 (bits d'entrée I2S du GL310, audio_type = 0)

Usage : gl310_pcm_probe.py [--variant gl310in] [--seconds 5] [--no-video]
Sortie : captures/pcm-probe-<variante>.raw (+ .wav) et le décompte des messages par (tâche, type, étiquette).
"""
import argparse
import array
import struct
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from studio.device import Gl310Device  # noqa: E402
from studio.model import Settings  # noqa: E402
from tools.gl310_capture import release  # noqa: E402

AUDIO_TASK = 1
AUDIO_FUNCTION = 0x80000040
SETUP = [  # (opcode, param0, slots) : c985_audio_boot
    (0xF1, AUDIO_FUNCTION, []),
    (0xF2, 0x01000108, []),
    (0x10, 0x0F, [0, 0]),
    (0x10, 0x10, [0, 0, 0]),
    (0x10, 0x12, [0]),
    (0x10, 0x13, [0x50, 0, 0x0A]),
    (0x10, 0x14, [1, 0x4A38]),
    (0x10, 0x04, [0, 0, 0]),
    (0x10, 0x02, [0xF1F1F1DA, 0xB6F1F1B6]),
]
CONFIG = [0x2101B214, 0x04380780, 0x0F7C0609, 0x005003E8, 0x1F4007D0, 0x80002000,
          0x6001000A, 0x04380780, 0x10, None, 0x480]  # None = AUDIO_CONTROL_PARAM (variante)
VARIANTS = {"c985": 0x01121080, "gl310in": 0x01161080}


def send(dev, opcode, task, param0, slots, bare=False):
    for _ in range(300):
        if not dev.reg_read(0x06CC)[0] & 1:
            break
        time.sleep(0.001)
    if not bare:
        dev.reg_write(0x06F8, param0)
        if opcode == 0x10:  # le C985 écrit les 10 emplacements (zéros au-delà)
            slots = (slots + [0] * 10)[:10]
        for i, v in enumerate(slots):
            dev.reg_write(0x06F4 - 4 * i, v)
    dev.reg_write(0x06CC, (task << 16) | 1)
    dev.reg_write(0x06FC, (task << 16) | opcode)


def rms(samples):
    return (sum(s * s for s in samples) / max(len(samples), 1)) ** 0.5


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--variant", choices=sorted(VARIANTS), default="gl310in")
    parser.add_argument("--seconds", type=float, default=5)
    parser.add_argument("--no-video", action="store_true", help="ne démarre pas l'encodeur MPEG-TS (tâche 0)")
    args = parser.parse_args()

    dev = Gl310Device()
    if not dev.open():
        sys.exit("LGP Lite introuvable (ou pris par le pilote noyau : sudo rmmod gl310).")
    if not dev.firmware_loaded():
        print("Firmware absent : démarrage complet…")
        dev.boot()
    if dev.reg_read(0x0800)[0] & 0x10000:
        print("Encodeur en marche : arrêt (0x02 + 0xF3)…")
        dev.stop_encoder()
    print(f"Signal : {dev.signal()}")
    if not args.no_video:
        dev.start_encoder(Settings())
        print("Encodeur MPEG-TS (tâche 0) démarré.")

    print(f"Tâche audio 1, variante {args.variant} : AUDIO_CONTROL_PARAM = {VARIANTS[args.variant]:#010x}")
    for opcode, param0, slots in SETUP:
        send(dev, opcode, AUDIO_TASK, param0, slots)
    config = [VARIANTS[args.variant] if v is None else v for v in CONFIG]
    for i, v in enumerate(config):
        dev.reg_write(0x06F8 - 4 * i, v)
    send(dev, 0x01, AUDIO_TASK, AUDIO_TASK, [], bare=True)

    counts, pcm = {}, bytearray()
    end = time.time() + args.seconds
    while time.time() < end:
        st = dev.reg_read(0x06C8)[0]
        if not st & 1:
            time.sleep(0.002)
            continue
        msg, p0, addr, addr2, size, pts = dev.reg_read(0x06B0, 6)
        task, kind, tag = msg >> 16, msg & 0xFFFF, p0 & 0xFFFF
        key = (task, kind, tag)
        counts[key] = counts.get(key, 0) + 1
        if counts[key] <= 3:
            print(f"tâche {task} type {kind:#x} étiquette {tag:#x} : p0={p0:#x} adr={addr:#x} adr2={addr2:#x} "
                  f"taille={size:#x} pts={pts:#x} état={st:#x}")
        if (kind & 0xFF) == 0x40 and task == AUDIO_TASK and 0 < size < 0x10000:
            pcm += dev.mem_read(addr, size * 4)
        release(dev, st, msg, p0, size, pts)

    print("Arrêt de la tâche audio puis de l'encodeur…")
    send(dev, 0x02, AUDIO_TASK, 0, [])
    time.sleep(0.3)
    send(dev, 0xF3, AUDIO_TASK, 0, [])
    time.sleep(0.3)
    dev.stop_encoder()
    dev.close()

    print("Messages reçus (tâche, type, étiquette) :")
    for (task, kind, tag), n in sorted(counts.items()):
        print(f"  tâche {task} type {kind:#06x} étiquette {tag:#06x} : {n}")
    if not pcm:
        print("Aucune donnée de la tâche audio.")
        return
    out = ROOT / f"captures/pcm-probe-{args.variant}.raw"
    out.write_bytes(pcm)
    seconds = len(pcm) / (48000 * 4)
    for name, data in (("tel quel", pcm), ("mots inversés", bytes(b for i in range(0, len(pcm) - 3, 4)
                                                                     for b in pcm[i:i + 4][::-1]))):
        samples = array.array("h", data[:len(data) // 2 * 2])
        print(f"  {name:13} : {len(pcm)} octets ≈ {seconds:.2f} s à 48 kHz stéréo, RMS {rms(samples):.0f}, "
              f"min {min(samples)}, max {max(samples)}")
    wav = out.with_suffix(".wav")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "s16le", "-ar", "48000", "-ac", "2", "-i", str(out), str(wav)],
                   check=True)
    print(f"Écrit : {out} et {wav}")


if __name__ == "__main__":
    main()

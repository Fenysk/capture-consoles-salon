"""Gl310Device : la seule classe qui parle au boîtier en USB.

Elle réutilise les outils de tools/ (chargement officiel, rejeu de la trace, lecture du flux) et ne doit être
utilisée que depuis un seul fil d'exécution à la fois (le moteur de server.py).
"""
from __future__ import annotations

import struct
import sys
import time
from pathlib import Path

import usb.core
import usb.util

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import gl310_boot  # noqa: E402
import gl310_encode  # noqa: E402
from gl310_capture import Box as CaptureBox  # noqa: E402
from gl310_capture import drain, release, word_swap  # noqa: E402

from .model import (TRACE_AUDIO_CONTROL_PARAM, TRACE_GOP_LOOP_FILTER, TRACE_RATE_CONTROL, Settings, Signal,  # noqa: E402
                    audio_control_param, gop_loop_filter, parse_timing, rate_control)

VENDOR, PRODUCT = 0x07CA, 0xC835
MCU_ADDR = 0x15
MCU_SIGNAL, MCU_TIMING = 0x1C, 0x1D
MCU_BRIGHTNESS, MCU_CONTRAST, MCU_APPLY_IMAGE = 0x43, 0x44, 0x42
MCU_PROFILE_REGS = (0x47, 0x49)          # enregistrement permanent : jamais
REG_RATE, REG_GOP, REG_AUDIO = 0x06EC, 0x06E0, 0x06D4
MSG_FRAME = 0x40


class Gl310Device(CaptureBox):
    def __init__(self, log=print):
        # On n'appelle pas CaptureBox.__init__ : il quitte le programme si le boîtier est absent.
        self.dev = None
        self.log = log

    # --- connexion ---------------------------------------------------------------------------
    def open(self) -> bool:
        dev = usb.core.find(idVendor=VENDOR, idProduct=PRODUCT)
        if dev is None:
            return False
        usb.util.claim_interface(dev, 0)
        self.dev = dev
        self.flush()
        return True

    def close(self):
        if self.dev is not None:
            try:
                usb.util.dispose_resources(self.dev)
            finally:
                self.dev = None

    # --- utilisés par gl310_boot.Box.run et gl310_encode.replay ----------------------------
    def say(self, text):
        self.log(text)

    def read_exact(self, ep, size, timeout):
        buf = bytearray()
        while len(buf) < size:
            buf += self.dev.read(ep, size - len(buf), timeout)
        return bytes(buf)

    # --- microcontrôleur -----------------------------------------------------------------------
    def mcu_read(self, reg, n=1) -> bytes:
        return self.raw(bytes([0x0C, 0x01, n, 0x00, MCU_ADDR, 0, 0, 0, reg]))[:n]

    def mcu_write(self, reg, values: bytes):
        if reg in MCU_PROFILE_REGS:
            raise ValueError(f"registre {reg:#04x} du micro : profil permanent, refusé")
        self.raw(bytes([0x0B, 0x01, 1 + len(values), 0x00, MCU_ADDR, 0, 0, 0, reg]) + values)

    # --- état ----------------------------------------------------------------------------------
    def firmware_loaded(self) -> bool:
        return self.raw(b"\x02\x00\x04\x00" + struct.pack("<II", 0x40, 0x40))[:4] == b"QSOS"

    def signal(self) -> Signal | None:
        if not self.mcu_read(MCU_SIGNAL)[0] & 0x04:
            return None
        return parse_timing(self.mcu_read(MCU_TIMING, 7))

    # --- démarrage -----------------------------------------------------------------------------
    def boot(self):
        """Initialisation officielle + firmwares en RAM + démarrage de l'ARM (boîtier fraîchement branché)."""
        steps = gl310_boot.official_init_steps() + gl310_boot.build_steps(
            gl310_boot.FW_AUDIO.read_bytes(), gl310_boot.FW_VIDEO.read_bytes())
        for step in steps:
            gl310_boot.Box.run(self, step)
        if not self.firmware_loaded():
            raise RuntimeError("firmware non chargé après le démarrage")

    def start_encoder(self, settings: Settings):
        replacements = {
            (REG_RATE, TRACE_RATE_CONTROL): rate_control(settings),
            (REG_GOP, TRACE_GOP_LOOP_FILTER): gop_loop_filter(settings),
            (REG_AUDIO, TRACE_AUDIO_CONTROL_PARAM): audio_control_param(settings),
        }

        def patch(_line, b):
            if b[:4] == b"\x01\x01\x01\x00":
                key = struct.unpack("<II", b[4:12])
                if key in replacements:   # seulement l'écriture du bloc de configuration (valeur de la trace)
                    return b[:8] + struct.pack("<I", replacements[key])
            if b[0] == 0x0B and b[8] == MCU_BRIGHTNESS:
                return b[:9] + bytes([settings.brightness])
            if b[0] == 0x0B and b[8] == MCU_CONTRAST:
                return b[:9] + bytes([settings.contrast])
            return b

        if not gl310_encode.replay(self, audio_pass=False, official=True, patch=patch):
            raise RuntimeError("démarrage de l'encodeur refusé")

    def stop_encoder(self):
        """0x02 StopEncoder puis 0xF3 SystemClose (vérifié : l'encodeur peut ensuite être relancé)."""
        drain(self)
        self.mbox_send(0x02, 0, 0, [])
        time.sleep(0.3)
        drain(self)
        self.mbox_send(0xF3, 0, 0, [])
        time.sleep(0.3)
        drain(self)

    def apply_image(self, settings: Settings):
        """Luminosité et contraste, à chaud (comme « Set brightness/contrast to HW » du pilote)."""
        self.mcu_write(MCU_BRIGHTNESS, bytes([settings.brightness]))
        self.mcu_write(MCU_CONTRAST, bytes([settings.contrast]))
        self.mcu_write(MCU_APPLY_IMAGE, b"\x01")

    # --- flux ----------------------------------------------------------------------------------
    def poll_ts(self) -> bytes | None:
        """Traite un message du firmware s'il y en a un : renvoie le morceau de MPEG-TS, sinon None."""
        st = self.reg_read(0x06C8)[0]
        if not st & 1:
            return None
        msg, p0, addr, _addr2, size, pts = self.reg_read(0x06B0, 6)
        data = None
        if (msg & 0xFF) == MSG_FRAME and (p0 & 0xFFFF) == 0x83:
            data = word_swap(self.mem_read(addr, size * 4))
        release(self, st, msg, p0, size, pts)   # toujours rendre le tampon, même s'il n'est pas lu
        return data

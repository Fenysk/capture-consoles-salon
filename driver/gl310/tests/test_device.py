#!/usr/bin/env python3
"""Contrat du pilote noyau gl310, vérifié sur le vrai boîtier (module chargé, console allumée en HDMI).

Lancer : python3 -m unittest driver/gl310/tests/test_device.py -v
Interface attendue (consommée par GL310 Studio natif) :
  /dev/videoN dont VIDIOC_QUERYCAP.driver == "gl310", capture + read()
  format : V4L2_PIX_FMT_MPEG 1920x1080 ; read() renvoie du MPEG-TS (H.264 PID 0x44, AAC PID 0x45)
  contrôles : luminosité, contraste (à chaud), débit vidéo, GOP, débit AAC, sortie AUDIO OUT (relance de l'encodeur)
  état : VIDIOC_ENUMINPUT (V4L2_IN_ST_NO_SIGNAL) et VIDIOC_QUERY_DV_TIMINGS (résolution, fréquence)
"""
import fcntl
import glob
import os
import select
import struct
import subprocess
import tempfile
import time
import unittest

VIDIOC_QUERYCAP = 0x80685600
VIDIOC_G_FMT = 0xC0D05604
VIDIOC_ENUMINPUT = 0xC050561A
VIDIOC_G_CTRL = 0xC008561B
VIDIOC_S_CTRL = 0xC008561C
VIDIOC_QUERYCTRL = 0xC0445624
VIDIOC_QUERY_DV_TIMINGS = 0x80845663

V4L2_CAP_VIDEO_CAPTURE = 0x00000001
V4L2_CAP_READWRITE = 0x01000000
V4L2_PIX_FMT_MPEG = 0x4745504D
V4L2_IN_ST_NO_SIGNAL = 0x00000002

CID_BRIGHTNESS = 0x00980900
CID_CONTRAST = 0x00980901
CID_VIDEO_BITRATE = 0x009909CF
CID_GOP_SIZE = 0x009909CB
CID_AAC_BITRATE = 0x0099096E
CID_AUDIO_OUT = 0x00982800        # V4L2_CID_USER_BASE + 0x1f00, privé au pilote gl310

TS_PACKET = 188
PID_VIDEO, PID_AUDIO = 0x44, 0x45


def find_gl310():
    for path in sorted(glob.glob("/dev/video*")):
        try:
            fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        except OSError:
            continue
        try:
            cap = bytearray(104)
            fcntl.ioctl(fd, VIDIOC_QUERYCAP, cap)
            if cap[:16].rstrip(b"\0") == b"gl310":
                return path
        except OSError:
            pass
        finally:
            os.close(fd)
    return None


def queryctrl(fd, cid):
    buf = bytearray(struct.pack("<II32siiiiII", cid, 0, b"", 0, 0, 0, 0, 0, 0, 0))
    fcntl.ioctl(fd, VIDIOC_QUERYCTRL, buf)
    _id, _type, name, lo, hi, step, default, flags, _r0, _r1 = struct.unpack("<II32siiiiII", buf)
    return {"name": name.rstrip(b"\0").decode(), "min": lo, "max": hi, "step": step, "default": default}


def get_ctrl(fd, cid):
    buf = bytearray(struct.pack("<Ii", cid, 0))
    fcntl.ioctl(fd, VIDIOC_G_CTRL, buf)
    return struct.unpack("<Ii", buf)[1]


def set_ctrl(fd, cid, value):
    fcntl.ioctl(fd, VIDIOC_S_CTRL, bytearray(struct.pack("<Ii", cid, value)))


def read_for(fd, seconds):
    data = bytearray()
    end = time.time() + seconds
    while time.time() < end:
        ready, _, _ = select.select([fd], [], [], 0.5)
        if ready:
            data += os.read(fd, 1 << 20)
    return bytes(data)


def ts_pids(data):
    start = data.find(b"\x47")
    while start >= 0 and not all(data[start + k * TS_PACKET:start + k * TS_PACKET + 1] == b"\x47" for k in range(5)):
        start = data.find(b"\x47", start + 1)
    pids, bad = {}, 0
    for off in range(max(start, 0), len(data) - TS_PACKET + 1, TS_PACKET):
        if data[off] != 0x47:
            bad += 1
            continue
        pid = ((data[off + 1] & 0x1F) << 8) | data[off + 2]
        pids[pid] = pids.get(pid, 0) + 1
    return start, pids, bad


class Gl310DeviceContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = find_gl310()

    def setUp(self):
        self.assertIsNotNone(self.path, "aucun /dev/video du pilote gl310 (module chargé ? boîtier branché ?)")
        self.fd = os.open(self.path, os.O_RDWR)

    def tearDown(self):
        os.close(self.fd)

    def test_capabilities(self):
        # Quand on interroge le périphérique, il se présente comme une capture vidéo lisible par read().
        cap = bytearray(104)
        fcntl.ioctl(self.fd, VIDIOC_QUERYCAP, cap)
        device_caps = struct.unpack_from("<I", cap, 88)[0]
        self.assertTrue(device_caps & V4L2_CAP_VIDEO_CAPTURE)
        self.assertTrue(device_caps & V4L2_CAP_READWRITE)

    def test_format_is_mpeg_1080p(self):
        fmt = bytearray(struct.pack("<I", 1) + bytes(204))
        fcntl.ioctl(self.fd, VIDIOC_G_FMT, fmt)
        width, height, pixfmt = struct.unpack_from("<III", fmt, 8)
        self.assertEqual((width, height, pixfmt), (1920, 1080, V4L2_PIX_FMT_MPEG))

    def test_controls(self):
        expected = {
            CID_BRIGHTNESS: (0, 255, 0x80),
            CID_CONTRAST: (0, 255, 0x3E),
            CID_VIDEO_BITRATE: (2_000_000, 30_000_000, 8_000_000),
            CID_GOP_SIZE: (15, 120, 60),
            CID_AAC_BITRATE: (128_000, 256_000, 128_000),
            CID_AUDIO_OUT: (0, 1, 1),
        }
        for cid, (lo, hi, default) in expected.items():
            q = queryctrl(self.fd, cid)
            self.assertEqual((q["min"], q["max"], q["default"]), (lo, hi, default), q["name"])

    def test_signal_status(self):
        # L'état du signal HDMI est lisible ; avec une console allumée, il décrit du 1080p.
        inp = bytearray(struct.pack("<I", 0) + bytes(76))
        fcntl.ioctl(self.fd, VIDIOC_ENUMINPUT, inp)
        status = struct.unpack_from("<I", inp, 56)[0]
        self.assertFalse(status & V4L2_IN_ST_NO_SIGNAL, "pas de signal HDMI : allume la console")
        timings = bytearray(132)
        fcntl.ioctl(self.fd, VIDIOC_QUERY_DV_TIMINGS, timings)
        width, height = struct.unpack_from("<II", timings, 4)
        self.assertEqual((width, height), (1920, 1080))

    def test_read_returns_mpeg_ts_with_video_and_audio(self):
        # Quand on lit le périphérique, on reçoit un MPEG-TS continu avec la vidéo et l'audio.
        data = read_for(self.fd, 4)
        self.assertGreater(len(data), 500_000, "moins de 1 Mbit/s reçu")
        start, pids, bad = ts_pids(data)
        self.assertGreaterEqual(start, 0, "aucun paquet TS aligné")
        self.assertEqual(bad, 0, "paquets TS désalignés")
        self.assertGreater(pids.get(PID_VIDEO, 0), 100)
        self.assertGreater(pids.get(PID_AUDIO, 0), 10)
        with tempfile.NamedTemporaryFile(suffix=".ts") as f:
            f.write(data[start:])
            f.flush()
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,width,height", "-of", "csv=p=0", f.name],
                capture_output=True, text=True, check=True).stdout
        self.assertIn("h264,1920,1080", probe)
        self.assertIn("aac", probe)

    def test_brightness_changes_live_without_stopping_stream(self):
        # Quand on change la luminosité pendant la lecture, le flux continue.
        read_for(self.fd, 1)
        set_ctrl(self.fd, CID_BRIGHTNESS, 0x60)
        self.assertEqual(get_ctrl(self.fd, CID_BRIGHTNESS), 0x60)
        self.assertGreater(len(read_for(self.fd, 2)), 200_000)
        set_ctrl(self.fd, CID_BRIGHTNESS, 0x80)

    def test_bitrate_change_restarts_encoder_at_new_rate(self):
        # Quand on change le débit pendant la lecture, l'encodeur repart et respecte le nouveau débit.
        read_for(self.fd, 1)
        set_ctrl(self.fd, CID_VIDEO_BITRATE, 4_000_000)
        read_for(self.fd, 3)                       # relance + première image clé
        low = len(read_for(self.fd, 4)) * 8 / 4
        set_ctrl(self.fd, CID_VIDEO_BITRATE, 8_000_000)
        self.assertLess(low, 6_000_000, f"débit mesuré {low / 1e6:.1f} Mbit/s pour 4 demandés")
        self.assertGreater(low, 1_000_000, f"débit mesuré {low / 1e6:.1f} Mbit/s : flux arrêté ?")

    def test_out_of_range_bitrate_is_refused(self):
        # Si un débit hors bornes est demandé, alors il est refusé (ERANGE ou EINVAL).
        with self.assertRaises(OSError):
            set_ctrl(self.fd, CID_VIDEO_BITRATE, 50_000_000)

    def test_second_reader_is_busy(self):
        # Tant qu'un lecteur reçoit le flux, un second lecteur est refusé (EBUSY).
        read_for(self.fd, 0.5)
        other = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            with self.assertRaises(OSError) as ctx:
                os.read(other, 4096)
            self.assertEqual(ctx.exception.errno, 16)
        finally:
            os.close(other)


if __name__ == "__main__":
    unittest.main()

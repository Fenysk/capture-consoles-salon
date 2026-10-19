"""Contrat de GL310 Studio (python3 -m unittest studio.test_model). Ne pas modifier : satisfaire en changeant le code."""
import unittest

from studio.model import (Err, Ok, Recording, Settings, Signal, Streaming, audio_control_param, gop_loop_filter,
                          parse_timing, rate_control, status_to_json, validate)


class EncoderRegisters(unittest.TestCase):
    def test_debit_video_remplace_seulement_le_debit(self):
        # Quand le débit vidéo vaut 8000 kbit/s, RATE_CONTROL doit garder qp_update=120 de la trace.
        self.assertEqual(rate_control(Settings(video_kbps=8000)), 0x00781F40)

    def test_intervalle_images_cles(self):
        # Quand l'intervalle vaut 60 images, GOP_LOOP_FILTER doit garder le reste de la trace.
        self.assertEqual(gop_loop_filter(Settings(keyframe_interval=60)), 0xF199003C)

    def test_audio_out_et_debit_audio(self):
        # Quand AUDIO OUT est actif et l'audio à 192 kbit/s, le bit 16 et les bits 0-9 doivent changer.
        self.assertEqual(audio_control_param(Settings(audio_kbps=192, audio_out=True)), 0x211710C0)

    def test_audio_out_coupe(self):
        # Quand AUDIO OUT est coupé, le bit 16 doit être à 0.
        self.assertEqual(audio_control_param(Settings(audio_kbps=128, audio_out=False)), 0x21161080)


class Validation(unittest.TestCase):
    def test_debit_hors_liste_refuse(self):
        # Si un débit hors liste est demandé, alors la validation doit renvoyer une erreur.
        self.assertIsInstance(validate(Settings(video_kbps=7000)), Err)

    def test_luminosite_hors_plage_refusee(self):
        # Si la luminosité sort de 0-255, alors la validation doit renvoyer une erreur.
        self.assertIsInstance(validate(Settings(brightness=300)), Err)

    def test_reglages_par_defaut_acceptes(self):
        # Quand les réglages par défaut sont validés, le système doit les accepter.
        self.assertIsInstance(validate(Settings()), Ok)


class Timing(unittest.TestCase):
    def test_switch2_1080p60(self):
        # Quand le micro renvoie 98 78 80 65 44 38 17, le signal doit être 1920×1080 à 60 Hz.
        self.assertEqual(parse_timing(bytes.fromhex("98788065443817")), Signal(1920, 1080, 60))


class StatusJson(unittest.TestCase):
    def test_streaming_en_json(self):
        # Quand le boîtier diffuse et enregistre, le JSON doit porter l'état et l'enregistrement.
        status = Streaming(signal=Signal(1920, 1080, 60), settings=Settings(), mbps=9.1, messages_per_s=34.0,
                           recording=Recording(path="recordings/a.ts", seconds=12.5, bytes=1000))
        data = status_to_json(status)
        self.assertEqual(data["state"], "streaming")
        self.assertEqual(data["recording"]["seconds"], 12.5)
        self.assertEqual(data["signal"], {"width": 1920, "height": 1080, "refresh_hz": 60})
        self.assertEqual(data["settings"]["video_kbps"], 8000)


if __name__ == "__main__":
    unittest.main()

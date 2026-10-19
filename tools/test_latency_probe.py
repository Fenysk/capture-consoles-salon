#!/usr/bin/env python3
"""Tests de la partie pure de latency_probe.py (code-barres de l'heure, calcul du délai). Lancer : python3 tools/test_latency_probe.py"""
import unittest

import latency_probe as lp


def rasterize(blocks, width, height):
    """Image grise width×height (octets) des blocs, donnés en coordonnées 1920×1080."""
    img = bytearray(width * height)
    sx, sy = width / lp.FRAME_W, height / lp.FRAME_H
    for x, y, w, h, white in blocks:
        if not white:
            continue
        for row in range(int(y * sy), int((y + h) * sy)):
            start = row * width
            img[start + int(x * sx):start + int((x + w) * sx)] = b"\xff" * (int((x + w) * sx) - int(x * sx))
    return bytes(img)


class CodeBarres(unittest.TestCase):
    def test_aller_retour(self):
        # Quand on dessine une valeur puis qu'on relit l'image réduite, on doit retrouver la même valeur.
        for value in (0, 1, 0x5A5A5, lp.CODE_MOD - 1, 123456):
            frame = rasterize(lp.code_blocks(value), 480, 270)
            self.assertEqual(lp.read_code(frame, 480, 270), value)

    def test_image_dechiree_refusee(self):
        # Si les deux rangées ne viennent pas de la même image (déchirement), alors la lecture est refusée.
        top = [b for b in lp.code_blocks(0) if b[1] < lp.ROW2_Y]
        bottom = [b for b in lp.code_blocks(1) if b[1] >= lp.ROW2_Y]
        self.assertIsNone(lp.read_code(rasterize(top + bottom, 480, 270), 480, 270))

    def test_image_noire_refusee(self):
        # Si l'image ne contient pas le code (console, écran noir), alors rien n'est lu.
        self.assertIsNone(lp.read_code(bytes(480 * 270), 480, 270))


class Delai(unittest.TestCase):
    def test_delai_simple(self):
        now = 1_000_000_123_456
        self.assertEqual(lp.delay_ms(lp.code_of(now - 1600), now), 1600)

    def test_delai_apres_le_tour_du_compteur(self):
        # Quand le compteur repasse par zéro entre l'affichage et la réception, le délai reste juste.
        now = lp.CODE_MOD * 1000 + 300
        self.assertEqual(lp.delay_ms(lp.code_of(now - 1600), now), 1600)

    def test_resume(self):
        text = lp.summary([1500, 1600, 1700, 1600, 1650])
        self.assertIn("médiane 1600 ms", text)
        self.assertIn("5 images", text)


if __name__ == "__main__":
    unittest.main()

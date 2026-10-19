#!/usr/bin/env python3
"""Balayage I2C en lecture seule du GL310, avec la commande de lecture 0C (vérifiée sur le micro 0x15).

  0C 01 01 00 <adresse7> 00 00 00 <registre>  ->  réponse <donnée> <état>

Pour chaque adresse 7 bits, on lit un ou plusieurs registres et on affiche la réponse brute.
Les adresses qui répondent autrement que les adresses vides révèlent une puce.
Aucune écriture : seul l'octet de registre à lire est envoyé, comme pour la LED.

Usage :
  gl310_i2cscan.py                     balaye 0x03-0x77, registre 0x00
  gl310_i2cscan.py --dev 0x48 --regs 0x00-0x03   lit des registres d'une adresse précise
"""
import argparse

import usb.core
import usb.util

EP_CMD_OUT, EP_CMD_IN = 0x04, 0x83


def read_reg(dev, addr7, reg):
    dev.write(EP_CMD_OUT, bytes([0x0C, 0x01, 0x01, 0x00, addr7, 0, 0, 0, reg]), 500)
    try:
        return bytes(dev.read(EP_CMD_IN, 512, 500))
    except usb.core.USBTimeoutError:
        return None


def parse_range(text):
    if "-" in text:
        a, b = (int(x, 0) for x in text.split("-"))
        return list(range(a, b + 1))
    return [int(x, 0) for x in text.split(",")]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dev", type=lambda s: int(s, 0), help="adresse 7 bits précise")
    parser.add_argument("--regs", default="0x00", help="registres, ex. 0x00-0x03 ou 0x00,0x2c")
    args = parser.parse_args()

    dev = None
    for pid in (0xC835, 0xD835):
        dev = usb.core.find(idVendor=0x07CA, idProduct=pid)
        if dev is not None:
            break
    if dev is None:
        raise SystemExit("LGP Lite introuvable.")
    usb.util.claim_interface(dev, 0)

    regs = parse_range(args.regs)
    addrs = [args.dev] if args.dev is not None else list(range(0x03, 0x78))
    answers = {}
    for a in addrs:
        answers[a] = [read_reg(dev, a, r) for r in regs]

    # Réponse la plus fréquente = adresse vide (sauf si on vise une adresse précise).
    flat = [tuple(v) for v in answers.values()]
    common = max(set(flat), key=flat.count) if args.dev is None else None
    for a, values in answers.items():
        if args.dev is None and tuple(values) == common:
            continue
        shown = "  ".join(f"r{r:02x}:{v.hex(' ') if v else 'rien'}" for r, v in zip(regs, values))
        print(f"0x{a:02x} (8 bits 0x{a * 2:02x}) : {shown}")
    if common is not None:
        print(f"Réponse des {flat.count(common)} autres adresses : " + "  ".join(v.hex(' ') if v else "rien" for v in common))


if __name__ == "__main__":
    main()

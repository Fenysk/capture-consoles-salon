#!/usr/bin/env python3
"""Génère les tables de commandes du GL310 à partir de la trace Windows (vendor/octv_gears_lgp/capture_sequence) :
studio-rs/src/tables.rs (GL310 Studio, Rust) et driver/gl310/gl310_tables.h (pilote noyau C, mis de côté).

La trace reste la seule source de vérité : le pilote noyau rejoue les mêmes commandes que les outils Python
(gl310_boot.official_init_steps et gl310_encode.replay), sans recopie à la main.

  gl310_official_init[]  lignes 1 à 224   (réglages du pont USB + 206 écritures I2C)
  gl310_encoder_start[]  lignes 371 à 757 (boîte aux lettres, bloc de configuration, micro, I2C)

Chaque commande porte des drapeaux que le pilote applique au moment de l'envoi :
  GL310_F_EXPECT      une réponse arrive sur EP 0x83
  GL310_F_WAIT_MBOX   lecture de 0x6CC : attendre que le firmware ait consommé le message précédent
  GL310_F_RATE/GOP/AUDIO   écriture du bloc de configuration à remplacer par le réglage (valeur de la trace)
  GL310_F_BRIGHTNESS/CONTRAST   écriture micro 0x43/0x44 à remplacer par le réglage
  GL310_F_RTC         écriture micro 0x30 : date et heure actuelles en BCD

Refuse de générer si la trace contient une écriture du profil permanent du micro (0x47/0x49).

Usage : gen_gl310_tables.py [--check]   (--check : échoue si le fichier généré n'est pas à jour)
"""
import argparse
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRACE = ROOT / "vendor/octv_gears_lgp/capture_sequence"
OUT_C = ROOT / "driver/gl310/gl310_tables.h"
OUT_RUST = ROOT / "studio-rs/src/tables.rs"

TRACE_RATE_CONTROL = 0x00787530
TRACE_GOP_LOOP_FILTER = 0xF199001E
TRACE_AUDIO_CONTROL_PARAM = 0x21161080
CONFIG_PATCHES = {
    (0x06EC, TRACE_RATE_CONTROL): "GL310_F_RATE",
    (0x06E0, TRACE_GOP_LOOP_FILTER): "GL310_F_GOP",
    (0x06D4, TRACE_AUDIO_CONTROL_PARAM): "GL310_F_AUDIO",
}
MCU_PATCHES = {0x43: "GL310_F_BRIGHTNESS", 0x44: "GL310_F_CONTRAST", 0x30: "GL310_F_RTC"}
MCU_PROFILE_REGS = (0x47, 0x49)
MAX_LEN = 16


def rows(first, last):
    for n, line in enumerate(TRACE.read_text().splitlines(), 1):
        if first <= n <= last:
            t = line.split()
            yield n, t[0] == "1", int(t[1]), bytes(int(x, 16) for x in t[2:])


def flags_of(b):
    flags = []
    if b[:4] == b"\x01\x00\x01\x00" and struct.unpack("<I", b[4:8])[0] == 0x06CC:
        flags.append("GL310_F_WAIT_MBOX")
    if b[:4] == b"\x01\x01\x01\x00":
        key = struct.unpack("<II", b[4:12])
        if key in CONFIG_PATCHES:
            flags.append(CONFIG_PATCHES[key])
    if b[0] == 0x0B:
        if b[8] in MCU_PROFILE_REGS:
            raise SystemExit(f"écriture du profil permanent du micro ({b[8]:#04x}) dans la trace : refus")
        if b[8] in MCU_PATCHES:
            flags.append(MCU_PATCHES[b[8]])
    return flags


def table(first, last, counts):
    """Lignes (n° de ligne, drapeaux, octets) de la trace, toutes sur EP 0x04."""
    out = []
    for n, expect, ep, b in rows(first, last):
        if ep != 4:
            raise SystemExit(f"ligne {n} : endpoint {ep} inattendu (seul EP 0x04 est rejoué)")
        if b[0] not in (0x01, 0x05, 0x0B, 0x0C):
            raise SystemExit(f"ligne {n} : commande {b[0]:#04x} inattendue")
        if len(b) > MAX_LEN:
            raise SystemExit(f"ligne {n} : commande de {len(b)} octets (> {MAX_LEN})")
        flags = flags_of(b) + (["GL310_F_EXPECT"] if expect else [])
        for f in flags:
            counts[f] = counts.get(f, 0) + 1
        out.append((n, flags, b))
    return out


TABLES = (("gl310_official_init", "OFFICIAL_INIT", 1, 224), ("gl310_encoder_start", "ENCODER_START", 371, 757))


def build():
    init_counts, start_counts = {}, {}
    init = table(1, 224, init_counts)
    start = table(371, 757, start_counts)
    for flag in ("GL310_F_RATE", "GL310_F_GOP", "GL310_F_AUDIO", "GL310_F_BRIGHTNESS", "GL310_F_CONTRAST", "GL310_F_RTC"):
        if start_counts.get(flag, 0) != 1:
            raise SystemExit(f"{flag} : {start_counts.get(flag, 0)} occurrence(s) dans le démarrage (1 attendue)")
    if any(k != "GL310_F_EXPECT" for k in init_counts):
        raise SystemExit(f"drapeaux inattendus dans l'initialisation officielle : {init_counts}")
    return [init, start]


def render_c(tables):
    lines = [
        "/* SPDX-License-Identifier: GPL-2.0 */",
        "/* Généré par tools/gen_gl310_tables.py à partir de la trace Windows : ne pas modifier à la main. */",
        "#ifndef GL310_TABLES_H",
        "#define GL310_TABLES_H",
        "",
    ]
    for (c_name, _, first, last), rows_ in zip(TABLES, tables):
        lines += [f"/* Trace lignes {first} à {last}. */", f"static const struct gl310_cmd {c_name}[] = {{"]
        for n, flags, b in rows_:
            data = ", ".join(f"0x{x:02x}" for x in b)
            lines.append(f"\t{{ {len(b):2}, {' | '.join(flags) or '0'}, {{ {data} }} }}, /* {n} */")
        lines += ["};", ""]
    return "\n".join(lines + ["#endif"]) + "\n"


def render_rust(tables):
    lines = [
        "//! Généré par tools/gen_gl310_tables.py à partir de la trace Windows : ne pas modifier à la main.",
        "",
        "/// Une réponse arrive sur EP 0x83.",
        "pub const EXPECT: u8 = 1 << 0;",
        "/// Lecture de 0x6CC : attendre que le firmware ait consommé le message précédent.",
        "pub const WAIT_MBOX: u8 = 1 << 1;",
        "/// Écritures à remplacer par les réglages courants (valeur de la trace).",
        "pub const RATE: u8 = 1 << 2;",
        "pub const GOP: u8 = 1 << 3;",
        "pub const AUDIO: u8 = 1 << 4;",
        "pub const BRIGHTNESS: u8 = 1 << 5;",
        "pub const CONTRAST: u8 = 1 << 6;",
        "/// Écriture micro 0x30 : date et heure actuelles en BCD.",
        "pub const RTC: u8 = 1 << 7;",
        "",
        "pub struct Cmd {",
        "    pub flags: u8,",
        "    pub bytes: &'static [u8],",
        "}",
        "",
    ]
    for (_, rust_name, first, last), rows_ in zip(TABLES, tables):
        lines += [f"/// Trace lignes {first} à {last}.", f"pub static {rust_name}: &[Cmd] = &["]
        for n, flags, b in rows_:
            data = ", ".join(f"0x{x:02x}" for x in b)
            names = " | ".join(f.removeprefix("GL310_F_") for f in flags) or "0"
            lines.append(f"    Cmd {{ flags: {names}, bytes: &[{data}] }}, // {n}")
        lines += ["];", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    tables = build()
    outputs = {OUT_C: render_c(tables), OUT_RUST: render_rust(tables)}
    if args.check:
        stale = [str(path) for path, text in outputs.items() if not path.exists() or path.read_text() != text]
        if stale:
            sys.exit(f"Pas à jour : {', '.join(stale)} — relance tools/gen_gl310_tables.py")
        print("Tables à jour.")
        return
    for path, text in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        print(f"Écrit : {path}")


if __name__ == "__main__":
    main()

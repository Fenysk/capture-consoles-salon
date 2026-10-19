"""Types purs de GL310 Studio : réglages, signal HDMI, état du boîtier, encodage des registres.

Aucun accès USB ici : tout est testable sans le boîtier (voir test_model.py).
Les valeurs de base sont celles de la trace Windows (bloc de configuration de l'encodeur) ;
on ne remplace que les champs dont la structure est prouvée par le pilote (CQLCodec_Set) :
  RATE_CONTROL       bits 0-15 débit (kbit/s), 16-27 qp_update, 28-29 mode, 30 fixed, 31 vbr
  GOP_LOOP_FILTER    bits 0-15 intervalle entre images clés (en images)
  AUDIO_CONTROL_PARAM bits 0-9 débit audio (kbit/s), 16 aou_pass_en (son vers AUDIO OUT)
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal, Union

# Valeurs de la trace (registres 0x6EC, 0x6E0, 0x6D4 du bloc de configuration).
TRACE_RATE_CONTROL = 0x00787530
TRACE_GOP_LOOP_FILTER = 0xF199001E
TRACE_AUDIO_CONTROL_PARAM = 0x21161080

VIDEO_KBPS_CHOICES = (4000, 6000, 8000, 12000, 20000, 30000)
KEYFRAME_CHOICES = (30, 60)            # en images, à 30 i/s : 1 s ou 2 s
AUDIO_KBPS_CHOICES = (128, 160, 192, 256)


@dataclass(frozen=True)
class Settings:
    video_kbps: int = 8000
    keyframe_interval: int = 60
    audio_kbps: int = 128
    audio_out: bool = True             # aou_pass_en : son sur la prise AUDIO OUT
    brightness: int = 0x80             # micro 0x43, à chaud
    contrast: int = 0x3E               # micro 0x44, à chaud


@dataclass(frozen=True)
class Ok:
    value: Settings
    ok: Literal[True] = True


@dataclass(frozen=True)
class Err:
    error: str
    ok: Literal[False] = False


Result = Union[Ok, Err]


def validate(settings: Settings) -> Result:
    """Refuse toute valeur hors des choix prévus (le firmware n'a été vérifié que sur ces valeurs)."""
    if settings.video_kbps not in VIDEO_KBPS_CHOICES:
        return Err(f"débit vidéo {settings.video_kbps} kbit/s non proposé {VIDEO_KBPS_CHOICES}")
    if settings.keyframe_interval not in KEYFRAME_CHOICES:
        return Err(f"intervalle d'images clés {settings.keyframe_interval} non proposé {KEYFRAME_CHOICES}")
    if settings.audio_kbps not in AUDIO_KBPS_CHOICES:
        return Err(f"débit audio {settings.audio_kbps} kbit/s non proposé {AUDIO_KBPS_CHOICES}")
    for name in ("brightness", "contrast"):
        if not 0 <= getattr(settings, name) <= 255:
            return Err(f"{name} doit être entre 0 et 255")
    return Ok(settings)


def rate_control(settings: Settings) -> int:
    return (TRACE_RATE_CONTROL & ~0xFFFF) | settings.video_kbps


def gop_loop_filter(settings: Settings) -> int:
    return (TRACE_GOP_LOOP_FILTER & ~0xFFFF) | settings.keyframe_interval


AOU_PASS_EN = 1 << 16


def audio_control_param(settings: Settings) -> int:
    value = (TRACE_AUDIO_CONTROL_PARAM & ~0x3FF & ~AOU_PASS_EN) | settings.audio_kbps
    return value | (AOU_PASS_EN if settings.audio_out else 0)


@dataclass(frozen=True)
class Signal:
    width: int
    height: int
    refresh_hz: int


STANDARD_RATES = (24, 25, 30, 50, 60, 120)
PCLK_NUMERATOR_KHZ = 0x34BC00          # horloge pixel (kHz) = 0x34BC00 / compteur (c985_nuc100.c)


def parse_timing(raw7: bytes) -> Signal:
    """Registre 0x1D du micro (7 octets) : totaux, actifs et compteur d'horloge (cf. c985_nuc100.c)."""
    r = raw7
    h_total = ((r[1] & 0x0F) << 8) | r[0]
    h_active = ((r[1] & 0xF0) << 4) | r[2]
    v_total = ((r[4] & 0x0F) << 8) | r[3]
    v_active = ((r[4] & 0xF0) << 4) | r[5]
    refresh = 0
    if r[6] and h_total and v_total:
        hz = PCLK_NUMERATOR_KHZ / r[6] * 1000 / (h_total * v_total)
        refresh = min(STANDARD_RATES, key=lambda std: abs(std - hz))
    return Signal(h_active, v_active, refresh)


# --- État du boîtier : un seul état à la fois, chacun avec ses propres champs. ---

@dataclass(frozen=True)
class Absent:
    state: Literal["absent"] = "absent"


@dataclass(frozen=True)
class Idle:
    signal: Signal | None
    firmware_loaded: bool
    state: Literal["idle"] = "idle"


@dataclass(frozen=True)
class Starting:
    step: str
    state: Literal["starting"] = "starting"


@dataclass(frozen=True)
class Recording:
    path: str
    seconds: float
    bytes: int


@dataclass(frozen=True)
class Streaming:
    signal: Signal | None
    settings: Settings
    mbps: float
    messages_per_s: float
    recording: Recording | None = None
    state: Literal["streaming"] = "streaming"


@dataclass(frozen=True)
class Failed:
    error: str
    state: Literal["failed"] = "failed"


Status = Union[Absent, Idle, Starting, Streaming, Failed]


def status_to_json(status: Status) -> dict:
    return asdict(status)

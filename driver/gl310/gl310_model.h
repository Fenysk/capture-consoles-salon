/* SPDX-License-Identifier: GPL-2.0 */
/*
 * gl310_model.h - réglages du GL310 et leur encodage dans les registres du firmware.
 *
 * Code pur, sans USB ni noyau : compilé dans le module ET dans tests/test_model.c.
 * Mêmes règles et mêmes valeurs de test que studio/model.py (le prototype Python) :
 *   RATE_CONTROL        bits 0-15 débit vidéo (kbit/s), le reste comme dans la trace
 *   GOP_LOOP_FILTER     bits 0-15 intervalle entre images clés (en images)
 *   AUDIO_CONTROL_PARAM bits 0-9 débit audio (kbit/s), bit 16 aou_pass_en (son sur la prise AUDIO OUT)
 * Le registre 0x1D du micro (7 octets) donne la résolution et la fréquence du signal HDMI.
 */
#ifndef GL310_MODEL_H
#define GL310_MODEL_H

#ifdef __KERNEL__
#include <linux/types.h>
#include <linux/math64.h>
#else
#include <stdbool.h>
#include <stdint.h>
typedef uint8_t u8;
typedef uint32_t u32;
typedef uint64_t u64;
static inline u64 div64_u64(u64 a, u64 b) { return a / b; }
#endif

/* Valeurs de la trace Windows (bloc de configuration 0x6EC, 0x6E0, 0x6D4). */
#define GL310_TRACE_RATE_CONTROL        0x00787530u
#define GL310_TRACE_GOP_LOOP_FILTER     0xF199001Eu
#define GL310_TRACE_AUDIO_CONTROL_PARAM 0x21161080u
#define GL310_AOU_PASS_EN               (1u << 16)

/* Bornes acceptées par le pilote (le firmware a été vérifié à 4, 9 et 30 Mbit/s, GOP 30 et 60). */
#define GL310_VIDEO_KBPS_MIN    2000u
#define GL310_VIDEO_KBPS_MAX    30000u
#define GL310_KEYFRAME_MIN      15u
#define GL310_KEYFRAME_MAX      120u
#define GL310_AUDIO_KBPS_MIN    128u
#define GL310_AUDIO_KBPS_MAX    256u

struct gl310_settings {
	u32 video_kbps;
	u32 keyframe_interval;  /* en images */
	u32 audio_kbps;
	bool audio_out;         /* aou_pass_en */
	u8 brightness;          /* micro 0x43, à chaud */
	u8 contrast;            /* micro 0x44, à chaud */
};

#define GL310_DEFAULT_SETTINGS { \
	.video_kbps = 8000, .keyframe_interval = 60, .audio_kbps = 128, \
	.audio_out = true, .brightness = 0x80, .contrast = 0x3E }

/* Signal HDMI présent : résolution active et fréquence arrondie à une valeur standard. */
struct gl310_signal {
	u32 width;
	u32 height;
	u32 refresh_hz;
	u32 h_total;
	u32 v_total;
	u64 pixelclock_hz;
};

#define GL310_PCLK_NUMERATOR_KHZ 0x34BC00u  /* horloge pixel (kHz) = 0x34BC00 / compteur */

static inline bool gl310_settings_valid(const struct gl310_settings *s)
{
	return s->video_kbps >= GL310_VIDEO_KBPS_MIN && s->video_kbps <= GL310_VIDEO_KBPS_MAX &&
	       s->keyframe_interval >= GL310_KEYFRAME_MIN && s->keyframe_interval <= GL310_KEYFRAME_MAX &&
	       s->audio_kbps >= GL310_AUDIO_KBPS_MIN && s->audio_kbps <= GL310_AUDIO_KBPS_MAX;
}

static inline u32 gl310_rate_control(const struct gl310_settings *s)
{
	return (GL310_TRACE_RATE_CONTROL & ~0xFFFFu) | s->video_kbps;
}

static inline u32 gl310_gop_loop_filter(const struct gl310_settings *s)
{
	return (GL310_TRACE_GOP_LOOP_FILTER & ~0xFFFFu) | s->keyframe_interval;
}

static inline u32 gl310_audio_control_param(const struct gl310_settings *s)
{
	u32 v = (GL310_TRACE_AUDIO_CONTROL_PARAM & ~0x3FFu & ~GL310_AOU_PASS_EN) | s->audio_kbps;

	return s->audio_out ? v | GL310_AOU_PASS_EN : v;
}

/* Renvoie false si le compteur d'horloge ou les totaux sont nuls (pas de signal exploitable). */
static inline bool gl310_parse_timing(const u8 r[7], struct gl310_signal *out)
{
	static const u32 standard_rates[] = { 24, 25, 30, 50, 60, 120 };
	u32 h_total = ((r[1] & 0x0Fu) << 8) | r[0];
	u32 v_total = ((r[4] & 0x0Fu) << 8) | r[3];
	u64 hz_milli, best_gap = ~0ull;
	unsigned int i;

	if (!r[6] || !h_total || !v_total)
		return false;
	out->width = ((r[1] & 0xF0u) << 4) | r[2];
	out->height = ((r[4] & 0xF0u) << 4) | r[5];
	out->h_total = h_total;
	out->v_total = v_total;
	out->pixelclock_hz = div64_u64((u64)GL310_PCLK_NUMERATOR_KHZ * 1000, r[6]);
	hz_milli = div64_u64(out->pixelclock_hz * 1000, (u64)h_total * v_total);
	for (i = 0; i < sizeof(standard_rates) / sizeof(standard_rates[0]); i++) {
		u64 std = (u64)standard_rates[i] * 1000;
		u64 gap = std > hz_milli ? std - hz_milli : hz_milli - std;

		if (gap < best_gap) {
			best_gap = gap;
			out->refresh_hz = standard_rates[i];
		}
	}
	return true;
}

#endif

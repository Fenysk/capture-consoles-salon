/*
 * Tests de gl310_model.h, sans boîtier : mêmes valeurs que studio/test_model.py.
 * Lancer : make -C driver/gl310 test
 */
#include <stdio.h>
#include <string.h>

#include "../gl310_model.h"

static int failures;

#define CHECK_EQ(what, got, want) do { \
	unsigned long long g_ = (got), w_ = (want); \
	if (g_ != w_) { \
		printf("ÉCHEC %s : obtenu %#llx, attendu %#llx\n", what, g_, w_); \
		failures++; \
	} else { \
		printf("ok    %s\n", what); \
	} \
} while (0)

static struct gl310_settings defaults(void)
{
	struct gl310_settings s = GL310_DEFAULT_SETTINGS;
	return s;
}

static void hex7(const char *hex, u8 out[7])
{
	for (int i = 0; i < 7; i++)
		sscanf(hex + 2 * i, "%2hhx", &out[i]);
}

int main(void)
{
	struct gl310_settings s = defaults();
	struct gl310_signal sig;
	u8 raw[7];

	/* Quand le débit vidéo est 8000 kbit/s, RATE_CONTROL garde les bits hauts de la trace. */
	s.video_kbps = 8000;
	CHECK_EQ("rate_control 8000", gl310_rate_control(&s), 0x00781F40);
	s.video_kbps = 30000;
	CHECK_EQ("rate_control 30000 = trace", gl310_rate_control(&s), GL310_TRACE_RATE_CONTROL);

	/* Quand l'intervalle d'images clés est 60, GOP_LOOP_FILTER ne change que les bits 0-15. */
	s = defaults();
	s.keyframe_interval = 60;
	CHECK_EQ("gop 60", gl310_gop_loop_filter(&s), 0xF199003C);
	s.keyframe_interval = 30;
	CHECK_EQ("gop 30 = trace", gl310_gop_loop_filter(&s), GL310_TRACE_GOP_LOOP_FILTER);

	/* Quand l'audio est à 192 kbit/s avec AUDIO OUT, les bits 0-9 et 16 changent. */
	s = defaults();
	s.audio_kbps = 192;
	s.audio_out = true;
	CHECK_EQ("audio 192 + AUDIO OUT", gl310_audio_control_param(&s), 0x211710C0);
	s.audio_kbps = 128;
	s.audio_out = false;
	CHECK_EQ("audio 128 sans AUDIO OUT = trace", gl310_audio_control_param(&s), GL310_TRACE_AUDIO_CONTROL_PARAM);

	/* Si un réglage sort des bornes, alors il est refusé. */
	s = defaults();
	CHECK_EQ("réglages par défaut valides", gl310_settings_valid(&s), 1);
	s.video_kbps = 50000;
	CHECK_EQ("débit 50000 refusé", gl310_settings_valid(&s), 0);
	s = defaults();
	s.audio_kbps = 64;
	CHECK_EQ("audio 64 refusé", gl310_settings_valid(&s), 0);

	/* Quand le micro décrit un signal 1080p60, on retrouve 1920x1080 à 60 Hz. */
	memset(&sig, 0, sizeof(sig));
	hex7("98788065443817", raw);
	CHECK_EQ("timing 1080p60 reconnu", gl310_parse_timing(raw, &sig), 1);
	CHECK_EQ("largeur", sig.width, 1920);
	CHECK_EQ("hauteur", sig.height, 1080);
	CHECK_EQ("fréquence", sig.refresh_hz, 60);
	CHECK_EQ("total horizontal", sig.h_total, 2200);
	CHECK_EQ("total vertical", sig.v_total, 1125);

	/* Si le compteur d'horloge est nul, alors aucun signal n'est décrit. */
	hex7("98788065443800", raw);
	CHECK_EQ("compteur nul refusé", gl310_parse_timing(raw, &sig), 0);

	printf(failures ? "%d test(s) en échec\n" : "Tous les tests passent.\n", failures);
	return failures != 0;
}

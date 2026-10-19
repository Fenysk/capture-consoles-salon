// SPDX-License-Identifier: GPL-2.0
/*
 * gl310_usb.c - dialogue USB avec le GL310 : démarrage, encodeur, boucle de recopie du flux.
 *
 * Endpoints (une seule interface, classe vendeur) :
 *   0x04 OUT commandes, 0x83 IN réponses, 0x02 OUT données (firmware), 0x81 IN données (flux, relectures).
 * Formats des commandes (adresses mémoire en mots de 4 octets) :
 *   01 01 01 00 reg val        écriture d'un registre du pont      01 00 n 00 reg     lecture de n registres
 *   02 01 04 00 a a val        écriture d'un mot en mémoire ARM    02 00 04 00 a a    lecture d'un mot
 *   09 0W 08 00 0 addr nmots   bloc mémoire (W=1 écriture sur EP 0x02, W=0 lecture sur EP 0x81)
 *   07 0R 00 00 00 00 00 00    ARM : R=0 arrêt, R=1 démarrage
 *   0B 01 n 00 15 0 0 0 reg …  écriture micro (I2C 0x15)            0C 01 n 00 15 0 0 0 reg   lecture micro
 * Boîte aux lettres (comme le C985) : vers l'ARM 0x6F8..0x6D0 paramètres, 0x6CC état, 0x6FC opcode ;
 * depuis l'ARM 0x6C8 état (bit 0 = message prêt), 0x6B0..0x6C4 contenu.
 */
#include <linux/bcd.h>
#include <linux/delay.h>
#include <linux/firmware.h>
#include <linux/kthread.h>
#include <linux/module.h>
#include <linux/slab.h>
#include <linux/swab.h>
#include <linux/time.h>
#include <linux/vmalloc.h>
#include <asm/unaligned.h>

#include "gl310.h"
#include "gl310_tables.h"

#define EP_CMD_OUT        0x04
#define EP_CMD_IN         0x83
#define EP_DATA_OUT       0x02
#define EP_DATA_IN        0x81
#define CMD_TIMEOUT_MS    1000
#define DATA_TIMEOUT_MS   5000
#define ANSWER_BYTES      512

#define BLOCK_WORDS       0x2000
#define BLOCK_BYTES       (BLOCK_WORDS * 4)
#define FIFO_BYTES        (4 << 20)   /* ~1 s à 30 Mbit/s */
#define MAX_MESSAGE_WORDS (1 << 20)

#define REG_TO_ARM_PARAM0   0x06F8
#define REG_TO_ARM_STATUS   0x06CC
#define REG_TO_ARM_MSG      0x06FC
#define REG_FROM_ARM_STATUS 0x06C8
#define REG_FROM_ARM_MSG    0x06B0
#define REG_ENCODER         0x0800
#define ENCODER_RUNNING     (1u << 16)

#define MSG_FRAME           0x40
#define TAG_TS              0x83
#define OP_STOP_ENCODER 0x02
#define OP_SYSTEM_CLOSE     0xF3
#define OP_COMPLETE_ARM     0x30

#define MCU_ADDR            0x15
#define MCU_SIGNAL          0x1C
#define MCU_SIGNAL_HDMI     0x04
#define MCU_TIMING          0x1D
#define MCU_APPLY_IMAGE     0x42
#define MCU_BRIGHTNESS      0x43
#define MCU_CONTRAST        0x44

/* Mémoire de l'ARM (en mots) : images des firmwares et configuration de démarrage QPSOS v2. */
#define AUDIO_BASE          0x40000
#define VIDEO_BASE          0x00000
#define CFG_FIXED_MODE      (0x2F1094 / 4)
#define CFG_INT_MODE        (0x2F1090 / 4)
#define CFG_BASE            (0x2F2000 / 4)
#define QSOS_SIGNATURE      0x534F5351   /* "QSOS" */
#define FW_AUDIO            "avermedia/qpaudfwusb.bin"
#define FW_VIDEO            "avermedia/qpvidfwusb.bin"

static bool verify_firmware = true;
module_param(verify_firmware, bool, 0644);
MODULE_PARM_DESC(verify_firmware, "relire et comparer chaque bloc de firmware chargé (défaut : oui)");

/* ---- commandes de base (io_lock tenu) ------------------------------------------------------- */

static bool is_mcu_profile_write(const u8 *b)
{
	/* 0x47/0x49 : enregistrement permanent du profil du micro. Jamais. */
	return b[0] == 0x0B && (b[8] == 0x47 || b[8] == 0x49);
}

/* Envoie les len premiers octets de cmd_buf ; si expect, lit la réponse dans answer_buf. */
static int gl310_send(struct gl310_dev *dev, int len, bool expect, int *answer_len)
{
	int actual, ret;

	lockdep_assert_held(&dev->io_lock);
	if (dev->disconnected)
		return -ENODEV;
	if (is_mcu_profile_write(dev->cmd_buf))
		return -EPERM;
	ret = usb_bulk_msg(dev->udev, usb_sndbulkpipe(dev->udev, EP_CMD_OUT), dev->cmd_buf, len,
			   &actual, CMD_TIMEOUT_MS);
	if (ret || !expect)
		return ret;
	ret = usb_bulk_msg(dev->udev, usb_rcvbulkpipe(dev->udev, EP_CMD_IN), dev->answer_buf, ANSWER_BYTES,
			   &actual, CMD_TIMEOUT_MS);
	if (!ret && answer_len)
		*answer_len = actual;
	return ret;
}

static void put_header(u8 *b, u8 op, u8 dir, u8 n)
{
	b[0] = op;
	b[1] = dir;
	b[2] = n;
	b[3] = 0;
}

static int reg_write(struct gl310_dev *dev, u32 reg, u32 val)
{
	put_header(dev->cmd_buf, 0x01, 1, 1);
	put_unaligned_le32(reg, dev->cmd_buf + 4);
	put_unaligned_le32(val, dev->cmd_buf + 8);
	return gl310_send(dev, 12, false, NULL);
}

static int reg_read(struct gl310_dev *dev, u32 reg, u32 *out, int count)
{
	int len = 0, i, ret;

	put_header(dev->cmd_buf, 0x01, 0, count);
	put_unaligned_le32(reg, dev->cmd_buf + 4);
	ret = gl310_send(dev, 8, true, &len);
	if (ret)
		return ret;
	if (len < 4 * count)
		return -EIO;
	for (i = 0; i < count; i++)
		out[i] = get_unaligned_le32(dev->answer_buf + 4 * i);
	return 0;
}

static int arm_run(struct gl310_dev *dev, bool run)
{
	memset(dev->cmd_buf, 0, 8);
	dev->cmd_buf[0] = 0x07;
	dev->cmd_buf[1] = run;
	return gl310_send(dev, 8, false, NULL);
}

static int word_write(struct gl310_dev *dev, u32 addr, u32 val)
{
	put_header(dev->cmd_buf, 0x02, 1, 4);
	put_unaligned_le32(addr, dev->cmd_buf + 4);
	put_unaligned_le32(addr, dev->cmd_buf + 8);
	put_unaligned_le32(val, dev->cmd_buf + 12);
	return gl310_send(dev, 16, false, NULL);
}

static int word_read(struct gl310_dev *dev, u32 addr, u32 *val)
{
	int len = 0, ret;

	put_header(dev->cmd_buf, 0x02, 0, 4);
	put_unaligned_le32(addr, dev->cmd_buf + 4);
	put_unaligned_le32(addr, dev->cmd_buf + 8);
	ret = gl310_send(dev, 12, true, &len);
	if (!ret && len < 4)
		ret = -EIO;
	if (!ret)
		*val = get_unaligned_le32(dev->answer_buf);
	return ret;
}

static int mem_header(struct gl310_dev *dev, bool write, u32 addr, u32 words)
{
	put_header(dev->cmd_buf, 0x09, write, 8);
	put_unaligned_le32(0, dev->cmd_buf + 4);
	put_unaligned_le32(addr, dev->cmd_buf + 8);
	put_unaligned_le32(words, dev->cmd_buf + 12);
	return gl310_send(dev, 16, true, NULL);
}

/* Lit exactement bytes octets du flux de données (EP 0x81) dans data_buf. */
static int read_data(struct gl310_dev *dev, int bytes)
{
	int got = 0, actual, ret;

	while (got < bytes) {
		ret = usb_bulk_msg(dev->udev, usb_rcvbulkpipe(dev->udev, EP_DATA_IN), dev->data_buf + got,
				   bytes - got, &actual, DATA_TIMEOUT_MS);
		if (ret)
			return ret;
		if (!actual)
			return -EIO;
		got += actual;
	}
	return 0;
}

/* Lit un bloc de mémoire ARM (au plus BLOCK_WORDS mots) dans data_buf. */
static int mem_read_block(struct gl310_dev *dev, u32 addr, u32 words)
{
	int ret = mem_header(dev, false, addr, words);

	return ret ? ret : read_data(dev, words * 4);
}

/* Écrit src (ou des zéros si src est NULL) en mémoire ARM, par blocs de 32 Ko, avec relecture éventuelle. */
static int mem_write(struct gl310_dev *dev, u32 addr, const u8 *src, size_t bytes, bool verify)
{
	size_t off;
	int actual, ret;

	for (off = 0; off < bytes; off += BLOCK_BYTES) {
		size_t n = min_t(size_t, BLOCK_BYTES, bytes - off);
		u32 a = addr + off / 4;

		ret = mem_header(dev, true, a, n / 4);
		if (ret)
			return ret;
		if (src)
			memcpy(dev->data_buf, src + off, n);
		else
			memset(dev->data_buf, 0, n);
		ret = usb_bulk_msg(dev->udev, usb_sndbulkpipe(dev->udev, EP_DATA_OUT), dev->data_buf, n,
				   &actual, DATA_TIMEOUT_MS);
		if (ret)
			return ret;
		if (!verify || !src)
			continue;
		ret = mem_read_block(dev, a, n / 4);
		if (ret)
			return ret;
		if (memcmp(dev->data_buf, src + off, n)) {
			dev_err(&dev->intf->dev, "relecture du firmware différente au mot %#x\n", a);
			return -EIO;
		}
	}
	return 0;
}

/* Vide les données restées en attente (capture interrompue en plein transfert). */
static void flush_endpoints(struct gl310_dev *dev)
{
	static const u8 eps[] = { EP_DATA_IN, EP_CMD_IN };
	int actual, i, rounds;

	for (i = 0; i < ARRAY_SIZE(eps); i++)
		for (rounds = 0; rounds < 64; rounds++)
			if (usb_bulk_msg(dev->udev, usb_rcvbulkpipe(dev->udev, eps[i]), dev->data_buf, BLOCK_BYTES,
					 &actual, 50))
				break;
}

/* ---- microcontrôleur ------------------------------------------------------------------------ */

static int mcu_read_locked(struct gl310_dev *dev, u8 reg, u8 *out, u8 n)
{
	int len = 0, ret;

	put_header(dev->cmd_buf, 0x0C, 1, n);
	memset(dev->cmd_buf + 4, 0, 4);
	dev->cmd_buf[4] = MCU_ADDR;
	dev->cmd_buf[8] = reg;
	ret = gl310_send(dev, 9, true, &len);
	if (!ret && len < n)
		ret = -EIO;
	if (!ret)
		memcpy(out, dev->answer_buf, n);
	return ret;
}

static int mcu_write(struct gl310_dev *dev, u8 reg, u8 val)
{
	put_header(dev->cmd_buf, 0x0B, 1, 2);
	memset(dev->cmd_buf + 4, 0, 4);
	dev->cmd_buf[4] = MCU_ADDR;
	dev->cmd_buf[8] = reg;
	dev->cmd_buf[9] = val;
	return gl310_send(dev, 10, true, NULL);
}

int gl310_mcu_read(struct gl310_dev *dev, u8 reg, u8 *out, u8 n)
{
	int ret;

	mutex_lock(&dev->io_lock);
	ret = mcu_read_locked(dev, reg, out, n);
	mutex_unlock(&dev->io_lock);
	return ret;
}

int gl310_read_signal(struct gl310_dev *dev, struct gl310_signal *sig)
{
	u8 status, timing[7];
	int ret;

	mutex_lock(&dev->io_lock);
	ret = dev->state == GL310_BOOTING ? -EAGAIN : mcu_read_locked(dev, MCU_SIGNAL, &status, 1);
	if (!ret && !(status & MCU_SIGNAL_HDMI))
		ret = -ENOLINK;
	if (!ret)
		ret = mcu_read_locked(dev, MCU_TIMING, timing, sizeof(timing));
	mutex_unlock(&dev->io_lock);
	if (!ret && !gl310_parse_timing(timing, sig))
		ret = -ENOLINK;
	return ret;
}

/* Luminosité et contraste à chaud (comme « Set brightness/contrast to HW » du pilote Windows). */
int gl310_apply_image(struct gl310_dev *dev)
{
	int ret;

	mutex_lock(&dev->io_lock);
	ret = dev->state != GL310_STREAMING ? 0 : mcu_write(dev, MCU_BRIGHTNESS, dev->settings.brightness);
	if (!ret && dev->state == GL310_STREAMING)
		ret = mcu_write(dev, MCU_CONTRAST, dev->settings.contrast);
	if (!ret && dev->state == GL310_STREAMING)
		ret = mcu_write(dev, MCU_APPLY_IMAGE, 1);
	mutex_unlock(&dev->io_lock);
	return ret;
}

/* ---- boîte aux lettres ---------------------------------------------------------------------- */

/* Attend que le firmware ait consommé le message précédent (0x6CC bit 0 à 0). */
static int wait_to_arm_free(struct gl310_dev *dev, int tries, unsigned int step_us)
{
	u32 st = 0;
	int ret, i;

	for (i = 0; i < tries; i++) {
		ret = reg_read(dev, REG_TO_ARM_STATUS, &st, 1);
		if (ret)
			return ret;
		if (!(st & 1))
			return 0;
		usleep_range(step_us, step_us + step_us / 2);
	}
	dev_warn_ratelimited(&dev->intf->dev, "message précédent non consommé (0x6CC = %#x)\n", st);
	return 0;
}

static int mbox_send(struct gl310_dev *dev, u16 opcode, u16 task, u32 param0, const u32 *slots, int nslots)
{
	int ret, i;

	ret = wait_to_arm_free(dev, 200, 1000);
	if (!ret)
		ret = reg_write(dev, REG_TO_ARM_PARAM0, param0);
	for (i = 0; i < nslots && !ret; i++)
		ret = reg_write(dev, REG_TO_ARM_PARAM0 - 4 - 4 * i, slots[i]);
	if (!ret)
		ret = reg_write(dev, REG_TO_ARM_STATUS, ((u32)task << 16) | 1);
	if (!ret)
		ret = reg_write(dev, REG_TO_ARM_MSG, ((u32)task << 16) | opcode);
	return ret;
}

/* Libère 0x6C8, envoie l'accusé si demandé, et rend le tampon d'une image (CompleteArm 0x30). */
static int release_message(struct gl310_dev *dev, u32 st, u32 msg, u32 p0, u32 size, u32 pts)
{
	u32 kind = msg & 0xFFFF, task = msg >> 16;
	int ret;

	ret = reg_write(dev, REG_FROM_ARM_STATUS, st & ~1u);
	if (!ret && (st & 0x100))
		ret = mbox_send(dev, kind < 0x80 ? 0x31 : 0xA2, task, 0, NULL, 0);
	if (!ret && (kind & 0xFF) == MSG_FRAME) {
		u32 slots[5] = { size, pts & 0x7FFFFFFF, pts >> 31, 0, (p0 >> 24) & 0xFF };

		ret = mbox_send(dev, OP_COMPLETE_ARM, task, p0 & 0xFFFF, slots, ARRAY_SIZE(slots));
	}
	return ret;
}

/* Rend sans les lire les messages en attente ; s'arrête dès que plus rien n'attend. */
static int drain(struct gl310_dev *dev)
{
	u32 st, m[6];
	int ret, i;

	for (i = 0; i < 2000; i++) {
		ret = reg_read(dev, REG_FROM_ARM_STATUS, &st, 1);
		if (ret || !(st & 1))
			return ret;
		ret = reg_read(dev, REG_FROM_ARM_MSG, m, 6);
		if (!ret)
			ret = release_message(dev, st, m[0], m[1], m[4], m[5]);
		if (ret)
			return ret;
	}
	return 0;
}

/* ---- démarrage du boîtier ------------------------------------------------------------------- */

static int run_table(struct gl310_dev *dev, const struct gl310_cmd *table, size_t count, const char *what)
{
	size_t i;
	int ret;

	for (i = 0; i < count; i++) {
		memcpy(dev->cmd_buf, table[i].bytes, table[i].len);
		ret = gl310_send(dev, table[i].len, table[i].flags & GL310_F_EXPECT, NULL);
		if (ret) {
			dev_err(&dev->intf->dev, "%s : commande %zu refusée (%d)\n", what, i, ret);
			return ret;
		}
	}
	return 0;
}

struct gl310_reg_op {
	bool write;
	u32 reg;
	u32 val;
};

/* Fenêtres mémoire du pont USB et réglages de départ (identiques au début de la trace). */
static const struct gl310_reg_op boot_prelude[] = {
	{ true, 0x06CC, 0 },
	{ true, 0x081C, 0x00004000 }, { true, 0x0820, 0x00103FFF }, { true, 0x0824, 0x00000000 },
	{ true, 0x0828, 0x00104000 }, { true, 0x082C, 0x00203FFF }, { true, 0x0830, 0x00100000 },
	{ true, 0x0834, 0x00204000 }, { true, 0x0838, 0x00303FFF }, { true, 0x083C, 0x00200000 },
	{ true, 0x0840, 0x70003124 }, { true, 0x0840, 0x90003124 },
	{ false, 0x0050 }, { true, 0x0050, 0x0404 }, { false, 0x0050 }, { true, 0x0050, 0x0404 },
	{ true, 0x0610, 0 }, { true, 0x0614, 0 },
	{ false, 0x0000 }, { true, 0x0000, 0x07FF0000 },
};

static const struct gl310_reg_op boot_epilogue[] = {
	{ false, 0x0610 }, { true, 0x0610, 0 }, { false, 0x0610 }, { true, 0x0610, 0 },
	{ false, 0x0610 }, { false, 0x0618 },
};

static int run_reg_ops(struct gl310_dev *dev, const struct gl310_reg_op *ops, size_t count)
{
	size_t i;
	u32 val;
	int ret = 0;

	for (i = 0; i < count && !ret; i++)
		ret = ops[i].write ? reg_write(dev, ops[i].reg, ops[i].val) : reg_read(dev, ops[i].reg, &val, 1);
	return ret;
}

static int firmware_present(struct gl310_dev *dev, bool *present)
{
	u32 sig;
	int ret = word_read(dev, 0x40, &sig);

	*present = !ret && sig == QSOS_SIGNATURE;
	return ret;
}

/* Lignes 225 à 370 de la trace, avec les firmwares du GL310 (même ordre que tools/gl310_boot.py). */
static int upload_firmware(struct gl310_dev *dev, const struct firmware *audio, const struct firmware *video)
{
	static const u32 audio_bss_words[] = { 0x2000, 0x2000, 0x1E3B };
	u32 bss = AUDIO_BASE + audio->size / 4, val;
	int ret, i;

	if (audio->size % 4 || video->size % 4)
		return -EINVAL;
	ret = arm_run(dev, false);
	if (!ret)
		ret = run_reg_ops(dev, boot_prelude, ARRAY_SIZE(boot_prelude));
	for (i = 0; i < ARRAY_SIZE(audio_bss_words) && !ret; i++) {
		ret = mem_write(dev, bss, NULL, audio_bss_words[i] * 4, false);
		bss += audio_bss_words[i];
	}
	if (!ret)
		ret = mem_write(dev, AUDIO_BASE, audio->data, audio->size, verify_firmware);
	if (!ret)
		ret = word_write(dev, CFG_FIXED_MODE, 1);
	if (!ret)
		ret = word_write(dev, CFG_INT_MODE, 0);
	if (!ret)
		ret = word_write(dev, CFG_BASE + 1, 0);
	if (!ret)
		ret = reg_write(dev, REG_TO_ARM_STATUS, 0);
	if (!ret)
		ret = mem_write(dev, VIDEO_BASE, video->data, video->size, verify_firmware);
	if (!ret)
		ret = arm_run(dev, true);
	if (!ret)
		ret = reg_write(dev, REG_TO_ARM_STATUS, 0);
	if (!ret)
		ret = word_read(dev, 0x40, &val);
	if (!ret)
		ret = word_read(dev, 0x41, &val);
	if (!ret)
		dev_info(&dev->intf->dev, "firmware QPSOS version %#010x\n", val);
	if (!ret)
		ret = word_read(dev, CFG_BASE + 4, &val);
	if (!ret)
		ret = run_reg_ops(dev, boot_epilogue, ARRAY_SIZE(boot_epilogue));
	return ret;
}

static int encoder_stop(struct gl310_dev *dev);

static int gl310_boot(struct gl310_dev *dev)
{
	const struct firmware *audio = NULL, *video = NULL;
	bool present;
	u32 enc;
	int ret;

	flush_endpoints(dev);
	ret = firmware_present(dev, &present);
	if (ret)
		return ret;
	if (present) {
		/* Rechargement du module sans débrancher : le firmware tourne déjà, on ne le recharge pas
		 * (un rechargement à chaud échoue), on arrête seulement l'encodeur s'il tourne. */
		dev_info(&dev->intf->dev, "firmware déjà en RAM, pas de rechargement\n");
		ret = reg_read(dev, REG_ENCODER, &enc, 1);
		if (!ret && (enc & ENCODER_RUNNING))
			ret = encoder_stop(dev);
		return ret;
	}

	ret = request_firmware(&audio, FW_AUDIO, &dev->intf->dev);
	if (!ret)
		ret = request_firmware(&video, FW_VIDEO, &dev->intf->dev);
	if (ret) {
		dev_err(&dev->intf->dev, "firmware introuvable (%d) : copier %s et %s dans /lib/firmware\n",
			ret, FW_AUDIO, FW_VIDEO);
		goto out;
	}
	/* Lignes 1 à 224 : réglages du pont et 206 écritures I2C, identiques au pilote Windows. */
	ret = run_table(dev, gl310_official_init, ARRAY_SIZE(gl310_official_init), "initialisation officielle");
	if (!ret)
		ret = upload_firmware(dev, audio, video);
	if (!ret)
		ret = firmware_present(dev, &present);
	if (!ret && !present)
		ret = -EIO;
out:
	release_firmware(video);
	release_firmware(audio);
	return ret;
}

static void gl310_boot_work(struct work_struct *work)
{
	struct gl310_dev *dev = container_of(work, struct gl310_dev, boot_work);
	int ret;

	mutex_lock(&dev->io_lock);
	ret = gl310_boot(dev);
	dev->state = ret ? GL310_FAILED : GL310_IDLE;
	dev->boot_error = ret;
	mutex_unlock(&dev->io_lock);
	if (ret)
		dev_err(&dev->intf->dev, "démarrage impossible (%d) : débrancher le boîtier 10 s puis le rebrancher\n",
			ret);
	else
		dev_info(&dev->intf->dev, "prêt\n");
	complete_all(&dev->booted);
}

/* ---- encodeur ------------------------------------------------------------------------------- */

static void fill_rtc(u8 *b)
{
	struct tm tm;

	time64_to_tm(ktime_get_real_seconds(), -sys_tz.tz_minuteswest * 60, &tm);
	b[0] = bin2bcd((tm.tm_year + 1900) % 100);
	b[1] = bin2bcd(tm.tm_mon + 1);
	b[2] = bin2bcd(tm.tm_mday);
	b[3] = bin2bcd(tm.tm_hour);
	b[4] = bin2bcd(tm.tm_min);
	b[5] = bin2bcd(tm.tm_sec);
}

/* Lignes 371 à 757 : 0x0A, SystemOpen, SystemLink, réglages 0x10, bloc de configuration, StartEncoder.
 * Les valeurs de la trace sont remplacées par les réglages courants là où gen_gl310_tables.py les a marquées. */
static int encoder_start(struct gl310_dev *dev)
{
	const struct gl310_settings *s = &dev->settings;
	size_t i;
	int ret;

	if (!gl310_settings_valid(s))
		return -EINVAL;
	for (i = 0; i < ARRAY_SIZE(gl310_encoder_start); i++) {
		const struct gl310_cmd *c = &gl310_encoder_start[i];
		u8 *b = dev->cmd_buf;

		if (c->flags & GL310_F_WAIT_MBOX) {
			ret = wait_to_arm_free(dev, 50, 10000);
			if (ret)
				return ret;
			continue;
		}
		memcpy(b, c->bytes, c->len);
		if (c->flags & GL310_F_RATE)
			put_unaligned_le32(gl310_rate_control(s), b + 8);
		if (c->flags & GL310_F_GOP)
			put_unaligned_le32(gl310_gop_loop_filter(s), b + 8);
		if (c->flags & GL310_F_AUDIO)
			put_unaligned_le32(gl310_audio_control_param(s), b + 8);
		if (c->flags & GL310_F_BRIGHTNESS)
			b[9] = s->brightness;
		if (c->flags & GL310_F_CONTRAST)
			b[9] = s->contrast;
		if (c->flags & GL310_F_RTC)
			fill_rtc(b + 9);
		ret = gl310_send(dev, c->len, c->flags & GL310_F_EXPECT, NULL);
		if (ret) {
			dev_err(&dev->intf->dev, "démarrage de l'encodeur : commande %zu refusée (%d)\n", i, ret);
			return ret;
		}
	}
	dev_info(&dev->intf->dev, "encodeur démarré : %u kbit/s, GOP %u, AAC %u kbit/s, AUDIO OUT %s\n",
		 s->video_kbps, s->keyframe_interval, s->audio_kbps, s->audio_out ? "oui" : "non");
	return 0;
}

/* 0x02 StopEncoder puis 0xF3 SystemClose : l'encodeur peut ensuite être relancé sans rebrancher. */
static int encoder_stop(struct gl310_dev *dev)
{
	int ret = drain(dev);

	if (!ret)
		ret = mbox_send(dev, OP_STOP_ENCODER, 0, 0, NULL, 0);
	msleep(300);
	if (!ret)
		ret = drain(dev);
	if (!ret)
		ret = mbox_send(dev, OP_SYSTEM_CLOSE, 0, 0, NULL, 0);
	msleep(300);
	if (!ret)
		ret = drain(dev);
	return ret;
}

/* ---- boucle de recopie du flux -------------------------------------------------------------- */

/* Recopie un morceau de MPEG-TS dans la file ; les octets de chaque mot arrivent inversés. */
static int copy_ts(struct gl310_dev *dev, u32 addr, u32 words)
{
	u32 done, i;
	int ret;

	for (done = 0; done < words; done += BLOCK_WORDS) {
		u32 n = min_t(u32, BLOCK_WORDS, words - done);
		u32 *w = (u32 *)dev->data_buf;

		ret = mem_read_block(dev, addr + done, n);
		if (ret)
			return ret;
		for (i = 0; i < n; i++)
			swab32s(&w[i]);
		if (kfifo_avail(&dev->fifo) < n * 4) {
			dev->bytes_dropped += n * 4;
			continue;
		}
		kfifo_in(&dev->fifo, dev->data_buf, n * 4);
		dev->bytes_in += n * 4;
	}
	return 0;
}

/* Traite un message du firmware s'il y en a un. Renvoie 1 si un message a été traité, 0 sinon. */
static int pump_one(struct gl310_dev *dev)
{
	u32 st, m[6];
	int ret;

	ret = reg_read(dev, REG_FROM_ARM_STATUS, &st, 1);
	if (ret || !(st & 1))
		return ret;
	ret = reg_read(dev, REG_FROM_ARM_MSG, m, 6);
	if (ret)
		return ret;
	dev->messages++;
	/* Lire les données AVANT de rendre le tampon, sinon le firmware peut le réécrire. */
	if ((m[0] & 0xFF) == MSG_FRAME && (m[1] & 0xFFFF) == TAG_TS && m[4] && m[4] <= MAX_MESSAGE_WORDS)
		ret = copy_ts(dev, m[2], m[4]);
	if (!ret)
		ret = release_message(dev, st, m[0], m[1], m[4], m[5]);
	if (!ret)
		wake_up_interruptible(&dev->wait);
	return ret ? ret : 1;
}

static int gl310_pump(void *arg)
{
	struct gl310_dev *dev = arg;
	int ret = 0;

	while (!kthread_should_stop()) {
		mutex_lock(&dev->io_lock);
		if (atomic_xchg(&dev->restart_pending, 0)) {
			ret = encoder_stop(dev);
			if (!ret)
				ret = encoder_start(dev);
		} else {
			ret = pump_one(dev);
		}
		mutex_unlock(&dev->io_lock);
		if (ret < 0)
			break;
		if (ret == 0)
			usleep_range(1000, 2000);
	}
	if (ret < 0) {
		dev_err(&dev->intf->dev, "flux interrompu (%d)\n", ret);
		WRITE_ONCE(dev->stream_error, ret);
		wake_up_interruptible(&dev->wait);
		/* Attendre kthread_stop() : le fil ne doit pas disparaître avant. */
		while (!kthread_should_stop())
			msleep_interruptible(100);
	}
	return 0;
}

/* Démarre l'encodeur et la boucle (dev->lock tenu par l'appelant). */
int gl310_stream_start(struct gl310_dev *dev)
{
	struct task_struct *pump;
	int ret;

	lockdep_assert_held(&dev->lock);
	mutex_lock(&dev->read_lock);
	kfifo_reset(&dev->fifo);
	mutex_unlock(&dev->read_lock);
	WRITE_ONCE(dev->stream_error, 0);
	atomic_set(&dev->restart_pending, 0);

	mutex_lock(&dev->io_lock);
	if (dev->disconnected)
		ret = -ENODEV;
	else if (dev->state != GL310_IDLE)
		ret = -EIO;
	else
		ret = drain(dev);
	if (!ret)
		ret = encoder_start(dev);
	if (!ret)
		dev->state = GL310_STREAMING;
	mutex_unlock(&dev->io_lock);
	if (ret)
		return ret;

	pump = kthread_run(gl310_pump, dev, "gl310-pump");
	if (IS_ERR(pump)) {
		gl310_stream_stop(dev);
		return PTR_ERR(pump);
	}
	dev->pump = pump;
	return 0;
}

/* Arrête la boucle puis l'encodeur (dev->lock tenu par l'appelant). */
void gl310_stream_stop(struct gl310_dev *dev)
{
	lockdep_assert_held(&dev->lock);
	if (dev->pump) {
		kthread_stop(dev->pump);
		dev->pump = NULL;
	}
	mutex_lock(&dev->io_lock);
	if (dev->state == GL310_STREAMING) {
		if (!dev->disconnected && encoder_stop(dev))
			dev_warn(&dev->intf->dev, "arrêt de l'encodeur incomplet\n");
		dev->state = GL310_IDLE;
		dev_info(&dev->intf->dev, "flux arrêté : %llu octets, %llu perdus, %llu messages\n",
			 dev->bytes_in, dev->bytes_dropped, dev->messages);
	}
	mutex_unlock(&dev->io_lock);
}

/* ---- branchement USB ------------------------------------------------------------------------ */

static bool has_expected_endpoints(struct usb_interface *intf)
{
	static const u8 wanted[] = { EP_CMD_OUT, EP_CMD_IN, EP_DATA_OUT, EP_DATA_IN };
	struct usb_host_interface *alt = intf->cur_altsetting;
	int i, j, found = 0;

	for (i = 0; i < ARRAY_SIZE(wanted); i++)
		for (j = 0; j < alt->desc.bNumEndpoints; j++)
			if (alt->endpoint[j].desc.bEndpointAddress == wanted[i] &&
			    usb_endpoint_xfer_bulk(&alt->endpoint[j].desc)) {
				found++;
				break;
			}
	return found == ARRAY_SIZE(wanted);
}

void gl310_free(struct gl310_dev *dev)
{
	vfree(dev->fifo.kfifo.data);
	kfree(dev->data_buf);
	kfree(dev->answer_buf);
	kfree(dev->cmd_buf);
	usb_put_intf(dev->intf);
	usb_put_dev(dev->udev);
	kfree(dev);
}

static int gl310_probe(struct usb_interface *intf, const struct usb_device_id *id)
{
	static const struct gl310_settings defaults = GL310_DEFAULT_SETTINGS;
	struct gl310_dev *dev;
	void *fifo_buf;
	int ret;

	if (!has_expected_endpoints(intf))
		return -ENODEV;
	dev = kzalloc(sizeof(*dev), GFP_KERNEL);
	if (!dev)
		return -ENOMEM;
	dev->udev = usb_get_dev(interface_to_usbdev(intf));
	dev->intf = usb_get_intf(intf);
	dev->settings = defaults;
	dev->state = GL310_BOOTING;
	mutex_init(&dev->io_lock);
	mutex_init(&dev->lock);
	mutex_init(&dev->read_lock);
	init_completion(&dev->booted);
	init_waitqueue_head(&dev->wait);
	INIT_WORK(&dev->boot_work, gl310_boot_work);

	dev->cmd_buf = kmalloc(64, GFP_KERNEL);
	dev->answer_buf = kmalloc(ANSWER_BYTES, GFP_KERNEL);
	dev->data_buf = kmalloc(BLOCK_BYTES, GFP_KERNEL);
	fifo_buf = vmalloc(FIFO_BYTES);
	if (!dev->cmd_buf || !dev->answer_buf || !dev->data_buf || !fifo_buf) {
		vfree(fifo_buf);
		gl310_free(dev);
		return -ENOMEM;
	}
	ret = kfifo_init(&dev->fifo, fifo_buf, FIFO_BYTES);
	if (ret) {
		vfree(fifo_buf);
		gl310_free(dev);
		return ret;
	}

	usb_set_intfdata(intf, dev);
	ret = gl310_v4l2_register(dev);   /* en cas d'échec, libère dev */
	if (ret) {
		usb_set_intfdata(intf, NULL);
		return ret;
	}
	schedule_work(&dev->boot_work);
	return 0;
}

static void gl310_disconnect(struct usb_interface *intf)
{
	struct gl310_dev *dev = usb_get_intfdata(intf);

	/* Laisser finir un démarrage en cours (il échoue vite si le boîtier a été débranché). */
	cancel_work_sync(&dev->boot_work);
	mutex_lock(&dev->lock);
	gl310_stream_stop(dev);
	mutex_lock(&dev->io_lock);
	dev->disconnected = true;
	mutex_unlock(&dev->io_lock);
	mutex_unlock(&dev->lock);
	if (!completion_done(&dev->booted)) {
		dev->boot_error = -ENODEV;
		complete_all(&dev->booted);
	}
	wake_up_interruptible_all(&dev->wait);
	usb_set_intfdata(intf, NULL);
	gl310_v4l2_unregister(dev);
	dev_info(&intf->dev, "débranché\n");
}

static const struct usb_device_id gl310_ids[] = {
	{ USB_DEVICE(0x07CA, 0xC835) },
	{ }
};
MODULE_DEVICE_TABLE(usb, gl310_ids);

static struct usb_driver gl310_driver = {
	.name = GL310_NAME,
	.probe = gl310_probe,
	.disconnect = gl310_disconnect,
	.id_table = gl310_ids,
};
module_usb_driver(gl310_driver);

MODULE_DESCRIPTION("AVerMedia Live Gamer Portable Lite (GL310) : capture HDMI en MPEG-TS");
MODULE_AUTHOR("Fenysk");
MODULE_LICENSE("GPL");
MODULE_FIRMWARE(FW_AUDIO);
MODULE_FIRMWARE(FW_VIDEO);

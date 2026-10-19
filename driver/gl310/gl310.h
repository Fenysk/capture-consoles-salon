/* SPDX-License-Identifier: GPL-2.0 */
/*
 * gl310.h - pilote noyau de l'AVerMedia Live Gamer Portable Lite (GL310, USB 07ca:c835).
 *
 * Le boîtier encode lui-même la vidéo (H.264 1080p) et le son (AAC) en MPEG-TS : le pilote charge les
 * firmwares en RAM, démarre l'encodeur, puis recopie les morceaux de flux dans une file que lit /dev/videoN.
 * Rien n'est écrit de façon permanente dans le boîtier : débrancher le remet à zéro.
 * Protocole : voir NOTES.md et les outils Python de tools/ (référence exécutable).
 */
#ifndef GL310_H
#define GL310_H

#include <linux/completion.h>
#include <linux/kfifo.h>
#include <linux/mutex.h>
#include <linux/usb.h>
#include <linux/wait.h>
#include <linux/workqueue.h>
#include <media/v4l2-ctrls.h>
#include <media/v4l2-device.h>

#include "gl310_model.h"

#define GL310_NAME "gl310"

/* Commandes « une ligne de trace » : générées par tools/gen_gl310_tables.py (gl310_tables.h). */
#define GL310_F_EXPECT     (1 << 0)
#define GL310_F_WAIT_MBOX  (1 << 1)
#define GL310_F_RATE       (1 << 2)
#define GL310_F_GOP        (1 << 3)
#define GL310_F_AUDIO      (1 << 4)
#define GL310_F_BRIGHTNESS (1 << 5)
#define GL310_F_CONTRAST   (1 << 6)
#define GL310_F_RTC        (1 << 7)

struct gl310_cmd {
	u8 len;
	u8 flags;
	u8 bytes[16];
};

/* Un seul état du boîtier à la fois. */
enum gl310_state {
	GL310_BOOTING,     /* initialisation officielle + firmwares en cours */
	GL310_IDLE,        /* firmware prêt, encodeur arrêté */
	GL310_STREAMING,   /* encodeur en marche, la boucle recopie le flux */
	GL310_FAILED,      /* démarrage impossible : débrancher puis rebrancher */
};

struct gl310_dev {
	struct usb_device *udev;
	struct usb_interface *intf;

	/* io_lock : une seule commande USB à la fois (les réponses arrivent dans l'ordre sur EP 0x83).
	 * Protège aussi state, settings et disconnected. */
	struct mutex io_lock;
	enum gl310_state state;
	struct gl310_settings settings;
	bool disconnected;
	u8 *cmd_buf;       /* 64 octets */
	u8 *answer_buf;    /* 512 octets */
	u8 *data_buf;      /* GL310_BLOCK_BYTES, pour EP 0x02 / 0x81 */

	struct work_struct boot_work;
	struct completion booted;
	int boot_error;

	/* lock : verrou des ioctl V4L2 ; protège reader et la vie de la boucle (pump). */
	struct mutex lock;
	struct file *reader;           /* seul fichier qui reçoit le flux */
	struct task_struct *pump;
	atomic_t restart_pending;      /* réglage d'encodage changé pendant le flux */
	int stream_error;

	struct mutex read_lock;        /* un seul consommateur de la file à la fois */
	DECLARE_KFIFO_PTR(fifo, u8);
	wait_queue_head_t wait;
	u64 bytes_in, bytes_dropped, messages;

	struct v4l2_device v4l2_dev;
	struct video_device vdev;
	struct v4l2_ctrl_handler ctrls;
};

/* gl310_usb.c */
int gl310_mcu_read(struct gl310_dev *dev, u8 reg, u8 *out, u8 n);
int gl310_apply_image(struct gl310_dev *dev);
int gl310_stream_start(struct gl310_dev *dev);
void gl310_stream_stop(struct gl310_dev *dev);
int gl310_read_signal(struct gl310_dev *dev, struct gl310_signal *sig);

/* gl310_v4l2.c */
int gl310_v4l2_register(struct gl310_dev *dev);
void gl310_v4l2_unregister(struct gl310_dev *dev);
void gl310_free(struct gl310_dev *dev);

#endif

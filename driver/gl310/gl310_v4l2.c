// SPDX-License-Identifier: GPL-2.0
/*
 * gl310_v4l2.c - /dev/videoN du GL310, sur le modèle du pilote hdpvr : le flux est déjà compressé
 * (MPEG-TS : H.264 + AAC), on le lit avec read(). Le premier lecteur démarre l'encodeur, sa fermeture l'arrête.
 *
 * Contrôles : luminosité et contraste (à chaud, par le micro) ; débit vidéo, intervalle d'images clés,
 * débit AAC et sortie AUDIO OUT (pendant le flux : l'encodeur est relancé avec les nouveaux réglages).
 * État du signal : VIDIOC_ENUMINPUT (V4L2_IN_ST_NO_SIGNAL) et VIDIOC_QUERY_DV_TIMINGS.
 */
#include <linux/module.h>
#include <linux/slab.h>
#include <media/v4l2-event.h>
#include <media/v4l2-fh.h>
#include <media/v4l2-ioctl.h>

#include "gl310.h"

#define GL310_CID_AUDIO_OUT (V4L2_CID_USER_BASE + 0x1f00)
#define TS_SIZEIMAGE        (188 * 1024)

/* ---- flux ----------------------------------------------------------------------------------- */

/* Le premier lecteur démarre l'encodeur ; tant qu'il lit, un second lecteur est refusé. */
static int claim_stream(struct gl310_dev *dev, struct file *file)
{
	int ret = 0;

	if (mutex_lock_interruptible(&dev->lock))
		return -ERESTARTSYS;
	if (dev->reader && dev->reader != file)
		ret = -EBUSY;
	else if (!dev->reader)
		ret = gl310_stream_start(dev);
	if (!ret)
		dev->reader = file;
	mutex_unlock(&dev->lock);
	return ret;
}

static bool stream_has_news(struct gl310_dev *dev)
{
	return !kfifo_is_empty(&dev->fifo) || READ_ONCE(dev->stream_error) || READ_ONCE(dev->disconnected);
}

static ssize_t gl310_read(struct file *file, char __user *buf, size_t count, loff_t *ppos)
{
	struct gl310_dev *dev = video_drvdata(file);
	unsigned int copied;
	int ret;

	ret = claim_stream(dev, file);
	if (ret)
		return ret;
	if (mutex_lock_interruptible(&dev->read_lock))
		return -ERESTARTSYS;
	while (kfifo_is_empty(&dev->fifo)) {
		ret = READ_ONCE(dev->stream_error);
		if (!ret && READ_ONCE(dev->disconnected))
			ret = -ENODEV;
		if (!ret && (file->f_flags & O_NONBLOCK))
			ret = -EAGAIN;
		if (!ret)
			ret = wait_event_interruptible(dev->wait, stream_has_news(dev));
		if (ret)
			goto out;
	}
	ret = kfifo_to_user(&dev->fifo, buf, count, &copied);
	if (!ret)
		ret = copied;
out:
	mutex_unlock(&dev->read_lock);
	return ret;
}

static __poll_t gl310_poll(struct file *file, poll_table *wait)
{
	struct gl310_dev *dev = video_drvdata(file);
	__poll_t mask = v4l2_ctrl_poll(file, wait);

	if (!(poll_requested_events(wait) & (EPOLLIN | EPOLLRDNORM)))
		return mask;
	if (claim_stream(dev, file))
		return mask | EPOLLERR;
	poll_wait(file, &dev->wait, wait);
	if (!kfifo_is_empty(&dev->fifo))
		mask |= EPOLLIN | EPOLLRDNORM;
	if (READ_ONCE(dev->stream_error) || READ_ONCE(dev->disconnected))
		mask |= EPOLLERR;
	return mask;
}

/* Attend la fin du démarrage du boîtier (quelques secondes après le branchement). */
static int gl310_open(struct file *file)
{
	struct gl310_dev *dev = video_drvdata(file);

	if (file->f_flags & O_NONBLOCK) {
		if (!completion_done(&dev->booted))
			return -EAGAIN;
	} else if (wait_for_completion_interruptible(&dev->booted)) {
		return -ERESTARTSYS;
	}
	if (dev->boot_error)
		return dev->boot_error;
	return v4l2_fh_open(file);
}

static int gl310_release(struct file *file)
{
	struct gl310_dev *dev = video_drvdata(file);

	mutex_lock(&dev->lock);
	if (dev->reader == file) {
		gl310_stream_stop(dev);
		dev->reader = NULL;
	}
	mutex_unlock(&dev->lock);
	return v4l2_fh_release(file);
}

static const struct v4l2_file_operations gl310_fops = {
	.owner = THIS_MODULE,
	.open = gl310_open,
	.release = gl310_release,
	.read = gl310_read,
	.poll = gl310_poll,
	.unlocked_ioctl = video_ioctl2,
};

/* ---- ioctl ---------------------------------------------------------------------------------- */

static int vidioc_querycap(struct file *file, void *fh, struct v4l2_capability *cap)
{
	struct gl310_dev *dev = video_drvdata(file);

	strscpy(cap->driver, GL310_NAME, sizeof(cap->driver));
	strscpy(cap->card, "AVerMedia Live Gamer Portable Lite", sizeof(cap->card));
	usb_make_path(dev->udev, cap->bus_info, sizeof(cap->bus_info));
	return 0;
}

static int vidioc_enum_fmt(struct file *file, void *fh, struct v4l2_fmtdesc *f)
{
	if (f->index)
		return -EINVAL;
	f->pixelformat = V4L2_PIX_FMT_MPEG;
	f->flags = V4L2_FMT_FLAG_COMPRESSED;
	return 0;
}

/* Format unique : MPEG-TS 1080p (l'encodeur du firmware ne sort que ça). */
static int vidioc_fmt(struct file *file, void *fh, struct v4l2_format *f)
{
	struct v4l2_pix_format *p = &f->fmt.pix;

	memset(p, 0, sizeof(*p));
	p->width = 1920;
	p->height = 1080;
	p->pixelformat = V4L2_PIX_FMT_MPEG;
	p->field = V4L2_FIELD_NONE;
	p->sizeimage = TS_SIZEIMAGE;
	p->colorspace = V4L2_COLORSPACE_REC709;
	return 0;
}

static int vidioc_enum_input(struct file *file, void *fh, struct v4l2_input *i)
{
	struct gl310_dev *dev = video_drvdata(file);
	struct gl310_signal sig;

	if (i->index)
		return -EINVAL;
	strscpy(i->name, "HDMI", sizeof(i->name));
	i->type = V4L2_INPUT_TYPE_CAMERA;
	i->capabilities = V4L2_IN_CAP_DV_TIMINGS;
	i->status = gl310_read_signal(dev, &sig) ? V4L2_IN_ST_NO_SIGNAL : 0;
	return 0;
}

static int vidioc_g_input(struct file *file, void *fh, unsigned int *i)
{
	*i = 0;
	return 0;
}

static int vidioc_s_input(struct file *file, void *fh, unsigned int i)
{
	return i ? -EINVAL : 0;
}

/* Le micro ne donne que les totaux et l'horloge : les « porches » portent toute la suppression.
 * L'horloge est recalée sur la fréquence standard reconnue, pour que total × fréquence = horloge. */
static void fill_timings(const struct gl310_signal *sig, struct v4l2_dv_timings *t)
{
	memset(t, 0, sizeof(*t));
	t->type = V4L2_DV_BT_656_1120;
	t->bt.width = sig->width;
	t->bt.height = sig->height;
	t->bt.hfrontporch = sig->h_total > sig->width ? sig->h_total - sig->width : 0;
	t->bt.vfrontporch = sig->v_total > sig->height ? sig->v_total - sig->height : 0;
	t->bt.pixelclock = (u64)sig->refresh_hz * sig->h_total * sig->v_total;
}

static int vidioc_query_dv_timings(struct file *file, void *fh, struct v4l2_dv_timings *t)
{
	struct gl310_dev *dev = video_drvdata(file);
	struct gl310_signal sig;
	int ret = gl310_read_signal(dev, &sig);

	if (!ret)
		fill_timings(&sig, t);
	return ret;
}

static const struct v4l2_ioctl_ops gl310_ioctl_ops = {
	.vidioc_querycap = vidioc_querycap,
	.vidioc_enum_fmt_vid_cap = vidioc_enum_fmt,
	.vidioc_g_fmt_vid_cap = vidioc_fmt,
	.vidioc_s_fmt_vid_cap = vidioc_fmt,
	.vidioc_try_fmt_vid_cap = vidioc_fmt,
	.vidioc_enum_input = vidioc_enum_input,
	.vidioc_g_input = vidioc_g_input,
	.vidioc_s_input = vidioc_s_input,
	.vidioc_query_dv_timings = vidioc_query_dv_timings,
	.vidioc_g_dv_timings = vidioc_query_dv_timings,
	.vidioc_log_status = v4l2_ctrl_log_status,
	.vidioc_subscribe_event = v4l2_ctrl_subscribe_event,
	.vidioc_unsubscribe_event = v4l2_event_unsubscribe,
};

/* ---- contrôles ------------------------------------------------------------------------------ */

static int gl310_s_ctrl(struct v4l2_ctrl *ctrl)
{
	struct gl310_dev *dev = container_of(ctrl->handler, struct gl310_dev, ctrls);
	struct gl310_settings *s = &dev->settings;
	bool image = ctrl->id == V4L2_CID_BRIGHTNESS || ctrl->id == V4L2_CID_CONTRAST;
	bool streaming;

	mutex_lock(&dev->io_lock);
	switch (ctrl->id) {
	case V4L2_CID_BRIGHTNESS:
		s->brightness = ctrl->val;
		break;
	case V4L2_CID_CONTRAST:
		s->contrast = ctrl->val;
		break;
	case V4L2_CID_MPEG_VIDEO_BITRATE:
		s->video_kbps = ctrl->val / 1000;
		break;
	case V4L2_CID_MPEG_VIDEO_GOP_SIZE:
		s->keyframe_interval = ctrl->val;
		break;
	case V4L2_CID_MPEG_AUDIO_AAC_BITRATE:
		s->audio_kbps = ctrl->val / 1000;
		break;
	case GL310_CID_AUDIO_OUT:
		s->audio_out = ctrl->val;
		break;
	}
	streaming = dev->state == GL310_STREAMING;
	mutex_unlock(&dev->io_lock);

	if (image)
		return gl310_apply_image(dev);
	if (streaming)
		atomic_set(&dev->restart_pending, 1);
	return 0;
}

static const struct v4l2_ctrl_ops gl310_ctrl_ops = {
	.s_ctrl = gl310_s_ctrl,
};

static const struct v4l2_ctrl_config audio_out_ctrl = {
	.ops = &gl310_ctrl_ops,
	.id = GL310_CID_AUDIO_OUT,
	.name = "Audio Out Jack",
	.type = V4L2_CTRL_TYPE_BOOLEAN,
	.min = 0,
	.max = 1,
	.step = 1,
	.def = 1,
};

static int add_controls(struct gl310_dev *dev)
{
	struct v4l2_ctrl_handler *hdl = &dev->ctrls;
	const struct gl310_settings d = GL310_DEFAULT_SETTINGS;

	v4l2_ctrl_handler_init(hdl, 6);
	v4l2_ctrl_new_std(hdl, &gl310_ctrl_ops, V4L2_CID_BRIGHTNESS, 0, 255, 1, d.brightness);
	v4l2_ctrl_new_std(hdl, &gl310_ctrl_ops, V4L2_CID_CONTRAST, 0, 255, 1, d.contrast);
	v4l2_ctrl_new_std(hdl, &gl310_ctrl_ops, V4L2_CID_MPEG_VIDEO_BITRATE,
			  GL310_VIDEO_KBPS_MIN * 1000, GL310_VIDEO_KBPS_MAX * 1000, 1000, d.video_kbps * 1000);
	v4l2_ctrl_new_std(hdl, &gl310_ctrl_ops, V4L2_CID_MPEG_VIDEO_GOP_SIZE,
			  GL310_KEYFRAME_MIN, GL310_KEYFRAME_MAX, 1, d.keyframe_interval);
	v4l2_ctrl_new_std(hdl, &gl310_ctrl_ops, V4L2_CID_MPEG_AUDIO_AAC_BITRATE,
			  GL310_AUDIO_KBPS_MIN * 1000, GL310_AUDIO_KBPS_MAX * 1000, 32000, d.audio_kbps * 1000);
	v4l2_ctrl_new_custom(hdl, &audio_out_ctrl, NULL);
	if (hdl->error)
		return hdl->error;
	dev->v4l2_dev.ctrl_handler = hdl;
	return v4l2_ctrl_handler_setup(hdl);
}

/* ---- enregistrement ------------------------------------------------------------------------- */

/* Appelé quand plus personne n'utilise le périphérique (débranché et tous les fichiers fermés). */
static void gl310_v4l2_release(struct v4l2_device *v4l2_dev)
{
	struct gl310_dev *dev = container_of(v4l2_dev, struct gl310_dev, v4l2_dev);

	v4l2_ctrl_handler_free(&dev->ctrls);
	v4l2_device_unregister(v4l2_dev);
	gl310_free(dev);
}

/* En cas d'échec, dev est libéré. */
int gl310_v4l2_register(struct gl310_dev *dev)
{
	struct video_device *vdev = &dev->vdev;
	int ret;

	dev->v4l2_dev.release = gl310_v4l2_release;
	ret = v4l2_device_register(&dev->intf->dev, &dev->v4l2_dev);
	if (ret) {
		gl310_free(dev);
		return ret;
	}
	ret = add_controls(dev);
	if (ret)
		goto err;

	strscpy(vdev->name, "AVerMedia GL310", sizeof(vdev->name));
	vdev->v4l2_dev = &dev->v4l2_dev;
	vdev->fops = &gl310_fops;
	vdev->ioctl_ops = &gl310_ioctl_ops;
	vdev->release = video_device_release_empty;
	vdev->lock = &dev->lock;
	vdev->vfl_dir = VFL_DIR_RX;
	vdev->device_caps = V4L2_CAP_VIDEO_CAPTURE | V4L2_CAP_READWRITE;
	video_set_drvdata(vdev, dev);
	ret = video_register_device(vdev, VFL_TYPE_VIDEO, -1);
	if (ret)
		goto err;
	dev_info(&dev->intf->dev, "flux disponible sur %s\n", video_device_node_name(vdev));
	return 0;
err:
	v4l2_device_put(&dev->v4l2_dev);
	return ret;
}

void gl310_v4l2_unregister(struct gl310_dev *dev)
{
	v4l2_device_disconnect(&dev->v4l2_dev);
	video_unregister_device(&dev->vdev);
	v4l2_device_put(&dev->v4l2_dev);
}

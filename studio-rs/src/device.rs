//! `Gl310` : la seule partie qui parle au boîtier, en USB (libusb). Traduction de tools/gl310_*.py.
//!
//! Endpoints : 0x04 commandes, 0x83 réponses, 0x02 données vers le boîtier, 0x81 données du boîtier.
//! Commandes (adresses mémoire en mots de 4 octets) :
//!   01 01 01 00 reg val        écriture d'un registre du pont      01 00 n 00 reg     lecture de n registres
//!   02 01 04 00 a a val        écriture d'un mot en mémoire ARM    02 00 04 00 a a    lecture d'un mot
//!   09 0W 08 00 0 addr nmots   bloc mémoire (W=1 écriture sur EP 0x02, W=0 lecture sur EP 0x81)
//!   07 0R 00 00 00 00 00 00    ARM : R=0 arrêt, R=1 démarrage
//!   0B 01 n 00 15 0 0 0 reg …  écriture micro                       0C 01 n 00 15 0 0 0 reg   lecture micro
//! Boîte aux lettres (comme le C985) : vers l'ARM 0x6F8..0x6D0 paramètres, 0x6CC état, 0x6FC opcode ;
//! depuis l'ARM 0x6C8 état (bit 0 = message prêt), 0x6B0..0x6C4 contenu.
//! Rien n'est permanent : débrancher le boîtier le remet à zéro. Le micro 0x47/0x49 (profil permanent) est refusé.

use std::fmt;
use std::path::Path;
use std::thread::sleep;
use std::time::Duration;

use chrono::{Datelike, Timelike};
use rusb::{Context, DeviceHandle, UsbContext};

use crate::model::{audio_control_param, gop_loop_filter, parse_timing, rate_control, Settings, Signal};
use crate::tables::{self, Cmd};

const VENDOR: u16 = 0x07CA;
const PRODUCT: u16 = 0xC835;
const PRODUCT_LOCKED: u16 = 0xD835; // identifiant vu après certains essais : il faut débrancher 10 s
const EP_CMD_OUT: u8 = 0x04;
const EP_CMD_IN: u8 = 0x83;
const EP_DATA_OUT: u8 = 0x02;
const EP_DATA_IN: u8 = 0x81;
const CMD_TIMEOUT: Duration = Duration::from_millis(1000);
const DATA_TIMEOUT: Duration = Duration::from_millis(5000);
const BLOCK_WORDS: u32 = 0x2000;

const REG_TO_ARM_PARAM0: u32 = 0x06F8;
const REG_TO_ARM_STATUS: u32 = 0x06CC;
const REG_TO_ARM_MSG: u32 = 0x06FC;
const REG_FROM_ARM_STATUS: u32 = 0x06C8;
const REG_FROM_ARM_MSG: u32 = 0x06B0;
const MSG_FRAME: u32 = 0x40;
const TAG_TS: u32 = 0x83;

const MCU_ADDR: u8 = 0x15;
const MCU_SIGNAL: u8 = 0x1C;
const MCU_SIGNAL_HDMI: u8 = 0x04;
const MCU_TIMING: u8 = 0x1D;
const MCU_PROFILE_REGS: [u8; 2] = [0x47, 0x49];

/// Mémoire de l'ARM (en mots) : images des firmwares et configuration de démarrage QPSOS v2.
const AUDIO_BASE: u32 = 0x40000;
const VIDEO_BASE: u32 = 0x00000;
const AUDIO_BSS_WORDS: [u32; 3] = [0x2000, 0x2000, 0x1E3B];
const CFG_FIXED_MODE: u32 = 0x2F1094 / 4;
const CFG_INT_MODE: u32 = 0x2F1090 / 4;
const CFG_BASE: u32 = 0x2F2000 / 4;
const QSOS: &[u8] = b"QSOS";

#[derive(Debug)]
pub enum Error {
    Usb(rusb::Error),
    Protocol(String),
}

impl From<rusb::Error> for Error {
    fn from(e: rusb::Error) -> Self {
        Error::Usb(e)
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        match self {
            Error::Usb(rusb::Error::Access) => write!(f, "accès USB refusé (règle udev 70-avermedia-gl310 absente ?)"),
            Error::Usb(rusb::Error::Busy) => write!(f, "boîtier déjà utilisé par un autre programme (ou le pilote noyau gl310)"),
            Error::Usb(e) => write!(f, "USB : {e}"),
            Error::Protocol(m) => write!(f, "{m}"),
        }
    }
}

pub type Result<T> = std::result::Result<T, Error>;

fn protocol<T>(message: impl Into<String>) -> Result<T> {
    Err(Error::Protocol(message.into()))
}

fn le32(b: &[u8], i: usize) -> u32 {
    u32::from_le_bytes([b[4 * i], b[4 * i + 1], b[4 * i + 2], b[4 * i + 3]])
}

fn bcd(v: u32) -> u8 {
    (((v / 10) << 4) | (v % 10)) as u8
}

pub struct Gl310 {
    handle: DeviceHandle<Context>,
}

impl Gl310 {
    /// `Ok(None)` si aucun boîtier n'est branché.
    pub fn open() -> Result<Option<Gl310>> {
        let context = Context::new()?;
        for device in context.devices()?.iter() {
            let desc = device.device_descriptor()?;
            if desc.vendor_id() != VENDOR {
                continue;
            }
            if desc.product_id() == PRODUCT_LOCKED {
                return protocol("le boîtier est en mode d835 : débranche-le 10 s puis rebranche-le");
            }
            if desc.product_id() == PRODUCT {
                let handle = device.open()?;
                handle.claim_interface(0)?;
                let gl310 = Gl310 { handle };
                gl310.flush();
                return Ok(Some(gl310));
            }
        }
        Ok(None)
    }

    /// Vide les données restées en attente (capture interrompue en plein transfert).
    fn flush(&self) {
        let mut buf = vec![0u8; 65536];
        for ep in [EP_DATA_IN, EP_CMD_IN] {
            while self.handle.read_bulk(ep, &mut buf, Duration::from_millis(50)).is_ok() {}
        }
    }

    // ---- commandes de base -----------------------------------------------------------------

    fn send(&self, cmd: &[u8], expect: bool) -> Result<Vec<u8>> {
        if cmd[0] == 0x0B && cmd.len() > 8 && MCU_PROFILE_REGS.contains(&cmd[8]) {
            return protocol(format!("écriture du profil permanent du micro ({:#04x}) refusée", cmd[8]));
        }
        self.handle.write_bulk(EP_CMD_OUT, cmd, CMD_TIMEOUT)?;
        if !expect {
            return Ok(Vec::new());
        }
        let mut answer = vec![0u8; 512];
        let n = self.handle.read_bulk(EP_CMD_IN, &mut answer, CMD_TIMEOUT)?;
        answer.truncate(n);
        Ok(answer)
    }

    fn reg_write(&self, reg: u32, val: u32) -> Result<()> {
        let mut cmd = vec![0x01, 0x01, 0x01, 0x00];
        cmd.extend(reg.to_le_bytes());
        cmd.extend(val.to_le_bytes());
        self.send(&cmd, false).map(drop)
    }

    fn reg_read(&self, reg: u32, count: u8) -> Result<Vec<u32>> {
        let mut cmd = vec![0x01, 0x00, count, 0x00];
        cmd.extend(reg.to_le_bytes());
        let answer = self.send(&cmd, true)?;
        if answer.len() < 4 * count as usize {
            return protocol(format!("réponse courte à la lecture de {reg:#06x}"));
        }
        Ok((0..count as usize).map(|i| le32(&answer, i)).collect())
    }

    fn reg(&self, reg: u32) -> Result<u32> {
        Ok(self.reg_read(reg, 1)?[0])
    }

    fn arm_run(&self, run: bool) -> Result<()> {
        self.send(&[0x07, run as u8, 0, 0, 0, 0, 0, 0], false).map(drop)
    }

    fn word_write(&self, addr: u32, val: u32) -> Result<()> {
        let mut cmd = vec![0x02, 0x01, 0x04, 0x00];
        cmd.extend(addr.to_le_bytes());
        cmd.extend(addr.to_le_bytes());
        cmd.extend(val.to_le_bytes());
        self.send(&cmd, false).map(drop)
    }

    fn word_read(&self, addr: u32) -> Result<Vec<u8>> {
        let mut cmd = vec![0x02, 0x00, 0x04, 0x00];
        cmd.extend(addr.to_le_bytes());
        cmd.extend(addr.to_le_bytes());
        self.send(&cmd, true)
    }

    fn mem_header(&self, write: bool, addr: u32, words: u32) -> Result<()> {
        let mut cmd = vec![0x09, write as u8, 0x08, 0x00, 0, 0, 0, 0];
        cmd.extend(addr.to_le_bytes());
        cmd.extend(words.to_le_bytes());
        self.send(&cmd, true).map(drop)
    }

    fn read_data(&self, bytes: usize) -> Result<Vec<u8>> {
        let mut buf = vec![0u8; bytes];
        let mut got = 0;
        while got < bytes {
            let n = self.handle.read_bulk(EP_DATA_IN, &mut buf[got..], DATA_TIMEOUT)?;
            if n == 0 {
                return protocol("données vides sur EP 0x81");
            }
            got += n;
        }
        Ok(buf)
    }

    /// Lit `bytes` octets de mémoire ARM à partir du mot `addr`, par blocs de 32 Ko.
    fn mem_read(&self, addr: u32, bytes: usize) -> Result<Vec<u8>> {
        let words = bytes.div_ceil(4) as u32;
        let mut out = Vec::with_capacity(words as usize * 4);
        let mut done = 0;
        while done < words {
            let n = BLOCK_WORDS.min(words - done);
            self.mem_header(false, addr + done, n)?;
            out.extend(self.read_data(n as usize * 4)?);
            done += n;
        }
        out.truncate(bytes);
        Ok(out)
    }

    /// Écrit `data` en mémoire ARM par blocs de 32 Ko ; si `verify`, relit et compare chaque bloc.
    fn mem_write(&self, addr: u32, data: &[u8], verify: bool) -> Result<()> {
        for (i, chunk) in data.chunks(BLOCK_WORDS as usize * 4).enumerate() {
            let a = addr + i as u32 * BLOCK_WORDS;
            self.mem_header(true, a, chunk.len() as u32 / 4)?;
            self.handle.write_bulk(EP_DATA_OUT, chunk, DATA_TIMEOUT)?;
            if verify {
                self.mem_header(false, a, chunk.len() as u32 / 4)?;
                if self.read_data(chunk.len())? != chunk {
                    return protocol(format!("relecture du firmware différente au mot {a:#x}"));
                }
            }
        }
        Ok(())
    }

    // ---- microcontrôleur -------------------------------------------------------------------

    fn mcu_read(&self, reg: u8, n: u8) -> Result<Vec<u8>> {
        let answer = self.send(&[0x0C, 0x01, n, 0x00, MCU_ADDR, 0, 0, 0, reg], true)?;
        if answer.len() < n as usize {
            return protocol(format!("réponse courte du micro (registre {reg:#04x})"));
        }
        Ok(answer[..n as usize].to_vec())
    }

    // ---- état ------------------------------------------------------------------------------

    pub fn firmware_loaded(&self) -> Result<bool> {
        Ok(self.word_read(0x40)?.starts_with(QSOS))
    }

    pub fn signal(&self) -> Result<Option<Signal>> {
        if self.mcu_read(MCU_SIGNAL, 1)?[0] & MCU_SIGNAL_HDMI == 0 {
            return Ok(None);
        }
        let raw: [u8; 7] = self.mcu_read(MCU_TIMING, 7)?.try_into().expect("7 octets demandés");
        Ok(parse_timing(&raw))
    }

    // ---- démarrage -------------------------------------------------------------------------

    fn run_table(&self, table: &[Cmd]) -> Result<()> {
        for cmd in table {
            self.send(cmd.bytes, cmd.flags & tables::EXPECT != 0)?;
        }
        Ok(())
    }

    /// Initialisation officielle (206 écritures I2C) puis firmwares en RAM et démarrage de l'ARM,
    /// sur un boîtier fraîchement branché. Même ordre que tools/gl310_boot.py (trace lignes 1 à 370).
    pub fn boot(&self, firmware_dir: &Path, progress: &mut dyn FnMut(&str)) -> Result<()> {
        let read = |name: &str| {
            std::fs::read(firmware_dir.join(name))
                .map_err(|e| Error::Protocol(format!("firmware {name} illisible dans {} : {e}", firmware_dir.display())))
        };
        let (audio, video) = (read("qpaudfwusb.bin")?, read("qpvidfwusb.bin")?);
        if audio.len() % 4 != 0 || video.len() % 4 != 0 {
            return protocol("taille de firmware non multiple de 4");
        }
        progress("initialisation officielle (206 écritures I2C)");
        self.run_table(tables::OFFICIAL_INIT)?;

        self.arm_run(false)?;
        self.reg_write(REG_TO_ARM_STATUS, 0)?;
        for (reg, val) in [
            (0x081C, 0x0000_4000), (0x0820, 0x0010_3FFF), (0x0824, 0), (0x0828, 0x0010_4000), (0x082C, 0x0020_3FFF),
            (0x0830, 0x0010_0000), (0x0834, 0x0020_4000), (0x0838, 0x0030_3FFF), (0x083C, 0x0020_0000),
            (0x0840, 0x7000_3124), (0x0840, 0x9000_3124),
        ] {
            self.reg_write(reg, val)?;
        }
        self.reg(0x0050)?;
        self.reg_write(0x0050, 0x0404)?;
        self.reg(0x0050)?;
        self.reg_write(0x0050, 0x0404)?;
        self.reg_write(0x0610, 0)?;
        self.reg_write(0x0614, 0)?;
        self.reg(0x0000)?;
        self.reg_write(0x0000, 0x07FF_0000)?;

        progress("firmware audio");
        let mut bss = AUDIO_BASE + audio.len() as u32 / 4;
        for words in AUDIO_BSS_WORDS {
            self.mem_write(bss, &vec![0u8; words as usize * 4], false)?;
            bss += words;
        }
        self.mem_write(AUDIO_BASE, &audio, true)?;
        self.word_write(CFG_FIXED_MODE, 1)?;
        self.word_write(CFG_INT_MODE, 0)?;
        self.word_write(CFG_BASE + 1, 0)?;
        self.reg_write(REG_TO_ARM_STATUS, 0)?;

        progress("firmware vidéo");
        self.mem_write(VIDEO_BASE, &video, true)?;
        self.arm_run(true)?;
        self.reg_write(REG_TO_ARM_STATUS, 0)?;
        self.word_read(0x40)?;
        self.word_read(0x41)?;
        self.word_read(CFG_BASE + 4)?;
        self.reg(0x0610)?;
        self.reg_write(0x0610, 0)?;
        self.reg(0x0610)?;
        self.reg_write(0x0610, 0)?;
        self.reg(0x0610)?;
        self.reg(0x0618)?;
        if !self.firmware_loaded()? {
            return protocol("firmware absent après le démarrage de l'ARM");
        }
        Ok(())
    }

    // ---- boîte aux lettres -----------------------------------------------------------------

    /// Attend que le firmware ait consommé le message précédent (0x6CC bit 0 à 0), puis continue quoi qu'il arrive.
    fn wait_to_arm_free(&self, tries: u32, step: Duration) -> Result<()> {
        for _ in 0..tries {
            if self.reg(REG_TO_ARM_STATUS)? & 1 == 0 {
                return Ok(());
            }
            sleep(step);
        }
        Ok(())
    }

    fn mbox_send(&self, opcode: u32, task: u32, param0: u32, slots: &[u32]) -> Result<()> {
        self.wait_to_arm_free(200, Duration::from_millis(1))?;
        self.reg_write(REG_TO_ARM_PARAM0, param0)?;
        for (i, &v) in slots.iter().enumerate() {
            self.reg_write(REG_TO_ARM_PARAM0 - 4 - 4 * i as u32, v)?;
        }
        self.reg_write(REG_TO_ARM_STATUS, (task << 16) | 1)?;
        self.reg_write(REG_TO_ARM_MSG, (task << 16) | opcode)
    }

    /// Libère 0x6C8, envoie l'accusé si demandé, et rend le tampon d'une image (CompleteArm 0x30).
    fn release(&self, st: u32, m: &[u32]) -> Result<()> {
        let (msg, p0, size, pts) = (m[0], m[1], m[4], m[5]);
        let (kind, task) = (msg & 0xFFFF, msg >> 16);
        self.reg_write(REG_FROM_ARM_STATUS, st & !1)?;
        if st & 0x100 != 0 {
            self.mbox_send(if kind < 0x80 { 0x31 } else { 0xA2 }, task, 0, &[])?;
        }
        if kind & 0xFF == MSG_FRAME {
            self.mbox_send(0x30, task, p0 & 0xFFFF, &[size, pts & 0x7FFF_FFFF, pts >> 31, 0, (p0 >> 24) & 0xFF])?;
        }
        Ok(())
    }

    /// Rend sans les lire les messages en attente ; s'arrête dès que plus rien n'attend.
    fn drain(&self) -> Result<()> {
        for _ in 0..2000 {
            let st = self.reg(REG_FROM_ARM_STATUS)?;
            if st & 1 == 0 {
                break;
            }
            let m = self.reg_read(REG_FROM_ARM_MSG, 6)?;
            self.release(st, &m)?;
        }
        Ok(())
    }

    // ---- encodeur --------------------------------------------------------------------------

    /// Trace lignes 371 à 757 (0x0A, SystemOpen, SystemLink, réglages 0x10, bloc de config, StartEncoder),
    /// avec les réglages à la place des valeurs de la trace là où le générateur les a marquées.
    pub fn start_encoder(&self, s: &Settings) -> Result<()> {
        let s = s.validate().map_err(Error::Protocol)?;
        self.drain()?;
        let now = chrono::Local::now();
        for cmd in tables::ENCODER_START {
            if cmd.flags & tables::WAIT_MBOX != 0 {
                self.wait_to_arm_free(50, Duration::from_millis(10))?;
                continue;
            }
            let mut b = cmd.bytes.to_vec();
            if cmd.flags & tables::RATE != 0 {
                b[8..12].copy_from_slice(&rate_control(&s).to_le_bytes());
            }
            if cmd.flags & tables::GOP != 0 {
                b[8..12].copy_from_slice(&gop_loop_filter(&s).to_le_bytes());
            }
            if cmd.flags & tables::AUDIO != 0 {
                b[8..12].copy_from_slice(&audio_control_param(&s).to_le_bytes());
            }
            if cmd.flags & tables::RTC != 0 {
                let t = [now.year() as u32 % 100, now.month(), now.day(), now.hour(), now.minute(), now.second()];
                for (i, v) in t.into_iter().enumerate() {
                    b[9 + i] = bcd(v);
                }
            }
            self.send(&b, cmd.flags & tables::EXPECT != 0)?;
        }
        Ok(())
    }

    /// 0x02 StopEncoder puis 0xF3 SystemClose : l'encodeur peut ensuite être relancé sans rebrancher.
    pub fn stop_encoder(&self) -> Result<()> {
        self.drain()?;
        self.mbox_send(0x02, 0, 0, &[])?;
        sleep(Duration::from_millis(300));
        self.drain()?;
        self.mbox_send(0xF3, 0, 0, &[])?;
        sleep(Duration::from_millis(300));
        self.drain()
    }

    /// Traite un message du firmware s'il y en a un : renvoie le morceau de MPEG-TS qu'il apporte.
    /// `Ok(None)` : rien en attente, ou message sans flux.
    pub fn poll_ts(&self) -> Result<Option<Vec<u8>>> {
        let st = self.reg(REG_FROM_ARM_STATUS)?;
        if st & 1 == 0 {
            return Ok(None);
        }
        let m = self.reg_read(REG_FROM_ARM_MSG, 6)?;
        let (msg, p0, addr, size) = (m[0], m[1], m[2], m[4]);
        // Lire les données AVANT de rendre le tampon, sinon le firmware peut le réécrire.
        let data = if msg & 0xFF == MSG_FRAME && p0 & 0xFFFF == TAG_TS && size > 0 && size < (1 << 20) {
            let mut data = self.mem_read(addr, size as usize * 4)?;
            // Les octets de chaque mot de 4 octets arrivent inversés.
            for word in data.as_chunks_mut::<4>().0 {
                word.reverse();
            }
            Some(data)
        } else {
            None
        };
        self.release(st, &m)?; // toujours rendre le tampon, même s'il n'est pas lu
        Ok(data)
    }
}

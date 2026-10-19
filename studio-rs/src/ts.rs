//! MPEG-TS : réalignement des morceaux reçus du boîtier en paquets de 188 octets, mémoire des tables PAT/PMT, et
//! repérage des images clés, pour qu'un lecteur qui arrive en cours de route (OBS, la tour, un enregistrement)
//! démarre tout de suite sur une image décodable au lieu d'attendre (jusqu'à 2 s) et de rejouer ce retard.
//! Le boîtier ne pose pas l'indicateur RAI : une image clé est un début de PES vidéo contenant un SPS (NAL 7),
//! suivi de PPS et IDR (séquence observée : AUD, SPS, PPS, IDR, toutes les 2 s).

pub const PACKET: usize = 188;
const SYNC: u8 = 0x47;

pub fn pid(packet: &[u8]) -> u16 {
    ((packet[1] as u16 & 0x1F) << 8) | packet[2] as u16
}

/// Charge utile d'un paquet (après l'éventuel champ d'adaptation), ou `None`.
fn payload(packet: &[u8]) -> Option<&[u8]> {
    match (packet[3] >> 4) & 0x3 {
        1 => Some(&packet[4..]),
        3 => packet.get(5 + packet[4] as usize..),
        _ => None,
    }
}

/// PID de la PMT du premier programme annoncé par une PAT.
pub fn pmt_pid_from_pat(packet: &[u8]) -> Option<u16> {
    let payload = payload(packet)?;
    let section = payload.get(1 + *payload.first()? as usize..)?;
    let length = ((section.get(1)? & 0x0F) as usize) << 8 | *section.get(2)? as usize;
    let entries = section.get(8..(3 + length).checked_sub(4)?)?;
    entries
        .as_chunks::<4>().0.iter()
        .find(|e| (e[0] as u16) << 8 | e[1] as u16 != 0)
        .map(|e| ((e[2] as u16 & 0x1F) << 8) | e[3] as u16)
}

/// PID de la vidéo H.264 (stream_type 0x1B) annoncée par une PMT.
pub fn video_pid_from_pmt(packet: &[u8]) -> Option<u16> {
    let payload = payload(packet)?;
    let section = payload.get(1 + *payload.first()? as usize..)?;
    if *section.first()? != 0x02 {
        return None;
    }
    let length = ((section.get(1)? & 0x0F) as usize) << 8 | *section.get(2)? as usize;
    let info_length = ((section.get(10)? & 0x0F) as usize) << 8 | *section.get(11)? as usize;
    let mut streams = section.get(12 + info_length..(3 + length).checked_sub(4)?)?;
    while streams.len() >= 5 {
        if streams[0] == 0x1B {
            return Some(((streams[1] as u16 & 0x1F) << 8) | streams[2] as u16);
        }
        let es_info_length = ((streams[3] & 0x0F) as usize) << 8 | streams[4] as usize;
        streams = streams.get(5 + es_info_length..)?;
    }
    None
}

/// Vrai si le paquet commence une image clé de la vidéo `video_pid` (début de PES contenant un SPS).
pub fn starts_keyframe(packet: &[u8], video_pid: u16) -> bool {
    if pid(packet) != video_pid || packet[1] & 0x40 == 0 {
        return false;
    }
    let Some(pes) = payload(packet).filter(|p| p.len() > 9 && p.starts_with(&[0, 0, 1])) else { return false };
    let es = pes.get(9 + pes[8] as usize..).unwrap_or_default();
    es.windows(4).any(|w| w[..3] == [0, 0, 1] && w[3] & 0x1F == 7)
}

#[derive(Default)]
pub struct TsAligner {
    pending: Vec<u8>,
    pat: Option<Vec<u8>>,
    pmt: Option<Vec<u8>>,
    pmt_pid: Option<u16>,
    video_pid: Option<u16>,
    last_keyframe: Option<usize>,
}

impl TsAligner {
    /// Ajoute un morceau et renvoie les paquets complets et alignés qu'il termine.
    pub fn feed(&mut self, data: &[u8]) -> Vec<u8> {
        self.last_keyframe = None;
        self.pending.extend_from_slice(data);
        let mut out = Vec::with_capacity(self.pending.len());
        let mut pos = 0;
        while self.pending.len() - pos >= PACKET {
            if self.pending[pos] != SYNC {
                pos = self.pending[pos + 1..].iter().position(|&b| b == SYNC).map_or(self.pending.len(), |i| pos + 1 + i);
                continue;
            }
            let packet = &self.pending[pos..pos + PACKET];
            match pid(packet) {
                0 => {
                    self.pmt_pid = pmt_pid_from_pat(packet).or(self.pmt_pid);
                    self.pat = Some(packet.to_vec());
                }
                p if Some(p) == self.pmt_pid => {
                    self.video_pid = video_pid_from_pmt(packet).or(self.video_pid);
                    self.pmt = Some(packet.to_vec());
                }
                _ => {}
            }
            if self.last_keyframe.is_none() && self.video_pid.is_some_and(|v| starts_keyframe(packet, v)) {
                self.last_keyframe = Some(out.len());
            }
            out.extend_from_slice(packet);
            pos += PACKET;
        }
        self.pending.drain(..pos);
        out
    }

    /// Position (en octets) du premier paquet qui commence une image clé dans ce qu'a renvoyé le dernier `feed`.
    pub fn last_keyframe(&self) -> Option<usize> {
        self.last_keyframe
    }

    /// Vrai quand la PAT et la PMT du flux ont été vues : un nouveau lecteur peut démarrer.
    pub fn has_header(&self) -> bool {
        self.pat.is_some() && self.pmt.is_some()
    }

    /// Dernières PAT et PMT vues, à envoyer en premier à un nouveau lecteur.
    pub fn header(&self) -> Vec<u8> {
        [self.pat.as_deref(), self.pmt.as_deref()].into_iter().flatten().flatten().copied().collect()
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;

    pub(crate) fn packet(pid: u16, fill: u8) -> Vec<u8> {
        let mut p = vec![fill; PACKET];
        p[0] = SYNC;
        p[1] = 0x40 | (pid >> 8) as u8;
        p[2] = pid as u8;
        p[3] = 0x10;
        p
    }

    pub(crate) fn pat(pmt_pid: u16) -> Vec<u8> {
        let mut p = packet(0, 0xFF);
        let section = [
            0x00, // pointer_field
            0x00, 0xB0, 13, 0x00, 0x01, 0xC1, 0x00, 0x00, // table_id, longueur 13, ts_id 1, version, sections
            0x00, 0x01, 0xE0 | (pmt_pid >> 8) as u8, pmt_pid as u8, // programme 1 -> PMT
            0xDE, 0xAD, 0xBE, 0xEF, // CRC (non vérifié)
        ];
        p[4..4 + section.len()].copy_from_slice(&section);
        p
    }

    pub(crate) fn pmt(pmt_pid: u16, video_pid: u16, audio_pid: u16) -> Vec<u8> {
        let mut p = packet(pmt_pid, 0xFF);
        let mut section = vec![
            0x00, // pointer_field
            0x02, 0xB0, 9 + 2 * 5 + 4, 0x00, 0x01, 0xC1, 0x00, 0x00, // table_id 2, longueur, programme 1
            0xE0 | (video_pid >> 8) as u8, video_pid as u8, 0xF0, 0x00, // PCR_PID, program_info_length
        ];
        for (stream_type, es_pid) in [(0x0F, audio_pid), (0x1B, video_pid)] {
            section.extend([stream_type, 0xE0 | (es_pid >> 8) as u8, es_pid as u8, 0xF0, 0x00]);
        }
        section.extend([0xDE, 0xAD, 0xBE, 0xEF]);
        p[4..4 + section.len()].copy_from_slice(&section);
        p
    }

    /// Début de PES vidéo : en-tête PES puis les NAL donnés (AUD d'abord, comme le boîtier).
    pub(crate) fn video_start(video_pid: u16, nal_types: &[u8]) -> Vec<u8> {
        let mut p = packet(video_pid, 0xAA);
        let mut payload = vec![0x00, 0x00, 0x01, 0xE0, 0x00, 0x00, 0x80, 0x80, 0x05, 0x21, 0x00, 0x01, 0x00, 0x01];
        for nal in nal_types {
            payload.extend([0x00, 0x00, 0x00, 0x01, 0x60 | nal, 0x42]);
        }
        p[4..4 + payload.len()].copy_from_slice(&payload);
        p
    }

    #[test]
    fn trouve_la_video_dans_la_pmt() {
        // Quand la PMT annonce l'AAC sur 0x45 puis le H.264 sur 0x44, la vidéo doit être 0x44.
        assert_eq!(video_pid_from_pmt(&pmt(0x42, 0x44, 0x45)), Some(0x44));
        assert_eq!(video_pid_from_pmt(&pat(0x42)), None);
    }

    #[test]
    fn reconnait_le_debut_d_une_image_cle() {
        // Quand un début de PES vidéo contient AUD, SPS, PPS, IDR, c'est une image clé ; AUD + tranche P n'en est pas une.
        assert!(starts_keyframe(&video_start(0x44, &[9, 7, 8, 5]), 0x44));
        assert!(!starts_keyframe(&video_start(0x44, &[9, 1]), 0x44));
        // Si le paquet appartient à un autre PID ou ne commence pas de PES, alors ce n'est pas une image clé.
        assert!(!starts_keyframe(&video_start(0x44, &[9, 7, 8, 5]), 0x45));
        let mut continuation = video_start(0x44, &[9, 7, 8, 5]);
        continuation[1] &= !0x40;
        assert!(!starts_keyframe(&continuation, 0x44));
    }

    #[test]
    fn situe_l_image_cle_dans_le_morceau() {
        // Quand PAT, PMT, une image P puis une image clé passent, la position de l'image clé doit être connue.
        let mut a = TsAligner::default();
        let mut data = [pat(0x42), pmt(0x42, 0x44, 0x45), video_start(0x44, &[9, 1])].concat();
        assert!(a.feed(&data).len() == 3 * PACKET && a.last_keyframe().is_none());
        data = [video_start(0x44, &[9, 1]), video_start(0x44, &[9, 7, 8, 5]), packet(0x45, 1)].concat();
        a.feed(&data);
        assert_eq!(a.last_keyframe(), Some(PACKET));
        a.feed(&packet(0x45, 2));
        assert_eq!(a.last_keyframe(), None);
    }

    #[test]
    fn lit_le_pid_d_un_paquet() {
        assert_eq!(pid(&packet(0x44, 0)), 0x44);
        assert_eq!(pid(&packet(0x1FFF, 0)), 0x1FFF);
    }

    #[test]
    fn trouve_la_pmt_dans_la_pat() {
        // Quand la PAT annonce le programme 1 sur le PID 0x42, la PMT doit être 0x42.
        assert_eq!(pmt_pid_from_pat(&pat(0x42)), Some(0x42));
    }

    #[test]
    fn saute_les_octets_parasites() {
        // Si le flux commence au milieu d'un paquet, alors seuls des paquets entiers et alignés sortent.
        let mut a = TsAligner::default();
        let mut data = vec![0x12, 0x34, 0x56];
        data.extend(packet(0x44, 1));
        data.extend(packet(0x45, 2));
        let out = a.feed(&data);
        assert_eq!(out.len(), 2 * PACKET);
        assert_eq!(out[0], SYNC);
        assert_eq!(out[PACKET], SYNC);
    }

    #[test]
    fn recolle_un_paquet_coupe_en_deux() {
        // Quand un paquet arrive en deux morceaux, il doit sortir entier au second.
        let mut a = TsAligner::default();
        let p = packet(0x44, 7);
        assert!(a.feed(&p[..100]).is_empty());
        assert_eq!(a.feed(&p[100..]), p);
    }

    #[test]
    fn retient_pat_et_pmt_pour_les_retardataires() {
        // Quand PAT puis PMT sont passées, un nouveau lecteur doit les recevoir en premier.
        let mut a = TsAligner::default();
        let (pat, pmt) = (pat(0x42), packet(0x42, 9));
        let mut data = pat.clone();
        data.extend(&pmt);
        data.extend(packet(0x44, 3));
        a.feed(&data);
        assert_eq!(a.header(), [pat, pmt].concat());
    }
}

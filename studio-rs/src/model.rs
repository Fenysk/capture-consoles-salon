//! Types purs de GL310 Studio : réglages, signal HDMI, état du boîtier, encodage des registres.
//!
//! Aucun accès USB ici. Mêmes règles et mêmes valeurs de test que `studio/model.py` (prototype Python).
//! On ne remplace que les champs dont la structure est prouvée par le pilote Windows (CQLCodec_Set) :
//!   RATE_CONTROL        bits 0-15 débit (kbit/s), le reste comme dans la trace
//!   GOP_LOOP_FILTER     bits 0-15 intervalle entre images clés (en images)
//!   AUDIO_CONTROL_PARAM bits 0-9 débit audio (kbit/s), bit 16 aou_pass_en (son sur la prise AUDIO OUT)

/// Valeurs de la trace Windows (registres 0x6EC, 0x6E0, 0x6D4 du bloc de configuration).
pub const TRACE_RATE_CONTROL: u32 = 0x0078_7530;
pub const TRACE_GOP_LOOP_FILTER: u32 = 0xF199_001E;
pub const TRACE_AUDIO_CONTROL_PARAM: u32 = 0x2116_1080;
const AOU_PASS_EN: u32 = 1 << 16;

/// Le firmware n'a été vérifié que sur ces valeurs.
pub const VIDEO_KBPS_CHOICES: [u32; 6] = [4000, 6000, 8000, 12000, 20000, 30000];
pub const KEYFRAME_CHOICES: [u32; 2] = [30, 60]; // en images, à 30 i/s : 1 s ou 2 s
pub const AUDIO_KBPS_CHOICES: [u32; 4] = [128, 160, 192, 256];

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Settings {
    pub video_kbps: u32,
    pub keyframe_interval: u32,
    pub audio_kbps: u32,
    /// aou_pass_en : son sur la prise AUDIO OUT du boîtier.
    pub audio_out: bool,
}

impl Default for Settings {
    fn default() -> Self {
        Settings { video_kbps: 8000, keyframe_interval: 60, audio_kbps: 128, audio_out: true }
    }
}

impl Settings {
    /// Refuse toute valeur hors des choix prévus.
    pub fn validate(self) -> Result<Settings, String> {
        if !VIDEO_KBPS_CHOICES.contains(&self.video_kbps) {
            return Err(format!("débit vidéo {} kbit/s non proposé {:?}", self.video_kbps, VIDEO_KBPS_CHOICES));
        }
        if !KEYFRAME_CHOICES.contains(&self.keyframe_interval) {
            return Err(format!("intervalle d'images clés {} non proposé {:?}", self.keyframe_interval, KEYFRAME_CHOICES));
        }
        if !AUDIO_KBPS_CHOICES.contains(&self.audio_kbps) {
            return Err(format!("débit audio {} kbit/s non proposé {:?}", self.audio_kbps, AUDIO_KBPS_CHOICES));
        }
        Ok(self)
    }

    /// Format du fichier de réglages : une ligne `clé=valeur` par champ.
    pub fn to_text(self) -> String {
        format!(
            "video_kbps={}\nkeyframe_interval={}\naudio_kbps={}\naudio_out={}\n",
            self.video_kbps, self.keyframe_interval, self.audio_kbps, self.audio_out
        )
    }

    /// Les clés absentes gardent leur valeur par défaut ; le résultat est validé.
    pub fn from_text(text: &str) -> Result<Settings, String> {
        let mut s = Settings::default();
        for line in text.lines().map(str::trim).filter(|l| !l.is_empty()) {
            let (key, value) = line.split_once('=').ok_or_else(|| format!("ligne illisible : {line}"))?;
            let bad = |_| format!("valeur illisible : {line}");
            match key.trim() {
                "video_kbps" => s.video_kbps = value.trim().parse().map_err(bad)?,
                "keyframe_interval" => s.keyframe_interval = value.trim().parse().map_err(bad)?,
                "audio_kbps" => s.audio_kbps = value.trim().parse().map_err(bad)?,
                "audio_out" => s.audio_out = value.trim().parse().map_err(|_| format!("valeur illisible : {line}"))?,
                // Anciennes versions : luminosité et contraste du micro, sans effet sur l'image encodée.
                "brightness" | "contrast" => {}
                other => return Err(format!("clé inconnue : {other}")),
            }
        }
        s.validate()
    }
}

pub fn rate_control(s: &Settings) -> u32 {
    (TRACE_RATE_CONTROL & !0xFFFF) | s.video_kbps
}

pub fn gop_loop_filter(s: &Settings) -> u32 {
    (TRACE_GOP_LOOP_FILTER & !0xFFFF) | s.keyframe_interval
}

pub fn audio_control_param(s: &Settings) -> u32 {
    let v = (TRACE_AUDIO_CONTROL_PARAM & !0x3FF & !AOU_PASS_EN) | s.audio_kbps;
    if s.audio_out { v | AOU_PASS_EN } else { v }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Signal {
    pub width: u32,
    pub height: u32,
    pub refresh_hz: u32,
}

const STANDARD_RATES: [u32; 6] = [24, 25, 30, 50, 60, 120];
const PCLK_NUMERATOR_KHZ: f64 = 0x34BC00 as f64; // horloge pixel (kHz) = 0x34BC00 / compteur (c985_nuc100.c)

/// Registre 0x1D du micro (7 octets) : totaux, actifs et compteur d'horloge. `None` si inexploitable.
pub fn parse_timing(r: &[u8; 7]) -> Option<Signal> {
    let h_total = ((r[1] as u32 & 0x0F) << 8) | r[0] as u32;
    let v_total = ((r[4] as u32 & 0x0F) << 8) | r[3] as u32;
    if r[6] == 0 || h_total == 0 || v_total == 0 {
        return None;
    }
    let hz = PCLK_NUMERATOR_KHZ / r[6] as f64 * 1000.0 / (h_total * v_total) as f64;
    let refresh_hz = *STANDARD_RATES.iter().min_by(|a, b| (**a as f64 - hz).abs().total_cmp(&(**b as f64 - hz).abs()))?;
    Some(Signal { width: ((r[1] as u32 & 0xF0) << 4) | r[2] as u32, height: ((r[4] as u32 & 0xF0) << 4) | r[5] as u32, refresh_hz })
}

/// Un seul état du boîtier à la fois, chacun avec ses propres champs.
#[derive(Clone, Debug, PartialEq)]
pub enum BoxStatus {
    /// Pas de boîtier GL310 branché (ou pris par un autre programme).
    Absent,
    /// Initialisation officielle et firmwares en cours de chargement.
    Booting { step: String },
    /// Firmware prêt, encodeur arrêté.
    Idle { signal: Option<Signal> },
    /// Encodeur en marche.
    Streaming { signal: Option<Signal>, mbps: f32, messages_per_s: f32 },
    /// Erreur : débrancher le boîtier 10 s puis le rebrancher.
    Failed { error: String },
}

#[cfg(test)]
mod tests {
    use super::*;

    fn with(f: impl FnOnce(&mut Settings)) -> Settings {
        let mut s = Settings::default();
        f(&mut s);
        s
    }

    #[test]
    fn debit_video_remplace_seulement_le_debit() {
        // Quand le débit vidéo vaut 8000 kbit/s, RATE_CONTROL doit garder qp_update=120 de la trace.
        assert_eq!(rate_control(&with(|s| s.video_kbps = 8000)), 0x0078_1F40);
    }

    #[test]
    fn intervalle_images_cles() {
        // Quand l'intervalle vaut 60 images, GOP_LOOP_FILTER doit garder le reste de la trace.
        assert_eq!(gop_loop_filter(&with(|s| s.keyframe_interval = 60)), 0xF199_003C);
    }

    #[test]
    fn audio_out_et_debit_audio() {
        // Quand AUDIO OUT est actif et l'audio à 192 kbit/s, le bit 16 et les bits 0-9 doivent changer.
        assert_eq!(audio_control_param(&with(|s| { s.audio_kbps = 192; s.audio_out = true; })), 0x2117_10C0);
    }

    #[test]
    fn audio_out_coupe() {
        // Quand AUDIO OUT est coupé, le bit 16 doit être à 0.
        assert_eq!(audio_control_param(&with(|s| { s.audio_kbps = 128; s.audio_out = false; })), TRACE_AUDIO_CONTROL_PARAM);
    }

    #[test]
    fn debit_hors_liste_refuse() {
        // Si un débit hors liste est demandé, alors la validation doit renvoyer une erreur.
        assert!(with(|s| s.video_kbps = 7000).validate().is_err());
        assert!(with(|s| s.keyframe_interval = 45).validate().is_err());
        assert!(with(|s| s.audio_kbps = 64).validate().is_err());
    }

    #[test]
    fn reglages_par_defaut_acceptes() {
        assert_eq!(Settings::default().validate(), Ok(Settings::default()));
    }

    #[test]
    fn fichier_de_reglages_aller_retour() {
        let s = with(|s| { s.video_kbps = 12000; s.audio_out = false; });
        assert_eq!(Settings::from_text(&s.to_text()), Ok(s));
        // Les clés absentes gardent leur valeur par défaut.
        assert_eq!(Settings::from_text("video_kbps=4000\n"), Ok(with(|s| s.video_kbps = 4000)));
        // Si le fichier contient une valeur invalide, alors il est refusé.
        assert!(Settings::from_text("video_kbps=7000\n").is_err());
        assert!(Settings::from_text("debit=4000\n").is_err());
    }

    #[test]
    fn anciens_reglages_d_image_ignores() {
        // Si le fichier vient d'une version avec luminosité et contraste (sans effet sur l'image encodée),
        // alors ces clés sont ignorées et ne sont plus écrites.
        let old = "video_kbps=12000\nbrightness=128\ncontrast=62\n";
        assert_eq!(Settings::from_text(old), Ok(with(|s| s.video_kbps = 12000)));
        assert!(!Settings::default().to_text().contains("brightness"));
        assert!(!Settings::default().to_text().contains("contrast"));
    }

    #[test]
    fn switch2_1080p60() {
        // Quand le micro renvoie 98 78 80 65 44 38 17, le signal doit être 1920×1080 à 60 Hz.
        let raw = [0x98, 0x78, 0x80, 0x65, 0x44, 0x38, 0x17];
        assert_eq!(parse_timing(&raw), Some(Signal { width: 1920, height: 1080, refresh_hz: 60 }));
    }

    #[test]
    fn compteur_nul_sans_signal() {
        // Si le compteur d'horloge est nul, alors aucun signal n'est décrit.
        assert_eq!(parse_timing(&[0x98, 0x78, 0x80, 0x65, 0x44, 0x38, 0x00]), None);
    }
}

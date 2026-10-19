//! `Hub` : reçoit le flux du boîtier, le réaligne en paquets TS et le distribue aux lecteurs
//! (décodeurs de l'aperçu et de la caméra, enregistrement, clients HTTP). Un nouveau lecteur reçoit PAT + PMT puis
//! le flux à partir de la prochaine image clé : il décode tout de suite, sans analyser ni rejouer d'images en retard.
//! Les clients réseau sont coupés à chaque redémarrage de l'encodeur (horodatages remis à zéro, sur lesquels la
//! Source média d'OBS se fige) : ils se reconnectent sur le nouveau flux.

use std::sync::mpsc::{sync_channel, Receiver, SyncSender, TrySendError};
use std::sync::{Arc, Mutex};

use crate::ts::TsAligner;

pub type Chunk = Arc<Vec<u8>>;

#[derive(Default)]
pub struct Hub {
    inner: Mutex<Inner>,
}

#[derive(Default)]
struct Inner {
    aligner: TsAligner,
    readers: Vec<Reader>,
}

struct Reader {
    tx: SyncSender<Chunk>,
    /// Faux tant que la première image clé n'est pas passée.
    live: bool,
    /// Coupé au prochain redémarrage de l'encodeur (clients réseau).
    session: bool,
}

impl Hub {
    pub fn feed(&self, data: &[u8]) {
        let mut inner = self.inner.lock().unwrap();
        let packets = inner.aligner.feed(data);
        if packets.is_empty() {
            return;
        }
        let from_keyframe = match inner.aligner.last_keyframe() {
            Some(k) if inner.aligner.has_header() && inner.readers.iter().any(|r| !r.live) => Some(Arc::new([inner.aligner.header(), packets[k..].to_vec()].concat())),
            _ => None,
        };
        let chunk = Arc::new(packets);
        // Un lecteur trop lent est débranché plutôt que de bloquer le boîtier : il se réabonnera.
        inner.readers.retain_mut(|r| {
            let Some(to_send) = (if r.live { Some(chunk.clone()) } else { from_keyframe.clone() }) else { return true };
            match r.tx.try_send(to_send) {
                Ok(()) => {
                    r.live = true;
                    true
                }
                Err(TrySendError::Full(_) | TrySendError::Disconnected(_)) => false,
            }
        });
    }

    /// Nouveau lecteur local (aperçu, caméra, enregistrement) : il recevra PAT + PMT puis le flux en direct dès la
    /// prochaine image clé, et le garde à travers les redémarrages de l'encodeur.
    pub fn subscribe(&self, capacity: usize) -> Receiver<Chunk> {
        self.add_reader(capacity, false)
    }

    /// Nouveau client réseau : comme `subscribe`, mais coupé au prochain redémarrage de l'encodeur.
    pub fn subscribe_session(&self, capacity: usize) -> Receiver<Chunk> {
        self.add_reader(capacity, true)
    }

    /// L'encodeur va (re)démarrer : coupe les clients réseau et oublie l'en-tête de l'ancien flux.
    pub fn new_session(&self) {
        let mut inner = self.inner.lock().unwrap();
        inner.aligner = TsAligner::default();
        inner.readers.retain(|r| !r.session);
    }

    fn add_reader(&self, capacity: usize, session: bool) -> Receiver<Chunk> {
        let (tx, rx) = sync_channel(capacity);
        self.inner.lock().unwrap().readers.push(Reader { tx, live: false, session });
        rx
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ts::tests::{packet, pat, pmt, video_start};
    use crate::ts::PACKET;

    fn received(rx: &Receiver<Chunk>) -> Vec<u8> {
        rx.try_iter().flat_map(|c| c.as_ref().clone()).collect()
    }

    #[test]
    fn un_nouveau_lecteur_demarre_sur_une_image_cle() {
        // Quand un lecteur arrive en cours de route, il doit recevoir PAT + PMT puis le flux à partir
        // de la prochaine image clé, rien de ce qui la précède.
        let hub = Hub::default();
        let (pat, pmt) = (pat(0x42), pmt(0x42, 0x44, 0x45));
        hub.feed(&[pat.clone(), pmt.clone(), video_start(0x44, &[9, 7, 8, 5])].concat());
        let rx = hub.subscribe(16);
        hub.feed(&[video_start(0x44, &[9, 1]), packet(0x45, 1)].concat());
        assert!(received(&rx).is_empty(), "rien avant l'image clé");
        let keyframe = video_start(0x44, &[9, 7, 8, 5]);
        hub.feed(&[video_start(0x44, &[9, 1]), keyframe.clone(), packet(0x45, 2)].concat());
        assert_eq!(received(&rx), [pat, pmt, keyframe, packet(0x45, 2)].concat());
        hub.feed(&packet(0x45, 3));
        assert_eq!(received(&rx).len(), PACKET, "ensuite tout le flux");
    }

    #[test]
    fn un_client_reseau_trop_en_retard_est_coupe() {
        // Si un client réseau ne suit plus et que sa file est pleine, alors il est coupé (il se reconnectera au
        // direct) au lieu d'accumuler du retard, et les autres lecteurs continuent.
        let hub = Hub::default();
        let (pat, pmt) = (pat(0x42), pmt(0x42, 0x44, 0x45));
        let slow = hub.subscribe_session(3);
        let local = hub.subscribe(64);
        hub.feed(&[pat, pmt, video_start(0x44, &[9, 7, 8, 5])].concat());
        for i in 0..3 {
            hub.feed(&packet(0x45, i));
        }
        assert_eq!(slow.try_iter().count(), 3, "ce qui était déjà en file est encore livré");
        assert_eq!(slow.try_recv().err(), Some(std::sync::mpsc::TryRecvError::Disconnected), "puis coupé");
        assert_eq!(received(&local).len(), 6 * PACKET, "les autres lecteurs reçoivent tout");
    }

    #[test]
    fn un_redemarrage_de_l_encodeur_coupe_les_clients_reseau() {
        // Quand l'encodeur redémarre, ses horodatages repartent de zéro et OBS se fige s'il garde la connexion :
        // les clients réseau doivent être coupés (ils se reconnectent), les lecteurs locaux continuent, et un
        // nouveau client démarre sur l'en-tête et l'image clé du nouveau flux.
        let hub = Hub::default();
        let (pat, pmt) = (pat(0x42), pmt(0x42, 0x44, 0x45));
        let keyframe = video_start(0x44, &[9, 7, 8, 5]);
        let local = hub.subscribe(16);
        let network = hub.subscribe_session(16);
        hub.feed(&[pat.clone(), pmt.clone(), keyframe.clone()].concat());
        received(&local);
        received(&network);
        hub.new_session();
        hub.feed(&[pat.clone(), pmt.clone(), keyframe.clone()].concat());
        assert_eq!(network.try_recv().err(), Some(std::sync::mpsc::TryRecvError::Disconnected), "client réseau coupé");
        assert_eq!(received(&local), [pat.clone(), pmt.clone(), keyframe.clone()].concat(), "lecteur local intact");
        // Un client qui arrive juste après le redémarrage n'hérite pas de l'en-tête de l'ancien flux.
        hub.new_session();
        let late = hub.subscribe_session(16);
        hub.feed(&[video_start(0x44, &[9, 1]), keyframe.clone()].concat());
        assert!(received(&late).is_empty(), "pas de PAT/PMT du nouveau flux encore : rien");
        hub.feed(&[pat.clone(), pmt.clone(), keyframe.clone()].concat());
        assert_eq!(received(&late), [pat, pmt, keyframe].concat());
    }
}

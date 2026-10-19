//! Flux SRT en mode live : le même MPEG-TS qu'en HTTP, pour OBS sur la tour (Source média en mode caller).
//! Contrairement au TCP, la latence reste fixe : un paquet plus en retard que `LATENCY_MS` est jeté au lieu de
//! s'accumuler (la Source média d'OBS ne rattrape jamais un retard). libsrt est appelée directement : chaque
//! appelant est un client réseau du hub (départ sur une image clé neuve, coupé à chaque redémarrage de l'encodeur).

use std::ffi::CStr;
use std::net::{Ipv4Addr, SocketAddrV4};
use std::os::raw::{c_char, c_int, c_void};
use std::sync::Arc;
use std::thread;
use std::time::Duration;

use crate::hub::Hub;

/// Charge utile d'un paquet SRT : 7 paquets TS (SRT_LIVE_DEF_PLSIZE).
pub const PAYLOAD: usize = 7 * 188;
/// Latence SRT (la plus grande des deux extrémités s'applique) : absorbe les pics de latence du Wi-Fi.
pub const LATENCY_MS: c_int = 300;
const BACKLOG_CHUNKS: usize = 300; // ~10 s, comme en HTTP

type SrtSocket = i32;
const SRT_INVALID_SOCK: SrtSocket = -1;
const SRT_ERROR: c_int = -1;
const SRTO_LATENCY: c_int = 23; // srt.h 1.5.3, enum SRT_SOCKOPT

// libsrt installée avec ffmpeg (sans paquet -dev, d'où le nom de fichier exact).
#[link(name = "libsrt-gnutls.so.1.5", kind = "dylib", modifiers = "+verbatim")]
unsafe extern "C" {
    fn srt_startup() -> c_int;
    fn srt_create_socket() -> SrtSocket;
    fn srt_setsockflag(u: SrtSocket, opt: c_int, optval: *const c_void, optlen: c_int) -> c_int;
    fn srt_bind(u: SrtSocket, name: *const libc::sockaddr, namelen: c_int) -> c_int;
    fn srt_listen(u: SrtSocket, backlog: c_int) -> c_int;
    fn srt_accept(u: SrtSocket, addr: *mut libc::sockaddr, addrlen: *mut c_int) -> SrtSocket;
    fn srt_sendmsg2(u: SrtSocket, buf: *const c_char, len: c_int, mctrl: *mut c_void) -> c_int;
    fn srt_close(u: SrtSocket) -> c_int;
    fn srt_getlasterror_str() -> *const c_char;
}

fn last_error() -> String {
    // SAFETY: libsrt renvoie une chaîne C statique (par fil d'exécution), jamais nulle.
    unsafe { CStr::from_ptr(srt_getlasterror_str()) }.to_string_lossy().into_owned()
}

/// Découpe un morceau du hub (paquets TS entiers) en messages SRT d'au plus 7 paquets TS.
pub fn messages(chunk: &[u8]) -> impl Iterator<Item = &[u8]> {
    chunk.chunks(PAYLOAD)
}

/// Écoute en SRT sur `bind` (UDP) : chaque appelant reçoit le flux en direct dans son propre fil.
pub fn serve(bind: SocketAddrV4, hub: Arc<Hub>, log: impl Fn(String) + Send + Sync + 'static) -> Result<(), String> {
    // SAFETY: appels libsrt avec des arguments valides ; `addr` vit pendant srt_bind.
    let listener = unsafe {
        if srt_startup() == SRT_ERROR {
            return Err(last_error());
        }
        let s = srt_create_socket();
        if s == SRT_INVALID_SOCK {
            return Err(last_error());
        }
        let addr = sockaddr(bind);
        let latency = LATENCY_MS;
        let size = |n: usize| n as c_int;
        if srt_setsockflag(s, SRTO_LATENCY, (&latency as *const c_int).cast(), size(size_of::<c_int>())) == SRT_ERROR
            || srt_bind(s, (&addr as *const libc::sockaddr_in).cast(), size(size_of::<libc::sockaddr_in>())) == SRT_ERROR
            || srt_listen(s, 4) == SRT_ERROR
        {
            let e = last_error();
            srt_close(s);
            return Err(e);
        }
        s
    };
    let log = Arc::new(log);
    thread::Builder::new().name("srt".into()).spawn(move || loop {
        let mut peer: libc::sockaddr_in = unsafe { std::mem::zeroed() };
        let mut len = size_of::<libc::sockaddr_in>() as c_int;
        // SAFETY: `peer` et `len` décrivent un tampon valide de la taille annoncée.
        let caller = unsafe { srt_accept(listener, (&mut peer as *mut libc::sockaddr_in).cast(), &mut len) };
        if caller == SRT_INVALID_SOCK {
            log(format!("SRT : attente d'appelant impossible ({})", last_error()));
            thread::sleep(Duration::from_secs(1));
            continue;
        }
        let name = format!("{}:{}", Ipv4Addr::from(u32::from_be(peer.sin_addr.s_addr)), u16::from_be(peer.sin_port));
        let (hub, log) = (hub.clone(), log.clone());
        thread::spawn(move || {
            log(format!("lecteur SRT {name} : connecté"));
            let end = send_live(caller, &hub);
            // SAFETY: socket accepté plus haut, fermé une seule fois.
            unsafe { srt_close(caller) };
            log(format!("lecteur SRT {name} : déconnecté{}", end.err().map(|e| format!(" ({e})")).unwrap_or_default()));
        });
    }).map_err(|e| e.to_string())?;
    Ok(())
}

/// Envoie le direct jusqu'à la coupure par le hub (redémarrage de l'encodeur, retard excessif) ou la perte de l'appelant.
fn send_live(caller: SrtSocket, hub: &Hub) -> Result<(), String> {
    for chunk in hub.subscribe_session(BACKLOG_CHUNKS) {
        for msg in messages(&chunk) {
            // SAFETY: `msg` est un tranche valide d'au plus PAYLOAD octets ; pas de contrôle de message.
            if unsafe { srt_sendmsg2(caller, msg.as_ptr().cast(), msg.len() as c_int, std::ptr::null_mut()) } == SRT_ERROR {
                return Err(last_error());
            }
        }
    }
    Ok(())
}

fn sockaddr(bind: SocketAddrV4) -> libc::sockaddr_in {
    // SAFETY: sockaddr_in est une structure C simple, valide une fois remplie de zéros.
    let mut addr: libc::sockaddr_in = unsafe { std::mem::zeroed() };
    addr.sin_family = libc::AF_INET as libc::sa_family_t;
    addr.sin_port = bind.port().to_be();
    addr.sin_addr.s_addr = u32::from(*bind.ip()).to_be();
    addr
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ts::PACKET;

    #[test]
    fn decoupe_en_messages_de_sept_paquets_ts() {
        // Quand un morceau contient 15 paquets TS, il doit partir en messages de 7, 7 et 1 paquets, sans rien perdre.
        let chunk: Vec<u8> = (0..15 * PACKET).map(|i| (i % 251) as u8).collect();
        let sizes: Vec<usize> = messages(&chunk).map(<[u8]>::len).collect();
        assert_eq!(sizes, [7 * PACKET, 7 * PACKET, PACKET]);
        assert_eq!(messages(&chunk).flatten().copied().collect::<Vec<u8>>(), chunk);
    }
}

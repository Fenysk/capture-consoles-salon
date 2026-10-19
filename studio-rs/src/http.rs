//! Flux réseau : `GET /stream.ts` renvoie le MPEG-TS en direct (OBS « Source média », VLC, ffplay,
//! et plus tard l'app web de la tour avec mpegts.js — d'où l'en-tête CORS).
//! Un client ne peut pas accumuler plus de retard que sa file (`BACKLOG_CHUNKS`, ~1 morceau par image) et le tampon
//! d'envoi du noyau (`SEND_BUFFER`) ; au-delà, le hub le coupe et il se reconnecte au direct (garde-fou contre
//! l'emballement ; la dérive normale d'OBS se règle côté lecteur). Un lecteur qui ne lit plus (Source média hors antenne) est coupé au bout de
//! `STALLED_CLIENT`, sinon il reprendrait plus tard un flux vieux de plusieurs minutes.

const BACKLOG_CHUNKS: usize = 300; // ~10 s : OBS prend ~1,5 s de retard à chaque connexion, une limite serrée le recoupe en boucle
const SEND_BUFFER: libc::c_int = 256 * 1024; // le noyau double la valeur : ~0,5 s à 8 Mbit/s
const STALLED_CLIENT: Duration = Duration::from_secs(10);

use std::io::{BufRead, BufReader, Write};
use std::net::{TcpListener, TcpStream};
use std::os::fd::AsRawFd;
use std::sync::Arc;
use std::thread;
use std::time::Duration;

use crate::hub::Hub;

pub fn serve(listen: &str, hub: Arc<Hub>, log: impl Fn(String) + Send + Sync + 'static) -> std::io::Result<()> {
    let listener = TcpListener::bind(listen)?;
    let log = Arc::new(log);
    thread::Builder::new().name("http".into()).spawn(move || {
        for stream in listener.incoming().flatten() {
            let (hub, log) = (hub.clone(), log.clone());
            thread::spawn(move || {
                let peer = stream.peer_addr().map(|a| a.to_string()).unwrap_or_default();
                match handle(stream, &hub) {
                    Ok(true) => log(format!("lecteur réseau {peer} : déconnecté")),
                    Ok(false) => {}
                    Err(e) => log(format!("lecteur réseau {peer} : {e}")),
                }
            });
        }
    })?;
    Ok(())
}

/// Renvoie `Ok(true)` si un flux a été servi.
fn handle(mut stream: TcpStream, hub: &Hub) -> std::io::Result<bool> {
    let mut reader = BufReader::new(stream.try_clone()?);
    let mut request = String::new();
    reader.read_line(&mut request)?;
    loop {
        let mut line = String::new();
        if reader.read_line(&mut line)? == 0 || line == "\r\n" || line == "\n" {
            break;
        }
    }
    let path = request.split_whitespace().nth(1).unwrap_or("");
    if !request.starts_with("GET ") || path.split('?').next() != Some("/stream.ts") {
        stream.write_all(b"HTTP/1.1 404 Not Found\r\nContent-Type: text/plain; charset=utf-8\r\nConnection: close\r\n\r\nFlux : /stream.ts\n")?;
        return Ok(false);
    }
    stream.write_all(
        b"HTTP/1.1 200 OK\r\nContent-Type: video/mp2t\r\nCache-Control: no-cache\r\n\
          Access-Control-Allow-Origin: *\r\nConnection: close\r\n\r\n",
    )?;
    stream.set_nodelay(true)?;
    stream.set_write_timeout(Some(STALLED_CLIENT))?;
    limit_send_buffer(&stream)?;
    for chunk in hub.subscribe_session(BACKLOG_CHUNKS) {
        if stream.write_all(&chunk).is_err() {
            break;
        }
    }
    Ok(true)
}

/// SO_SNDBUF fixe : sans lui, le noyau laisse grossir la file d'envoi jusqu'à 4 Mo (~4 s de retard).
fn limit_send_buffer(stream: &TcpStream) -> std::io::Result<()> {
    let size = SEND_BUFFER;
    // SAFETY: descripteur valide (emprunté à `stream`), option entière de la taille annoncée.
    let ok = unsafe {
        libc::setsockopt(
            stream.as_raw_fd(),
            libc::SOL_SOCKET,
            libc::SO_SNDBUF,
            (&size as *const libc::c_int).cast(),
            std::mem::size_of::<libc::c_int>() as libc::socklen_t,
        )
    };
    if ok == 0 { Ok(()) } else { Err(std::io::Error::last_os_error()) }
}

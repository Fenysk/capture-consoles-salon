//! Flux réseau : `GET /stream.ts` renvoie le MPEG-TS en direct (OBS « Source média », VLC, ffplay,
//! et plus tard l'app web de la tour avec mpegts.js — d'où l'en-tête CORS).
//! Un lecteur qui ne lit plus (OBS met en pause une Source média hors antenne) est coupé au bout de
//! `STALLED_CLIENT` : sinon sa connexion resterait ouverte indéfiniment.

const STALLED_CLIENT: Duration = Duration::from_secs(10);

use std::io::{BufRead, BufReader, Write};
use std::net::{TcpListener, TcpStream};
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
    for chunk in hub.subscribe_session(1024) {
        if stream.write_all(&chunk).is_err() {
            break;
        }
    }
    Ok(true)
}

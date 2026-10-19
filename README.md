# Capture des consoles du salon (LGP Lite sous Linux)

Jouer aux consoles depuis le canapé du salon et streamer depuis la tour PC de la chambre, sans déplacer les consoles.

```
Console (Switch 2…) ──HDMI──▶ AVerMedia LGP Lite (GL310) ──USB──▶ PC portable Linux ──réseau──▶ Tour Windows (OBS, Twitch)
                                     └──HDMI OUT──▶ TV du salon (image)    └──AUDIO OUT (jack)──▶ son pour jouer
```

## Où on en est (19/10/2026)

| Étape | État |
|-------|------|
| 1. Capter l'image et le son du LGP Lite sous Linux | ✅ H.264 1080p30 + AAC 48 kHz, via un pilote maison en espace utilisateur |
| Son sur la TV par la sortie HDMI du boîtier | ❌ Muet dans tous les cas (même sans PC, même avec le pilote Windows) : on utilise la prise **AUDIO OUT** ou un casque Bluetooth sur la Switch |
| GL310 Studio natif en Rust (aperçu, enregistrement, réglages, état) | ✅ `studio-rs/`, pilote USB intégré, sans module noyau |
| OBS sous Linux voit le boîtier | ✅ caméra virtuelle PipeWire « GL310 » + son du jeu en entrée audio « GL310-Audio » (testés sans OBS) |
| 2. Envoyer le flux vers OBS sur la tour | ✅ Utilisé en live le 19/10 : Source média « Console de jeu » (scène « Master - AverMedia ») sur `http://IP-DU-PORTABLE:4848/stream.ts`, reconnexion 1 s, décodage matériel |
| Mode console (`obs/mode-console.lua`) | ⏳ Écrit et testé sans OBS ; à installer et tester sur la tour, hors live |
| Lecture du flux par l'app web `stream_twitch` | ⏸ Plus tard (mpegts.js, CORS déjà ouvert) |
| Installation (menu Démarrer, icône) | ✅ `./install.sh` |
| Pilote noyau C (`driver/gl310`) | ⏸ Écrit et compilé, jamais chargé (Secure Boot : clé MOK non enregistrée), mis de côté |

Résultat : une capture d'une minute de Mario Kart World (1 868 images, son OK), gardée sur le portable (les images et
vidéos ne sont pas publiées).

## GL310 Studio (le plus simple)

**Installation** (une fois, puis après chaque modification du code) — sans sudo. Les firmwares du boîtier et les
sources tierces ne sont pas publiés dans ce dépôt (licences d'AVerMedia et des auteurs) : `tools/fetch-vendor.sh` les
télécharge une fois dans `vendor/` (pilote Windows du GL310 sur archive.org, md5 vérifié ; trace du C875 ; pilote C985).

```bash
tools/fetch-vendor.sh
./install.sh
```

Il compile l'appli, la copie dans `~/.local/bin`, copie les firmwares dans `~/.local/share/gl310-studio/` et ajoute
**GL310 Studio** (avec son icône) au menu Démarrer, rubrique Son et vidéo. `./install.sh --remove` désinstalle.
Il n'y a pas de pilote séparé : le pilote USB est dans l'appli (seule la règle udev de `tools/setup-phase1.sh` est
nécessaire, déjà en place sur ce PC).

**Utilisation** : menu Démarrer > GL310 Studio. Dès que le boîtier est branché, l'appli charge le firmware si besoin
(≈ 10 s) et lance l'encodeur toute seule ; elle relance l'encodeur quand la console se réveille.

- **aperçu en direct** au centre, **état du boîtier** en haut (branché, démarrage, signal HDMI, débit reçu) ;
- **Enregistrer** : écrit `~/Vidéos/GL310/gl310-AAAAMMJJ-HHMMSS.ts`, converti automatiquement en `.mp4` à l'arrêt ;
- **réglages** : débit vidéo (4 à 30 Mbit/s), image clé toutes les 1 ou 2 s, débit audio, son sur la prise AUDIO OUT
  (« Appliquer » relance l'encodeur, 1 à 2 s de coupure). Pas de luminosité ni de contraste : le boîtier ne les applique
  pas à l'image envoyée au PC ; dans OBS, filtre « Correction colorimétrique » ;
- **sorties** :
  - caméra virtuelle PipeWire **GL310** → OBS sous Linux : « Capture de périphérique vidéo (PipeWire) » ;
  - son du jeu en entrée audio **GL310-Audio** (créée au lancement, retirée à la fermeture) → OBS : « Capture d'entrée audio » ;
  - flux MPEG-TS sur le réseau local, adresse affichée dans la fenêtre (de la forme `http://IP-DU-PORTABLE:4848/stream.ts`).

**Dans OBS sur la tour** : Sources > + > **Source média** → décocher « Fichier local » → Entrée :
`http://IP-DU-PORTABLE:4848/stream.ts` → Format d'entrée : `mpegts` → « Mise en mémoire tampon réseau » : **0 Mo** →
« Options FFmpeg » : **`probesize=500000 analyzeduration=500000`** (surtout pas `fflags=nobuffer`, qui ajoute 2 s) →
cocher le décodage matériel → « Délai de reconnexion » : **1 s** (à chaque redémarrage de l'encodeur — console qui se réveille, réglage appliqué, boîtier rebranché — l'appli coupe les connexions réseau : OBS se reconnecte sur le nouveau flux au lieu de garder des horodatages remis à zéro). Sans ces options, OBS analyse ~4 s de flux avant d'afficher et garde ce retard.
L'appli fait démarrer chaque lecteur sur une image clé : à la connexion, l'image arrive en 0 à 2 s (intervalle des
images clés), puis le direct suit sans retard ajouté. Il faut que GL310 Studio tourne sur le portable et que la console
soit allumée. `127.0.0.1` ne marche que sur le portable lui-même. Si ça saccade : mémoire tampon à 0,5–1 Mo.

**Wi-Fi** : l'économie d'énergie du Wi-Fi faisait saccader le flux (la carte s'endort entre deux paquets). `install.sh`
installe `gl310-wifi-powersave`, lancé à l'ouverture de session : il la **coupe sur secteur** et la **remet sur
batterie** (chaque bascule reconnecte le Wi-Fi 2 à 3 s ; trace : `journalctl -t gl310-wifi`). Si l'adresse du portable change (Wi-Fi), la fenêtre affiche la
nouvelle ; on peut la figer dans la box internet (bail DHCP fixe).

Réglages mémorisés dans `~/.config/gl310-studio/settings.txt`. Tests : `./gate.sh` (voir plus bas).
Besoins : ffmpeg, GStreamer (`gst-launch-1.0`, greffon pipewire), PipeWire (`pactl`, `pw-cat`, `pw-link`) — déjà
présents sur Linux Mint 22. Pour n'écouter que sur ce PC : `gl310-studio --listen 127.0.0.1:4848`.

L'ancien prototype web (`python3 -m studio`, page http://127.0.0.1:4848) reste disponible ; ne pas lancer les deux à la fois
(un seul programme peut parler au boîtier).

## Utilisation en ligne de commande

Une seule fois, pour installer les outils et l'accès USB sans root :

```bash
bash tools/setup-phase1.sh
```

À chaque branchement du boîtier (LGP Lite branché en USB, console allumée) :

```bash
python3 tools/gl310_boot.py && python3 tools/gl310_encode.py --observe 2 && python3 tools/gl310_capture.py --frames 100000 --timeout 60 --out captures/test.ts
```

Puis, pour regarder : `ffmpeg -i captures/test.ts -c copy captures/test.mp4` et ouvrir le MP4 (Celluloid).

- Si quelque chose bloque : **débrancher le boîtier 10 s et le rebrancher**, tout ce qu'on lui envoie est en RAM.
- Ne jamais écrire dans les registres 0x47 / 0x49 du microcontrôleur (profil enregistré de façon permanente) : les outils le refusent.

## Développement

`./gate.sh` est la seule preuve qu'un changement est fini : il doit finir en 0 avant chaque commit (le crochet
`tools/git-hooks/pre-commit` le lance ; à activer une fois avec `git config core.hooksPath tools/git-hooks`). Il vérifie
qu'aucune info perso n'entre dans le dépôt public (`tools/check-public.sh`), puis lance les tests Rust et clippy, les
tests du mode console, du prototype Python, de l'outil de mesure du délai et du modèle du pilote C. Décisions :
[JOURNAL.md](JOURNAL.md), en tête, avant le code.

## Contenu du dossier

| Chemin | Rôle |
|--------|------|
| [NOTES.md](NOTES.md) | Journal complet de l'enquête : protocole USB, firmware, boîte aux lettres, tests du son |
| [JOURNAL.md](JOURNAL.md) | Décisions de conception (et options écartées) |
| `studio-rs/` | **GL310 Studio en Rust** : `device.rs` (pilote USB), `engine.rs` (fil USB), `outputs.rs` (aperçu, caméra et micro PipeWire), `http.rs`, `recorder.rs`, `app.rs` (fenêtre egui), `model.rs`/`ts.rs`/`picture.rs` (testés), `tables.rs` (généré) |
| `studio/` | Prototype web de GL310 Studio en Python (`model.py` testé, `device.py`, `server.py`, `web/`) |
| `driver/gl310/` | Pilote noyau C (V4L2, MPEG-TS comme hdpvr) : compilé, jamais chargé, mis de côté (voir JOURNAL.md) |
| `obs/` | **Mode console** pour OBS sur la tour : `mode-console.lua` (raccourci qui cale webcam, micro, bruitages… sur le jeu en retard), notice `obs/README.md`, tests `obs/run-tests.sh` |
| `tools/gen_gl310_tables.py` | Génère les tables de commandes (Rust et C) à partir de la trace Windows |
| `tools/latency_probe.py` | Mesure le délai propre du boîtier : la sortie HDMI du portable affiche l'heure (en clair et en code-barres) sur l'entrée du boîtier ; sert aussi la page d'heure `http://<portable>:4849/` pour mesurer le délai dans OBS (tests : `tools/test_latency_probe.py`) |
| `tools/gl310_pcm_probe.py` | Essai (pas encore lancé) : son PCM brut par une seconde tâche du firmware, comme le C985 |
| `recordings/` | Enregistrements faits depuis GL310 Studio (non publiés) |
| `tools/gl310_boot.py` | Initialisation officielle (206 écritures I2C), chargement des firmwares en RAM, démarrage de l'ARM |
| `tools/gl310_encode.py` | Démarrage de l'encodeur (boîte aux lettres, réglages du micro, passage audio vers AUDIO OUT) |
| `tools/gl310_capture.py` | Récupère le flux MPEG-TS (H.264 + AAC) depuis la mémoire du boîtier |
| `tools/gl310.py`, `gl310_i2cscan.py`, `gl310_memcheck.py` | Petits outils de diagnostic (LED, registres, bus I2C, mémoire) |
| `tools/usbpcap_decode.py` | Décode une capture USB (pcap) en commandes lisibles |
| `vm/` | VM Windows 11 d'évaluation pour observer le pilote officiel (`start-vm.sh --usb`, `vmctl.py`) |
| `vendor/` | Sources tierces : pilote Windows GL310, projet C875 (trace), pilote Linux C985 (non publiées : `tools/fetch-vendor.sh`) |
| `logs/` | Journaux des essais et capture USB du pilote Windows (non publiés) |
| `captures/` | Images et vidéos capturées (non publiées) |

## Références

- App web du stream (tour Windows) : https://github.com/Fenysk/stream_twitch

- Pilote Linux du C985 (même famille de firmware) : https://github.com/FireCulex/avermedia-c985-linux
- Projet abandonné pour le LGP C875 (trace USB Windows) : https://github.com/Trouffman/octv_gears_lgp
- Pilote Windows du GL310 (copie archive.org) : https://archive.org/details/aver-media-gl-310-usb-hd-capture-device-v-3.2802.64.40

## Licence

MIT ([LICENSE](LICENSE)), sauf `studio/web/mpegts.js`, copie de [mpegts.js](https://github.com/xqq/mpegts.js) sous
licence Apache 2.0. Les firmwares et pilotes d'AVerMedia ne sont pas dans ce dépôt (`tools/fetch-vendor.sh`).

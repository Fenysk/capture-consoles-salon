# Journal des décisions

## 2026-10-19 — Les clients réseau sont coupés à chaque redémarrage de l'encodeur
**Contexte.** Boîtier rebranché pendant un live : l'encodeur repart avec des horodatages remis à zéro, alors que la connexion d'OBS restait ouverte. Fenysk a vu le jeu disparaître d'OBS. Deux relances de l'appli juste avant, faites en plein live sans le savoir, l'avaient déjà coupé. mpegts.js (app web) gère mal un flux dont les horodatages reculent.
**Décision.** Le hub distingue lecteurs locaux (aperçu, caméra, enregistrement : gardés) et clients réseau (`subscribe_session` : coupés par `new_session()` avant chaque démarrage de l'encodeur, et l'en-tête PAT/PMT de l'ancien flux est oublié). OBS se reconnecte : « Délai de reconnexion » à 1 s. Ne plus relancer l'appli sans demander à la tour si Fenysk est en live.
**Renoncement.** Réécrire PTS/DTS/PCR pour un flux continu (plus fragile pour un gain d'environ 1 s, et seulement lors de ces rares redémarrages) ; couper aussi les lecteurs locaux (l'enregistrement s'arrêterait).

## 2026-10-19 — Délai du boîtier mesuré avec la sortie HDMI du portable ; luminosité et contraste retirés
**Contexte.** La première mesure du délai du boîtier (~1,6 s) reposait sur l'horloge de la Switch, qui peut être décalée. Les curseurs luminosité/contraste (micro 0x43/0x44) ne changeaient rien à l'image encodée.
**Décision.** `tools/latency_probe.py` : la sortie HDMI du portable remplace la console sur l'entrée du boîtier et affiche l'heure du portable en code-barres ; chaque image reçue donne un délai exact, sur la même horloge. La même heure est servie en page web (port 4849) pour lire, dans OBS sur la tour, l'écart entre le jeu et une source sans retard. Luminosité et contraste sont retirés de GL310 Studio (le démarrage garde les valeurs de la trace) ; dans OBS, filtre « Correction colorimétrique ».
**Renoncement.** Micro du portable face aux enceintes (enregistre la pièce, demande quelqu'un devant la console) ; webcam ou téléphone filmant la télé et l'écran (manuel, peu de mesures) ; rétro-ingénierie du vrai réglage d'image dans la VM Windows (long, pour un réglage qu'OBS fait déjà).

## 2026-10-19 — Mode console : un raccourci OBS et une scène-liste « Synchro jeu »
**Contexte.** Le jeu arrive dans OBS ~2 s après tout le reste (webcam, micro, bruitages, Jean-Renard…). Fenysk veut un seul raccourci qui bascule, robuste aux nouvelles caméras et aux renommages, ultra simple à configurer.
**Décision.** Script Lua `obs/mode-console.lua` (sans extension) : la scène « Synchro jeu » sert de liste ; chaque source qu'elle contient est retardée elle-même (asynchrone : « Délai vidéo (asynchrone) », qui retarde aussi son son ; synchrone : « Délai de rendu » + décalage du son ; son seul : décalage), repérée par son identifiant ; le jeu (…/stream.ts) n'est jamais retardé ; un seul réglage, le délai. Déclencheur : raccourci, Stream Deck ou obs-websocket `TriggerHotkeyByName`.
**Renoncement.** Cases à cocher par nom de source (cassent au renommage) ; délai posé sur une scène (obligerait à refaire les mises en page) ; bascule automatique par scène (Fenysk veut un toggle) ; extensions Advanced Scene Switcher / Source Clone ; logique dans l'app web (possible plus tard par obs-websocket, sur la même convention).

## 2026-10-19 — Chaque lecteur démarre sur une image clé ; OBS : probesize/analyzeduration réduits
**Contexte.** OBS sur la tour avait ~4 s de retard même avec 0 Mo de mémoire tampon : ffmpeg (Source média) analyse ~4 s de flux avant d'afficher, puis rejoue tout en temps réel (mesuré : 5,7 s avant la première image par défaut, 7,2 s avec `fflags=nobuffer`).
**Décision.** Le hub n'envoie à un nouveau lecteur que PAT + PMT puis le flux à partir de la prochaine image clé (début de PES vidéo contenant un SPS : le boîtier ne pose pas l'indicateur RAI). Réglage OBS conseillé : `probesize=500000 analyzeduration=500000`, mémoire tampon 0 Mo.
**Renoncement.** SRT (pas nécessaire tant que le Wi-Fi tient) ; images clés toutes les 1 s par défaut (moins de qualité au même débit, ne gagne qu'au démarrage).

## 2026-10-19 — Économie d'énergie du Wi-Fi coupée sur secteur, remise sur batterie
**Contexte.** Le flux HTTP vers OBS sur la tour saccadait : Wi-Fi 5 GHz correct (signal 77 %), mais `wifi.powersave = 3` faisait monter les allers-retours vers la tour (jusqu'à 75 ms au lieu de 1 à 2 ms) et OBS se reconnectait. Fenysk veut la coupure sur secteur et le retour de l'économie sur batterie.
**Décision.** Script `tools/gl310-wifi-powersave.sh` lancé à l'ouverture de session (autostart posé par install.sh) : toutes les 5 s, il lit l'alimentation (`/sys/class/power_supply`) et règle la connexion Wi-Fi active par `nmcli` (2 = coupée sur secteur, 0 = valeur par défaut sur batterie), puis la reconnecte.
**Renoncement.** Règle udev + script root ou TLP (sudo, et Fenysk pilote le portable à distance) ; service systemd utilisateur (hors session graphique, polkit refuserait nmcli) ; réglage à chaud sans reconnexion (NetworkManager ne sait pas réappliquer `powersave`).

## 2026-10-19 — Flux ouvert au réseau local par défaut, installation par l'utilisateur sans sudo
**Contexte.** OBS sur la tour ne voyait rien avec `http://127.0.0.1:4848/stream.ts` (adresse de la tour elle-même, et l'appli n'écoutait que le portable). Fenysk veut aussi lancer l'appli depuis le menu comme les autres.
**Décision.** L'appli écoute sur `0.0.0.0:4848` et affiche l'adresse réseau réelle ; `./install.sh` (sans sudo) installe binaire, firmwares, icône et entrée de menu dans `~/.local` ; les enregistrements vont dans `~/Vidéos/GL310`.
**Renoncement.** Mot de passe sur le flux (réseau domestique, simplicité) ; paquet .deb (inutile pour un seul PC).

## 2026-10-19 — GL310 Studio en Rust avec son propre pilote USB ; le pilote noyau C est mis de côté
**Contexte.** Le module noyau compile, mais le Secure Boot exige d'enregistrer une clé MOK. Fenysk veut finalement du « simple et efficace », en Rust, sans viser le grand public. OBS sous Linux doit voir le boîtier, et l'app web de la tour doit pouvoir le recevoir plus tard.
**Décision.** Une seule appli Rust `studio-rs/` : pilote USB en espace utilisateur (rusb, portage de tools/*.py, tables générées depuis la trace), aperçu natif egui (décodage par ffmpeg), enregistrement, réglages, état. Le flux MPEG-TS est servi en HTTP (`/stream.ts`, CORS ouvert) : OBS (source média) en local, l'app web de la tour (mpegts.js) plus tard.
OBS voit aussi le boîtier comme une **caméra virtuelle PipeWire** « GL310 » (ffmpeg décode, `gst-launch-1.0 … pipewiresink` publie un nœud Video/Source rôle Camera) et le **son du jeu** comme une entrée audio « GL310-Audio » (module `pactl` Audio/Source/Virtual temporaire, alimenté par `pw-cat` relié avec `pw-link`).
**Renoncement.** Pilote noyau C (gardé dans driver/gl310, compilé, jamais chargé : Secure Boot + MOK, risque de plantage du PC) ; webcam v4l2loopback (module noyau, donc clé MOK) ; liaison Rust directe à libpipewire (paquets -dev + sudo, pour le même résultat) ; pipewiresink pour le son (trous de 64 ms : le nœud cadence le graphe sans réserve) ; diffusion grand public.

## 2026-10-19 — Un vrai pilote noyau Linux pour le GL310, et GL310 Studio en natif
**Contexte.** Les outils Python et GL310 Studio (web) fonctionnent, mais Fenysk veut un logiciel natif (Rust si possible)
et un pilote pour que le boîtier soit vu comme une carte de capture normale (/dev/video + ALSA).
**Décision.** Module noyau en C (comme avermedia-c985-linux), empaqueté avec DKMS. Vidéo exposée en MPEG-TS compressé
(modèle : pilote hdpvr), car l'USB 2.0 ne peut pas transporter de vidéo 1080p brute. Audio PCM via ALSA à étudier (piste « PCM Out »).
**Renoncement.** Service en espace utilisateur + v4l2loopback/PipeWire (recommandé pour sa sécurité, mais écarté par Fenysk) ;
GL310 Studio en page web (gardé comme prototype de référence).

## 2026-10-19 — GL310 Studio : une page web locale pour piloter le boîtier
**Contexte.** Fenysk veut un petit programme pour voir le flux, enregistrer, régler l'image et le son, et voir l'état du boîtier.
Un essai a montré qu'on peut arrêter l'encodeur (0x02 + 0xF3) puis le relancer avec d'autres réglages sans rebrancher (30 → 9 Mbit/s).
**Décision.** Dossier `studio/` : `model.py` (types purs : réglages, état, encodage des registres), `device.py` (`Gl310Device`, un seul
fil d'exécution possède l'USB et réutilise tools/gl310_*), `server.py` (HTTP stdlib, port 4848, flux TS en direct, API JSON), `web/` (page + mpegts.js local).
Réglages : débit vidéo, intervalle d'images clés, débit audio, sortie AUDIO OUT (relance de l'encodeur), luminosité/contraste (micro, à chaud).
**Renoncement.** Pas d'appli de bureau GTK/Qt (dépendances lourdes, inaccessible depuis la tour) ; pas de réglage de volume :
le sens du registre 0x41 du micro n'est pas prouvé ; pas de changement de résolution (champs d'entrée non compris, 1080p conservé).

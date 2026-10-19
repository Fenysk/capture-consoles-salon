# LGP Lite (GL310) sous Linux — notes de recherche

Objectif étape 1 : capter l'audio et la vidéo HDMI du LGP Lite sur le portable Linux
(HP, Linux Mint 22, noyau 6.8, i7 7e gén + GTX 1050 Mobile, 16 Go).
Étape 2 (plus tard) : envoyer le flux à la tour qui streame via OBS.

## Le boîtier

- USB `07ca:c835` « Aver_C835_USB », USB 2.0, classe 0xFF (propriétaire, pas UVC).
- Aucun pilote dans le noyau Linux (linux-hardware.org, jusqu'à 6.18).
- Le pilote Windows 3.2802.64.40 (2020) déclare `PID_c835` **et** `PID_d835` :
  le boîtier change probablement d'identifiant une fois son firmware chargé.
- Puces (teardown iFixit) : encodeur H.264 Sigma Designs PL330B, récepteur HDMI ITE IT6604,
  émetteur HDMI ITE IT6613 (passthrough), 512 Mo de RAM.
- Sortie déjà compressée en H.264, 1080p30 max. Pas de HDCP (à couper sur PS4/PS5).

## Interface USB (relevée sur ce PC)

Une seule interface, 4 endpoints bulk de 512 octets. **Identique au LGP classique C875 (`07ca:0875`).**

| Endpoint | Sens | Rôle (d'après le projet C875) |
|----------|------|-------------------------------|
| 0x04 | OUT | commandes (registres, I2C, LED…) |
| 0x83 | IN  | réponses aux commandes |
| 0x02 | OUT | envoi des firmwares, contrôle vidéo |
| 0x81 | IN  | données vidéo |

## Projet 1 : octv_gears_lgp (C875, 2014-2015, abandonné)

Dossier `vendor/octv_gears_lgp`. Code C + libusb, ne compile pas tel quel (bugs de syntaxe).

- `firmware/qpvidfwusb.bin` (vidéo, ARM) et `firmware/qpaudfw.bin` (audio), extraits du pilote C875.
- `capture_sequence` : trace USB Windows complète de l'initialisation (762 lignes) :
  écritures de registres, configuration I2C des puces HDMI, envoi des deux firmwares par blocs de 32 Ko sur 0x02.
- Format des commandes observé sur 0x04 :
  - `01 01 01 00 <adresse 32 bits LE> <valeur 32 bits LE>` : écriture d'un registre
  - `01 00 01 00 <adresse 32 bits LE>` : lecture d'un registre
  - `05 01 02 00 <périphérique I2C> 00 00 00 <registre> <valeur>` : écriture I2C
  - `0b 01 02 00 15 00 00 00 2c XX` : commande la LED
- Le projet avait réussi : LED, envoi des firmwares, attente du signal « vidéo prête ».
  Il n'avait pas réussi : un flux vidéo fiable.

## Projet 2 : avermedia-c985-linux (C985 PCIe, août-oct. 2026, fonctionne)

Dossier `vendor/avermedia-c985-linux`. Pilote noyau V4L2 + ALSA, vidéo et audio fonctionnels.

- Même système de firmware : « QPSOS ».
- **Vérifié** : le firmware vidéo du C875 porte la signature `QSOS` à l'offset 0x100,
  exactement ce que contrôle `c985_fw.c`, avec la version 2 à 0x106.
- Le transport diffère (PCIe + mailbox contre USB bulk), mais la logique du firmware
  (commandes, démarrage du flux, audio) est probablement très proche : une référence précieuse.

## Phase 0 : pilote Windows du GL310 (fait le 2026-10-19)

Source : copie archive.org du pilote 3.2802.64.40 (le site AVerMedia ne le propose plus).
Dossier `vendor/gl310-driver/` (zip md5 `6aa363d7d7458dd08f021277353d6f2f`), fichiers utiles dans `extracted/`.

- `AVer835_x64.inf` : `DriverVer=06/11/2020,3.2802.64.40`, déclare `USB\VID_07ca&PID_c835` et `PID_d835`.
  Les firmwares sont envoyés par le pilote (`DownloadFW=1`, `UseExternalFW=1`).
- `qpaudfwusb.bin` (363 832 o) : **identique octet pour octet** au firmware audio envoyé dans la
  trace `capture_sequence` du projet C875 (lignes 253 à 297, offsets 0x0 à 0x58000).
  Le `qpaudfw.bin` livré dans ce projet est une version plus ancienne (07/2013 contre 08/2013).
- `qpvidfwusb.bin` (454 064 o) : même firmware ARM QPSOS v2 (`QSOS` à 0x100, version 2, tag `04152013`)
  que celui du C875. Les chaînes ne diffèrent que par un tag de date (`09132013AUDI` contre `07262013AUDI`).
  La trace en contient une troisième compilation (454 048 o) : même famille, build différent.
- `AVer330USB.sys` : le pilote parle au firmware par messages via une boîte aux lettres :
  `QPFWAPI_SendMessageToARM`, `QPFWAPI_MailboxReady`, `QPFWAPI_AckARMMessage`, plus les familles
  `QPFWENCAPI_*` (encodeur), `QPFWCODECAPI_*` et `QPFWDECAPI_*`. C'est le même principe que `c985_mbox.c`.
  Les écritures de registres `01 01 01 00 …` sur l'endpoint 0x04 servent probablement à piloter cette boîte aux lettres par USB.
- Audio configurable : LPCM, AAC, G.711 (`EncAudioControlExLPCM`/`AAC`/`G711`).
- Récepteur HDMI piloté comme un ITE IT6603 (`CIT6603_*`, détection HDCP via `IsHDCPContent`).

Conclusion : la trace d'initialisation du C875 a été enregistrée avec le firmware audio exact du GL310.
C'est un très bon point de départ pour un pilote en espace utilisateur.

## Phase 1 : premier contact (fait le 2026-10-19)

- `tools/setup-phase1.sh` : installe `python3-usb`, `libusb-1.0-0-dev`, `ffmpeg`, `v4l-utils` et la règle
  `/etc/udev/rules.d/70-avermedia-gl310.rules` (c835 et d835, groupe plugdev + uaccess). Accès sans root : OK.
- `tools/gl310.py` : outil de test pyusb (`info`, `probe`, `regread`, `mcuread`, `led`, `blink`).

Résultats, boîtier fraîchement branché, sans firmware chargé :

| Commande | Réponse | Lecture |
|----------|---------|---------|
| lecture registre 0x0038 | `20 00 01 00` | 0x00010020 |
| lecture registre 0x0050 | `06 04 00 00` | 0x00000406 (le pilote Windows y écrit ensuite 0x0404) |
| lecture registre 0x0000 | `00 00 ff 07` | 0x07FF0000 (le pilote Windows y réécrit la même valeur) |
| lecture micro 0x15, reg 0x2C | `00 08` | |
| écriture micro reg 0x2C = 0x0b / 0x03 / 0x05 | `08` | relecture : `00 08` |
| écriture micro reg 0x2C = 0x08 | `08` | relecture : `08 08` |

**Constaté à l'œil : la LED du boîtier a changé pendant le test.** Le GL310 exécute donc nos commandes
USB depuis Linux, même sans firmware chargé.

Interprétation :
- Les lectures de registres marchent avant le firmware : elles passent par le pont USB lui-même.
- Les écritures vers le microcontrôleur (0x15) marchent aussi avant le firmware : la LED change.
- Les réponses se terminent toutes par `08`, sans doute un octet d'état ou d'accusé de réception.
  La relecture du registre LED ne reflète pas la valeur écrite (`00` au lieu de 0x0b…).
  Le registre 0x2C déclenche peut-être une action plutôt que de stocker une valeur. À creuser, ce n'est pas bloquant.

## Protocole d'envoi des firmwares (déduit de la trace)

- `09 01 08 00 00 00 00 00 <adresse32> <longueur32 en mots de 4 octets>` sur 0x04, puis le bloc de données sur 0x02.
  Exemple : `… 00 00 04 00 00 20 00 00` = 0x2000 mots (32 Ko) à l'adresse 0x00040000.
- `09 00 08 …` : relecture du même bloc sur 0x81, pour vérification.
- Audio chargé à partir de 0x00040000, vidéo à partir de 0x00000000.
- Juste avant : `07 00 00 …` (ligne 225), sans doute l'arrêt de l'ARM. Juste après : `07 01 00 …` (ligne 360), sans doute son démarrage.

## Phase 2 prudente : firmwares en RAM et démarrage de l'ARM (faite le 2026-10-19)

`tools/gl310_boot.py` rejoue les lignes 225 à 370 de la trace, avec les firmwares du GL310 et **sans aucune écriture I2C**.
Toutes les écritures I2C de la trace sont avant la ligne 225.
`--dry-run` compare les 146 commandes à la trace : seule la longueur du dernier bloc vidéo diffère (0x1B6C contre 0x1B68 mots).
Le journal complet est dans `logs/boot-*.log`.

Sens des commandes, confirmé par le pilote C985 :
- `07 00` / `07 01` : arrêt et démarrage de l'ARM (`DM_ResetArm`).
- `02 01 04 00 <mot> <mot> <val>` : écriture d'un mot en mémoire ARM, adresse en mots.
  Les trois écritures de la trace sont exactement la configuration QPSOS v2 du C985 :
  0x2F1094 (FixedMode) = 1, 0x2F1090 (IntMode) = 0 (polling), 0x2F2004 (config_base + 4) = 0.
- Registre 0x06CC : boîte aux lettres, que l'ARM surveille une fois lancé (selon le pilote C985).

Résultats :
- Les en-têtes `09 01` et `09 00` répondent `00`.
- **Les 26 blocs relus sont identiques** à ceux envoyés : 12 blocs audio et 14 blocs vidéo.
- Après `07 01` : le mot 0x40 vaut `QSOS` et le mot 0x41 vaut 0x0002001E (relus en RAM).
  Le mot à config_base + 0x10 vaut 0. Le registre 0x0610 vaut 0, le registre 0x0618 vaut 0x0000FF1F.
- Le boîtier reste en `c835` : `d835` n'est donc pas l'identifiant pris après le firmware, mais sans doute une autre variante matérielle.
- **L'ARM tourne** (`tools/gl310_memcheck.py`, lecture seule) : 147 mots de la zone vidéo ont changé
  par rapport à l'image envoyée, à partir de l'octet 0x064A78. On y trouve des pointeurs (0x00077B80…) et des
  listes vides qui pointent sur elles-mêmes (0x064AF0 -> 0x064AF0) : le système QPSOS a initialisé ses structures.
  Rien ne bouge entre deux lectures à 2 s d'intervalle : le firmware attend des ordres.
- La zone audio est inchangée : le firmware audio ne tourne sans doute qu'une fois démarré par l'ARM.

Source HDMI de test branchée sur le LGP Lite : Switch 2 (Mario Kart World).

Correction : d'après `c985_nuc100.c`, les registres 0x610, 0x614 et 0x618 sont des GPIO (direction, valeur, entrée),
pas une boîte aux lettres.

## Phase 3a : bus I2C et microcontrôleur, en lecture seule (faite le 2026-10-19)

- `0C 01 <n> 00 <adr7> 00 00 00 <reg>` : lecture de n octets. Réponse `<données…> <état>`, état `08` = OK, `06` = pas de puce.
  `0B 01 <n> 00 <adr7> 00 00 00 <reg> <données…>` : écriture (n = registre + données).
- Balayage `tools/gl310_i2cscan.py` (0x03-0x77) : **une seule puce sur ce bus, le microcontrôleur 0x15**.
- `06 …` : pas de réponse, commande inconnue. `05 00 01 00 <adr> 00 00 00 <reg>` répond `08` quelle que soit l'adresse : ce n'est pas une lecture.
  Le bus des commandes `05` du C875 (puces 0x21/0x31/0x35/0x3D, probablement Analog Devices) reste illisible.
  Il ne concerne sans doute pas le GL310 : le pilote Windows décrit aussi les ADV7441/ADV7614, et iFixit voit un ITE sur le GL310.
- **Le microcontrôleur 0x15 est le même type que le NUC100 du C985** (même adresse, même table de registres).
  D'après `c985_nuc100.c`, c'est lui qui gère l'entrée HDMI. Lectures :

| Registre | Lecture | Sens (table C985) |
|----------|---------|-------------------|
| 0x0B (3 o) | `33 31 30` = "310" | identifiant du modèle (C985 : "985") |
| 0x04 | 0x05 | version du firmware du micro |
| 0x1B | 0x00 | occupé = non |
| 0x1C | 0xBF | bit 2 = **signal HDMI présent** |
| 0x1D (7 o) | `98 78 80 65 44 38 17` | **1920×1080 actifs, 2200×1125 au total, horloge 0x34BC00/0x17 ≈ 150 MHz : 1080p60** |
| 0x26 | 0x03 | polarités des synchros |

  La Switch 2 est donc bien détectée en 1080p60. Le GL310 encode 1080p30 au maximum : il faudra lui demander un encodage à 30 i/s.
- La trace C875 lit exactement ces registres après le démarrage (0x1B, 0x1C, puis 0x1D sur 7 octets à la ligne 611) :
  elle surveille l'entrée HDMI par le microcontrôleur.

## Phase 3b : encodeur démarré, première capture (faite le 2026-10-19)

`tools/gl310_encode.py` rejoue les lignes 371 à 757 de la trace avec seulement les registres de la boîte aux lettres
et les lectures du micro. Les 20 écritures au micro et à l'I2C sont retirées.
`tools/gl310_capture.py` récupère ensuite le flux.

Boîte aux lettres (registres du pont USB, même disposition que le C985) :
- vers l'ARM : 0x6F8..0x6D0 paramètres, 0x6CC = (task<<16)|1, 0x6FC = (task<<16)|opcode.
  Le firmware remet le bit 0 de 0x6CC à 0 quand il a consommé le message.
- depuis l'ARM : 0x6C8 bit 0 = message prêt ; 0x6B0 = (task<<16)|type, 0x6B4..0x6C4 = paramètres.
  L'hôte doit remettre 0x6C8 bit 0 à 0, sinon le firmware reste bloqué.
- lecture de plusieurs registres : `01 00 <n> 00 <reg32>`.
- séquence de la trace : 0x0A, F1 (0x80000011), registre 0x800 = 7, F2 (0x01000100),
  réglages 0x10 (sous-commandes 0x0F, 0x10, 0x12, 0x13, 0x14, 0x16, 0x17, 0x02),
  bloc de config 0x6F8..0x6D0 (1920×1080, 30 i/s), puis 0x01 StartEncoder.
  Les 14 messages sont consommés. Après le démarrage, 0x800 = 0x00010007 (« vidéo prête »).

Messages d'image (type 0x40) :
- 0x6B4 : étiquette 0x83 (flux TS), 0x6B8 : adresse en mots, 0x6BC : 1 au retour au début du tampon,
  0x6C0 : taille en mots, 0x6C4 : 0xAAAAAAAA.
- Le flux est stocké dans un **tampon circulaire, des mots 0x668F00 à 0x6D6200** (1,8 Mo).
  Le message qui atteint la fin s'arrête au milieu d'un paquet TS, qui se termine au début du message suivant.
- Après lecture, on rend le tampon avec 0x30 CompleteArm (0x6F8 = étiquette, 0x6F4 = taille en mots…), comme le C985.
- Les données sont du **MPEG-TS dont chaque mot de 4 octets arrive à l'envers** : il faut inverser les octets.
- Le contenu est en **H.264 High 1920×1080 à 30 i/s (PID 0x44) et en AAC-LC 48 kHz stéréo (PID 0x45)**, plus PAT/PMT (0x0, 0x42).
- **Première capture d'écran propre** : `captures/switch2.png` (Mario Kart World, image clé intacte).
  Extrait vidéo : `captures/switch2-clip.mp4`. L'audio capturé contient bien du son (moyenne −27,8 dB, crêtes −14,9 dB).

Problèmes restants :
- Débit très élevé, environ 21 Mbit/s. Le bloc de config de la trace semble demander 30 000 kbit/s (0x6EC = 0x00787530).
- Le script Python ne suit pas toujours. Sur 4,4 s : 8 trous de compteur TS, et seulement 50 images vidéo décodées au lieu d'environ 130.
  Pistes : baisser le débit, lire plus gros d'un coup, réécrire la boucle en C ou avec des transferts asynchrones.
- **Pas de son sur la télé branchée en sortie HDMI du LGP** (image OK). L'audio arrive pourtant bien dans le boîtier.
  Il faut savoir si c'est lié à notre session (les écritures micro retirées : 0x40 à 0x44, 0x2F) ou si le boîtier fait toujours ça sans le pilote Windows.

## Son absent sur la télé (sortie HDMI du LGP) — enquête du 2026-10-19

- Test : boîtier débranché de l'USB puis rebranché (état d'usine, sans firmware) → **toujours pas de son sur la télé**.
  Ce n'est donc pas notre session qui coupe le son.
- Registres du micro à l'état d'usine (lecture seule) :
  0x00..0x0F = `00 43 36 48 05 0d 0c 10 12 01 47 33 31 30 00 00` (0x0A..0x0D = "G310").
  0x43 = 0x80 et 0x44 = 0x3E : déjà égaux aux valeurs que la trace écrit.
  0x40, 0x41, 0x42, 0x2F = 0 : la trace y écrit 0x01, 0x86, 0x01, 0x02. 0x30 (6 o) = 0 : la trace y écrit une date BCD.
- Le pilote GL310 contient les classes ProjectC875, C985, C985E et CV910, mais pas de GL310 dédiée.
  Il utilise sans doute le code du C875.
- **Attention** : le pilote sait *enregistrer un profil* dans le micro (`PROJECT_PROPERTY_SET_SAVE_MCU_PROFILE`, débit, fréquence d'images, résolution).
  Les écritures 0x43/0x44 (0x3E80 = 16 000), 0x41/0x42 et 0x40 = 1 de la trace ressemblent à ce profil d'enregistrement autonome du C875,
  0x40 = 1 étant peut-être « enregistrer ». **Ne pas les rejouer** sans comprendre : risque d'écriture permanente.
  0x30 + 0x2F = 2 correspondent probablement à « MCU Set RTC time ».
- « Info: Audio Switch done by MCU » est un événement envoyé par le micro au PC (fonction 0x46be0), pas une commande.
  Il en va de même pour « MCU I2C read/write command done » : le proxy I2C du micro (0x0E/0x13/0x12) est asynchrone.
- Switch 2 réglée en sortie audio stéréo : toujours pas de son sur la télé.
- Guide officiel `QG_GL310_D47.pdf` : le GL310 a **HDMI IN, HDMI OUT, AUDIO IN (jack 3,5 mm) et AUDIO OUT (jack 3,5 mm)**.
  Hypothèse : la « bascule audio » est une détection de prise. Si un jack est branché sur AUDIO IN, le boîtier
  bascule peut-être sur l'entrée analogique. Autre possibilité : le son de copie ne sort que par AUDIO OUT.
- Fenysk se souvient d'avoir eu du son en HDMI sous Windows : c'est donc le pilote qui l'active.
  La prise AUDIO OUT, testée sur des enceintes, fonctionne.
- Fichier d'installation, section `[Encoder]` : `EncLinkVOut=1`, `EncLinkAOut=1`, ce qui correspond au paramètre F2 (0x01000100) ;
  `EncAudioControlParam=0x21161080`, `EncAudioControlExAAC=0x520800F4`.
  Le bloc de config de la trace contient 0x6D4 = AUDIO_CONTROL_PARAM et 0x6D0 = AUDIO_CONTROL_EX.
- Champs de **AUDIO_CONTROL_PARAM**, d'après le découpage fait par le pilote (fonction CQLCodec_Set, vers 0x5fd65) :
  bits 0-9 débit audio (0x80 = 128 kbit/s), 10-11 ch_sel, 12-15 sample_rate, **16 aou_pass_en**, 17 ai_msb, 18 lrclk_i,
  19 bclk_i, 20 ai_i2s, 21 ai_rj, 22 ai_m, 23-24 ch_num (2), 25 pts, 26 disAi, 27 raw, 28-31 audio_type (2).
  La valeur par défaut 0x21161080 a **aou_pass_en = 0**. Essai : `gl310_encode.py --audio-pass` envoie 0x21171080.
- Le pilote gère aussi `QPCODEC_PROP_AOU_CONFIG` (ao_en, ao_msb, lrclk_i, bclk_i, ao_i2s, ao_rj, ao_s) et
  `QPCODEC_PROP_I2S_OUT_CNTL` (enable) : ce sont d'autres réglages de la sortie audio du firmware, à essayer si besoin.
- Avec aou_pass_en, le tampon circulaire de l'encodeur est ailleurs (première image à l'adresse 0x102F00 en mots).
- Essai `--audio-pass` (0x21171080) : l'encodeur tourne, 90 s stables (3 130 messages, environ 24 Mbit/s), mais **toujours pas de son HDMI**.
- Registres du micro identifiés dans le pilote (appels à MCU::WriteRegister, fonction 0x47e20 : dl = registre, r8b = valeur) :
  | Reg | Rôle (message du pilote) | Valeur |
  |-----|--------------------------|--------|
  | 0x3A | bascule vidéo : « Switch to HDMI video » / « VGA video » | 0x02 / 0x03 |
  | 0x2E | bascule audio : « Switch to HDMI audio » / « 3.5 external » / « LR external » | 0x04 / 0x05 / 0x06 |
  | 0x40 | fin d'initialisation (fonction 0x48700, juste avant la LED) | 0x01 |
  | 0x43, 0x44 (+ 0x42 pour valider) | luminosité / contraste | 0x80 / 0x3E |
  | 0x30.., 0x2F | horloge (« Year, Month, Day… », fonction 0x48230) | date BCD, 0x02 |
  | 0x2C | LED | |
  | **0x47, 0x49** | **SET_SAVE_MCU_PROFILE (enregistrement permanent) : NE PAS TOUCHER** | |
- Essais : 0x2E = 0x04 seul, puis 0x40 = 1, puis 0x2E = 0x04 (qui reste alors à 0x04) : **toujours pas de son HDMI**.
  Remarque : écrire 0x40 = 1 remet 0x2E à 0.
- Fenysk se souvient d'avoir eu du son HDMI sous Windows. Le moyen le plus sûr de trouver ce qui manque :
  une trace USBPcap du vrai pilote GL310 sur la tour, avec un écran à haut-parleurs branché en sortie.
- Pistes de contournement si la sortie HDMI reste muette : casque ou enceinte sur AUDIO OUT,
  répartiteur HDMI avant le LGP, ou casque Bluetooth sur la Switch 2.

## VM Windows pour observer le pilote officiel (2026-10-19)

- `vm/start-vm.sh [--usb]` : QEMU/KVM, q35, UEFI Secure Boot (OVMF_CODE_4M.secboot + vars MS), TPM 2.0 swtpm
  (socket dans /tmp : AppArmor n'autorise que /tmp et $HOME, et les chemins de socket sont limités à 108 caractères),
  3 vCPU, 6 Go, disque `vm/win11.qcow2` 64 Go, CD 1 = ISO Windows, CD 2 = `vm/gl310-driver.iso` (pilote GL310),
  e1000 + NAT, qemu-xhci ; avec `--usb`, ajoute `usb-host` 07ca:c835.
- ISO : Windows 11 Enterprise LTSC 2024 Evaluation en-us, build 26100.1742,
  `vm/iso/win11-ltsc-eval-en-us.iso` (5 112 850 432 o, sha256 67cec586…5da68).
  Index 1 = Enterprise LTSC Evaluation (à choisir), index 2 = édition N, sans composants multimédias.
- `vm/capture-usb.sh` : `sudo modprobe usbmon` puis `dumpcap` sur le bus du boîtier → `logs/win-trace-*.pcapng`.
- `tools/usbpcap_decode.py` : décode la capture (via tshark) en opérations REG/MCU/MOT/BLOC, au même format que la trace C875.
- Procédure : boîtier débranché puis rebranché (état d'usine) → `vm/capture-usb.sh` → `vm/start-vm.sh --usb`
  → installation du pilote depuis le CD GL310DRV → vérifier le son sur la télé → arrêter la capture → décoder.

### Résultats de la VM (2026-10-19)

- 1re installation bloquée : sans identifiant unique, Windows a pris la VM pour une machine d'entreprise
  (écran de connexion SAP). Corrigé avec UUID et numéro de série uniques (`vm/vm-identity`), et une VM sans réseau par défaut.
- La capture se fait **sans sudo** : `usb-host,...,pcap=logs/win-usb-*.pcap` (QEMU tronque chaque paquet à 320 octets,
  mais les commandes sont complètes). Pilotage de la VM : `vm/vmctl.py` (touches AZERTY, captures d'écran).
- Pilote installé avec `pnputil` (script `install.cmd` du CD). DirectShow expose un seul périphérique, « AVerMedia C835 Capture »,
  que ffmpeg classe en type « none ».
- **Séquence du pilote officiel GL310** (`logs/win-decoded.txt`) :
  1. réglages du pont USB (0x081C..0x0840, 0x0610/0x0614, 0x50) ;
  2. **206 écritures I2C `05`** (puces 0x31 ×3, 0x35 ×5, 0x21 ×22, 0x3D ×176) : **identiques octet pour octet** aux lignes 1 à 224 de la trace C875 ;
  3. firmwares (identique à notre phase 2), puis démarrage de l'ARM, puis lectures QSOS ;
  4. micro : lecture de 0x2E et 0x3A, puis 0x43=80, 0x44=3E, 0x42=01, 0x41=86, message 0x0A, 0x30 = date BCD du jour,
     0x2F=02, 0x40=01, LED 0x2C=05 — **identique à la trace C875** ;
  5. ensuite, surveillance de 0x1B et 0x25 environ toutes les 0,75 s. Pas de démarrage d'encodeur sans application.
- Conclusion : **la seule vraie différence avec notre séquence Linux, ce sont les 206 écritures I2C** retirées par prudence.
  Comme le pilote officiel les fait à chaque branchement, elles sont sûres. C'est probablement ce qui configure le son HDMI.
- Outils mis à jour : `gl310_boot.py` fait désormais l'initialisation officielle par défaut (`--no-i2c` pour l'ancien mode) ;
  `gl310_encode.py` inclut les écritures micro et I2C (`--prudent` pour l'ancien mode), refuse 0x47/0x49, et met la date du jour dans 0x30.

### Tests du son HDMI à faire

- A (VM) : la VM tourne avec le pilote officiel chargé. Réveiller la Switch 2 et écouter la télé.
- B (Linux) : éteindre la VM, débrancher/rebrancher le boîtier, puis
  `python3 tools/gl310_boot.py && python3 tools/gl310_encode.py --observe 2 && python3 tools/gl310_capture.py --frames 100000 --timeout 120`
  et écouter la télé.

### Résultats des tests de son HDMI (2026-10-19)

- **Test A** (VM, pilote Windows officiel chargé, Switch 2 réveillée) : image OK, **pas de son** sur la télé.
- **Test A2** : ffmpeg sous Windows ne sait pas ouvrir « AVerMedia C835 Capture » (seule sortie listée : « PCM Out »).
  L'ouverture déclenche des milliers d'accès aux GPIO 0x0610/0x0618, sans doute l'authentification par la puce AT88
  (`AVer_GPIOI2C::AT88_*`), mais pas de démarrage d'encodeur : il faudrait l'application AVerMedia.
- **Test B** (Linux, séquence officielle complète : 206 I2C, firmwares, micro, encodeur, 5 I2C de démarrage) :
  tout est acquitté, l'encodeur tourne, mais **pas de son** sur la télé.
- La fiche technique officielle prévoit pourtant le son en sortie HDMI (« Digital: HDMI, Pass-through from HDMI Input »).
- Switch 2 branchée directement sur la télé (même câble, même entrée) : **son OK**. Le son se perd donc dans le LGP Lite.
- Infos de la télé sur l'entrée via le LGP : « 1080p 60Hz **Digital Stereo** SDR ». Ce n'est pas du DVI :
  une piste audio PCM stéréo existe, mais elle est **silencieuse**.
- **Test C** (initialisation officielle + aou_pass_en + micro + encodeur) : toujours pas de son HDMI.
- **Boîtier alimenté par un chargeur USB, sans PC : pas de son HDMI non plus.** Le son manque donc dans tous les états du boîtier
  (sans PC, pilote Windows, séquence Linux complète, avec ou sans aou_pass_en). Explication probable : copie audio HDMI
  défaillante sur ce boîtier, ou incompatible avec la Switch 2. Contournements : répartiteur HDMI avant le LGP Lite
  (télé en direct), prise AUDIO OUT vers la télé ou une barre de son, casque Bluetooth sur la Switch 2.
- Pistes qui étaient restantes : alimenter le boîtier par un chargeur USB (sans PC), fait, voir ci-dessus ;
  lancer l'application AVerMedia dans la VM, puisque le pilote seul ne suffit pas (test A).
- Ancienne piste : le problème ne venait plus du logiciel côté Linux. Il fallait vérifier la chaîne HDMI matérielle :
  la Switch 2 branchée directement sur la télé (même câble, même entrée) ; un autre câble sur HDMI OUT ;
  la télé qui afficherait « DVI » (pas d'audio) au lieu d'« HDMI » sur cette entrée.

**Décision (2026-10-19)** : pour le son en jouant, Fenysk utilisera la prise AUDIO OUT ou un casque Bluetooth sur la Switch 2.
`gl310_encode.py` active désormais aou_pass_en par défaut (`--no-audio-pass` pour l'enlever), car la prise AUDIO OUT
a fonctionné dans cette configuration.

### Séquence de démarrage complète (boîtier fraîchement branché)

    python3 tools/gl310_boot.py && python3 tools/gl310_encode.py --observe 2 && python3 tools/gl310_capture.py --frames 100000 --timeout 120

## Phase 4 : pilote noyau, puis GL310 Studio en Rust (2026-10-19)

**Pilote noyau C (`driver/gl310/`)** — module V4L2 sur le modèle de hdpvr : `/dev/videoN` en V4L2_PIX_FMT_MPEG,
`read()` renvoie le MPEG-TS, 6 contrôles (luminosité, contraste, débit vidéo, GOP, débit AAC, AUDIO OUT), état du
signal par VIDIOC_ENUMINPUT / VIDIOC_QUERY_DV_TIMINGS, DKMS (`install.sh`), chargement de développement (`load.sh`).
Compile sans avertissement (W=1), `make test` vert. **Jamais chargé** : Secure Boot actif et la clé MOK d'Ubuntu
(`/var/lib/shim-signed/mok/MOK.der`) n'a jamais été enregistrée — le pilote NVIDIA DKMS ne se charge pas non plus pour
cette raison (le portable tourne sur l'Intel HD 630). Mis de côté au profit de l'appli Rust.

**Tables générées** : `tools/gen_gl310_tables.py` lit la trace et écrit `studio-rs/src/tables.rs` et
`driver/gl310/gl310_tables.h` (initialisation officielle l. 1-224, démarrage de l'encodeur l. 371-757) avec des drapeaux
(réponse attendue, attente de la boîte aux lettres, champs à remplacer : débit, GOP, audio, luminosité, contraste, horloge).
Il vérifie que chaque champ à remplacer apparaît une seule fois et refuse toute écriture micro 0x47/0x49.

**GL310 Studio en Rust (`studio-rs/`)** — pilote USB en espace utilisateur (rusb), traduction directe des outils Python.
Vérifié sur le boîtier : arrêt + relance de l'encodeur, 121 morceaux en 4 s, 7,7 Mbit/s pour 8 demandés, H.264
1920×1080 30 i/s + AAC, image correcte.
- Le bit 16 de 0x800 reste à 1 après l'arrêt de l'encodeur (0x02 + 0xF3) : il ne dit pas si l'encodeur tourne.
  L'appli fait donc toujours un arrêt (inoffensif sur un encodeur déjà arrêté) quand le firmware est déjà en RAM.
- Caméra virtuelle : `gst-launch-1.0 fdsrc ! rawvideoparse format=i420 … ! pipewiresink mode=provide` avec
  `media.class=Video/Source,media.role=Camera` crée un nœud caméra (I420 1920×1080 30/1) — **seulement à la première
  image** (rien sans signal HDMI). Un lecteur qui rend ses tampons tout de suite (`pipewiresrc always-copy=true`, comme
  OBS) reçoit 30 i/s ; un lecteur qui les garde bloque le producteur après 16 images. Pas encore essayé dans OBS
  (OBS n'est pas installé sur le portable).
- Au démarrage, pipewiresink affiche `GStreamer-CRITICAL … gst_caps_is_equal` : sans conséquence observée.
- **Son du jeu** : publié d'abord avec pipewiresink (`media.class=Audio/Source`), il avait des trous (64 ms toutes les
  ~1 s, 6,2 s reçues en 9 s) : le nœud cadençait le graphe sans réserve. Solution retenue : entrée virtuelle
  `pactl load-module module-null-sink sink_name=gl310_audio media.class=Audio/Source/Virtual` (retirée à la fermeture),
  alimentée par `pw-cat --playback -P '{ node.autoconnect = false, node.name = gl310-feed }' --latency 100ms` et
  reliée par `pw-link gl310-feed:output_FL gl310_audio:input_FL` (+ FR). Sans lien explicite, WirePlumber envoie
  pw-cat vers les haut-parleurs (une « source » n'est pas une cible de lecture). Résultat : 7,98 s reçues sur 8 s, sans
  trou, −32 dB comme la source.
- Essais de l'appli complète avec la Switch (1080p60) : signal retrouvé tout seul au réveil de la console (encodeur relancé
  en 1 s) ; aperçu ; caméra 148 images en 5 s ; flux HTTP lu par ffprobe (H.264 1080p + AAC) ; enregistrement de 18,8 s
  converti en MP4 ; débit 8 → 4 Mbit/s appliqué (mesuré 4,8 Mbit/s TS compris) puis remis à 8 ; fermeture propre
  (encodeur arrêté, entrée audio retirée). Processeur : appli 37 %, ffmpeg vidéo 28 %, gst 18 % d'un cœur.
  Pas encore essayé : luminosité/contraste à chaud en Rust, OBS lui-même (non installé sur le portable).

## Flux vers OBS sur la tour : saccades (2026-10-19)

OBS (Source média, `http://IP-DU-PORTABLE:4848/stream.ts`) recevait le flux mais saccadait. Mesures sur le portable
(`ss -tni`, ping vers la tour) : Wi-Fi 5 GHz 270 Mbit/s, signal 77 % ; TCP rtt 88 ms ± 78 (minimum 2 ms),
620 Ko en attente d'envoi, 162 segments retransmis ; OBS reconnecté 4 fois en 4 min. `wifi.powersave = 3` (NetworkManager)
→ coupé (`nmcli connection modify … 802-11-wireless.powersave 2` + reconnexion ; `device reapply` refuse ce réglage).
Ensuite le réseau livre 30 à 98 Mbit/s par moments ; ~3 Mo restent en attente dans la socket (OBS lit au rythme de la
lecture : ~3 s de retard, pas de perte). Mémoire tampon OBS conseillée : 2–3 Mo (1 Mo, conseillé d'abord, était trop
juste). Bascule automatique secteur/batterie : `tools/gl310-wifi-powersave.sh` (vérifiée en simulant l'alimentation).
Si ça saccade encore : câble Ethernet (`eno1` libre) ou envoi en SRT (ffmpeg du portable a `srt`).

Délai (~4 s dans OBS avec 0 Mo de tampon) : rien en attente côté portable (Send-Q ≈ 0, rtt 3–5 ms). C'est l'analyse
de ffmpeg à l'ouverture. Première image (meilleure de 4, ffmpeg du portable, lecteur démarré sur image clé) : par défaut
5,7 s ; `fflags=nobuffer flags=low_delay` 7,2 s (pire) ; `probesize=500000 analyzeduration=500000` 2,0 s, dont ~2 s
d'attente de l'image clé suivante (artefact des essais enchaînés), donc analyse quasi nulle. Images clés : début de PES
vidéo (PID 0x44) avec AUD, SPS, PPS, IDR, toutes les 2 s ; pas d'indicateur RAI.

Après démarrage sur image clé + `probesize=500000 analyzeduration=500000` : ~3 s ressentis dans OBS. Côté portable :
PTS − PCR 67 ms (vidéo) / 57 ms (audio), flux en temps réel (7,27 s de PCR en 7,24 s), 0 trou de continuité et 0 TEI
sur 20 s (103 544 paquets). Côté tour (relevé du Claude de la tour) : OBS 32.2.1, Ethernet derrière un
routeur (1 saut, 2 ms) ; **tampon audio global d'OBS au plafond de 960 ms depuis 14:45** (« Audio du bureau ») : il retarde
toutes les sources, image comprise ; 9 coupures et ~1 580 erreurs H.264 entre 16:24 et 17:00 (jonctions en plein GOP,
coupures Wi-Fi, relances de l'encodeur pendant les essais).

**Latence du boîtier mesurée** (écran d'accueil de la Switch, changement de minute de l'horloge repéré dans le flux
décodé sur le portable, comparé à l'horloge du portable synchronisée par NTP) : 1,69 s (17:48) et 1,53 s (17:49). Le
boîtier + l'appli + le décodage local prennent donc ~1,6 s ; l'appli et le réseau n'y sont pour presque rien. OBS ajoute
0,5 à 1,5 s (2–3 s ressentis sur la tour). Outil : `clock_latency.py` (crop 110×80 à x=1500, y=70 dans l'image 1080p).
**Luminosité/contraste du micro (0x43/0x44 + 0x42)** : aucun effet sur l'image encodée (luminance du flux identique à
0 et à 128) ; peut-être seulement sur la sortie HDMI vers la télé, non vérifié. **Retirés de GL310 Studio** (19/10, 18:40) : le démarrage
envoie les valeurs de la trace (0x80 / 0x3E, déjà celles d'usine). 0x43/0x44 = 0x3E80 = 16 000 ressemble plutôt à un
débit du profil d'enregistrement autonome qu'à un réglage d'image. Les anciennes clés `brightness`/`contrast` du
fichier de réglages sont ignorées.

**Mesure du délai sans la console** : `tools/latency_probe.py`. La sortie HDMI du portable (HDMI-1 pour xrandr, carte Intel)
remplace la console sur l'entrée du boîtier ; le script y affiche l'heure du portable (en clair + code-barres de 2 × 20
blocs, la seconde rangée inversée, heure en ms modulo 2^20), lit `http://127.0.0.1:4848/stream.ts` (ffmpeg `-threads 1`,
image réduite à 480×270 en gris) et calcule réception − affichage pour chaque image. Essai sur l'écran du portable
(`--output eDP-1 --source x11grab`) : 356 images, 0 illisible, **68 ms** (affichage + capture + décodage : à retrancher).
Page `http://<portable>:4849/` : l'heure du portable synchronisée par le réseau (meilleur aller-retour), à mettre en
source navigateur dans OBS à côté du jeu ; l'écart entre les deux heures est le délai du jeu dans OBS.

## Prochaines étapes

1. ~~Récupérer les firmwares du GL310~~ : fait (voir Phase 0).
2. ~~Outils + règle udev + test de la LED~~ : fait (voir Phase 1).
3. ~~Phase 2 prudente : firmwares en RAM et démarrage de l'ARM~~ : fait, l'ARM tourne.
4. ~~Phase 3 : obtenir de la vidéo~~ : fait, première capture propre (voir Phase 3b). Il reste à :
   régler le son de la télé, fiabiliser le débit, puis transmettre le flux à la tour (étape 2 du projet).
   Ancien plan, pour mémoire :
   - Configurer le récepteur HDMI. La trace contient environ 200 écritures I2C (adresses 0x21, 0x31, 0x35, 0x3D),
     mais elles viennent du C875. Avant de les rejouer, il vaut mieux identifier les puces en lecture seule,
     ou enregistrer la trace du GL310 sur la tour.
   - Parler au firmware par la boîte aux lettres (`QPFWAPI_SendMessageToARM`) pour démarrer l'encodeur.
     Ce sont les lignes 371 à 762 de la trace, à interpréter avec `c985_mbox.c`.
   - Lire le flux H.264 sur l'endpoint 0x81 et l'envoyer à ffmpeg.
5. Si ça bloque : trace USBPcap du GL310 sur la tour Windows, ou VM Windows (QEMU/KVM) avec le boîtier
   passé en USB et capture usbmon du vrai protocole GL310. Les endpoints sont en bulk, pas en isochrone, donc le passthrough a de bonnes chances de marcher.

## Sources

- https://hackweek.opensuse.org/projects/linux-driver-for-the-avermedia-lgp-lite-gl310
- https://github.com/Trouffman/octv_gears_lgp
- https://github.com/FireCulex/avermedia-c985-linux
- https://www.ifixit.com/Teardown/AVerMedia+GL310+Teardown/140562
- https://goughlui.com/2015/04/10/review-teardown-avermedia-c875-live-gamer-portable-hdmi-capture-box-gl710/
- https://linux-hardware.org/?id=usb:07ca-c835
- https://archive.org/details/aver-media-gl-310-usb-hd-capture-device-v-3.2802.64.40

**Relance de GL310 Studio en plein live (19/10, 19:04, avec l'accord de Fenysk)** : nouvelle version (clients réseau
coupés à chaque démarrage de l'encodeur), OBS « Console de jeu » en `reconnect_delay_sec` 1 et `hw_decode` true (réglé
par la session de la tour). Le jeu revient dans OBS ~7 s après la relance, tout seul (mesure de la tour, une capture
par seconde). Ces 7 s comprennent le délai normal du jeu (~2,5 s), la reconnexion (1 s), l'attente d'une image clé
(0 à 2 s) et l'analyse du flux (0,5 s). Passer aux images clés toutes les 1 s ne gagnerait qu'~1 s, et seulement
dans ce cas rare : non retenu.

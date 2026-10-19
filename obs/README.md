# Mode console pour OBS (sur la tour)

Le jeu de la console arrive dans OBS avec environ 2 s de retard (boîtier GL310, réseau, OBS). Tout ce qui vit « au
présent » (webcam, micro, bruitages, Jean-Renard…) arrive donc **avant** le jeu. `mode-console.lua` ajoute un seul
raccourci qui bascule : au premier appui, ces sources sont retardées du même délai que le jeu ; au second, tout
revient exactement comme avant.

## Installation (une fois)

1. Copier `mode-console.lua` sur la tour, par exemple dans `%APPDATA%\obs-studio\scripts\`.
2. OBS > **Outils > Scripts** > **+** > choisir `mode-console.lua`. Régler **Délai** (2000 ms pour commencer).
3. Créer une scène **Synchro jeu**. C'est une simple **liste**, à ne jamais afficher : y ajouter, avec
   **Ajouter > (type) > Ajouter une source existante**, tout ce qui doit suivre le jeu (webcam, micro, Jean-Renard,
   bruitages…). Les mises en page ne changent pas : chaque source est retardée partout où elle apparaît.
4. **Paramètres > Raccourcis clavier** > « Mode console : activer / désactiver » : choisir une touche. Sur le Stream
   Deck, une action « Raccourci clavier » qui envoie cette touche.
5. Facultatif : une source texte nommée **Témoin mode console** (par exemple « MODE CONSOLE » dans un coin). Elle
   n'est visible qu'en mode console.

## Au quotidien

- Nouvelle caméra, nouveau bruitage : l'ajouter à la scène « Synchro jeu ». Rien d'autre à régler.
- Source renommée : aucune importance (le script repère les sources par leur identifiant).
- Le jeu (la Source média qui lit `…/stream.ts`) n'est jamais retardé ; s'il se retrouve dans la liste, le script
  refuse et l'écrit dans le journal d'OBS (Aide > Fichiers journaux).
- OBS fermé en mode console : tout est remis comme avant au démarrage suivant.
- App web (obs-websocket) : requête `TriggerHotkeyByName` avec `hotkeyName = "mode_console.toggle"`.

## Régler le délai

- **Image** : pointer la webcam vers la télé (aucun retard), bouger le curseur sur l'écran d'accueil de la Switch,
  ajuster « Délai » jusqu'à ce que la webcam et la source du jeu bougent en même temps.
- **Son** : enceintes sur la prise AUDIO OUT du boîtier, entendues par le micro ; ajuster jusqu'à ce que le son capté
  par le micro tombe en même temps que celui de la source du jeu.
- Changer le délai pendant le mode console : il est réappliqué tout de suite, sans s'additionner.

## Comment ça marche

| Type de source | Exemple | Retard appliqué |
|---|---|---|
| Image asynchrone | webcam, caméra mobile, vidéo | filtre « Délai vidéo (asynchrone) », qui retarde aussi son son |
| Image synchrone | Jean-Renard (navigateur), image | filtre « Délai de rendu » + décalage de synchronisation du son |
| Son seul | micro, sons | décalage de synchronisation |

Les filtres s'appellent « Mode console » et sont retirés au second appui ; les décalages d'origine sont rendus.
Le « Délai de rendu » au-delà de 500 ms n'est réglable que par script et garde les images en mémoire vidéo
(~1,2 Go pour 2,5 s d'une source 1920×1080 à 60 i/s) : une RTX 3070 l'encaisse, mais éviter d'en mettre dix.

Tests sans OBS (sur le portable Linux) : `obs/run-tests.sh` (faux OBS, 43 vérifications).

# Règles du dépôt

- **`./gate.sh` à 0 avant toute annonce et tout commit.** C'est toi qui le lances et tu boucles jusqu'au vert ; ne jamais
  l'affaiblir (test modifié pour passer, `#[allow]`, motif ajouté à une liste d'exceptions).
- **Dépôt public** : aucune info perso (IP du réseau local, e-mail, chemins personnels : `tools/check-public.sh`), aucun
  fichier d'AVerMedia ni d'autres auteurs (firmwares, pilotes, trace : `vendor/`, `tools/fetch-vendor.sh`), aucune
  image ou vidéo de la maison.
- **Décisions** : une entrée en tête de [JOURNAL.md](JOURNAL.md) (Contexte / Décision / Renoncement), écrite avant le
  code quand le changement touche l'architecture ou un contrat. Enquête technique : [NOTES.md](NOTES.md).
- **Boîtier** : ne jamais écrire les registres 0x47 / 0x49 du microcontrôleur (profil permanent). Tout le reste est en
  RAM ; en cas de blocage, débrancher le boîtier 10 s.
- **Live** : ne jamais relancer GL310 Studio, l'encodeur ou le boîtier sans avoir vérifié que Fenysk n'est pas en live
  (le flux alimente OBS sur la tour ; demander à la session de la tour qui lit OBS).
- Commits d'une ligne en anglais, comme `stream_twitch`. Docs et commentaires en français, commentaires rares et courts.

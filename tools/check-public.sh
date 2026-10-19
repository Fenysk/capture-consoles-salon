#!/bin/sh
# Le dépôt est public : refuse les infos perso dans les fichiers suivis (IP du réseau local, adresses e-mail,
# chemins personnels). Échoue en listant chaque ligne fautive.
set -eu
cd "$(dirname "$0")/.."

PATTERNS='(^|[^0-9.])(192\.168|10|172\.(1[6-9]|2[0-9]|3[01]))\.[0-9]{1,3}\.[0-9]{1,3}([^0-9.]|$)
[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z0-9.-]*[A-Za-z]{2,}
/home/[A-Za-z]
[Cc]:\\+Users\\+[A-Za-z]'

# Fichiers suivis, plus ceux pas encore ajoutés mais non ignorés (premier commit).
found=$(git ls-files -z --cached --others --exclude-standard | xargs -0 grep -I -n -E -e "$PATTERNS" -- 2>/dev/null \
	| grep -v -E 'C:\\+Users\\+Public' || true)

if [ -n "$found" ]; then
	echo "Infos perso dans le dépôt public :"
	echo "$found" | cut -c1-200
	exit 1
fi
echo "check-public : rien de personnel."

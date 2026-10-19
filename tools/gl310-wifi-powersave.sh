#!/bin/sh
# Économie d'énergie du Wi-Fi selon l'alimentation du portable :
#   - sur secteur  : coupée (802-11-wireless.powersave = 2), sinon la carte s'endort entre deux paquets et le flux
#                    GL310 vers la tour saccade (allers-retours jusqu'à 75 ms au lieu de 1 à 2 ms) ;
#   - sur batterie : remise à la valeur par défaut du système (0 = « default », activée sur Linux Mint).
# Lancé à l'ouverture de session (~/.config/autostart/gl310-wifi-powersave.desktop, posé par install.sh).
# Passe par nmcli : pas de mot de passe. Chaque bascule reconnecte le Wi-Fi (2 à 3 s), NetworkManager ne sachant
# pas appliquer ce réglage à chaud.
set -u
SUPPLIES=${GL310_POWER_SUPPLY_DIR:-/sys/class/power_supply} # remplaçable pour les essais

on_mains() {
	for supply in "$SUPPLIES"/*; do
		[ "$(cat "$supply/type" 2>/dev/null)" = Mains ] && [ "$(cat "$supply/online" 2>/dev/null)" = 1 ] && return 0
	done
	return 1
}

wifi_connection() {
	nmcli -t -f NAME,TYPE connection show --active | awk -F: '$2 == "802-11-wireless" { print $1; exit }'
}

# apply <valeur nmcli> <nom affiché par nmcli -g>
apply() {
	connection=$(wifi_connection)
	[ -n "$connection" ] || return 0
	[ "$(nmcli -g 802-11-wireless.powersave connection show "$connection")" = "$2" ] && return 0
	nmcli connection modify "$connection" 802-11-wireless.powersave "$1" &&
		nmcli connection up "$connection" >/dev/null &&
		logger -t gl310-wifi "économie d'énergie Wi-Fi : $2 ($connection)"
}

last=""
while :; do
	if on_mains; then state=secteur; else state=batterie; fi
	key="$state|$(wifi_connection)"
	if [ "$key" != "$last" ]; then
		if [ "$state" = secteur ]; then apply 2 disable; else apply 0 default; fi
		last="$state|$(wifi_connection)"
	fi
	sleep 5
done

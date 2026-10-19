#!/bin/sh
# Installe GL310 Studio pour l'utilisateur courant, sans sudo : l'appli (avec son pilote USB intégré),
# les firmwares du boîtier, l'icône, l'entrée « GL310 Studio » du menu (rubrique Son et vidéo), et la bascule
# automatique de l'économie d'énergie du Wi-Fi (coupée sur secteur pour un flux fluide, remise sur batterie).
#
#   ./install.sh            installe ou met à jour (compile d'abord)
#   ./install.sh --remove   désinstalle (garde les réglages et les vidéos)
set -eu

ROOT=$(cd "$(dirname "$0")" && pwd)
DATA=${XDG_DATA_HOME:-$HOME/.local/share}
BIN=$HOME/.local/bin/gl310-studio
FIRMWARE=$DATA/gl310-studio/firmware
DESKTOP=$DATA/applications/gl310-studio.desktop
ICONS=$DATA/icons/hicolor
RULE=/etc/udev/rules.d/70-avermedia-gl310.rules
WIFI_BIN=$HOME/.local/bin/gl310-wifi-powersave
WIFI_AUTOSTART=${XDG_CONFIG_HOME:-$HOME/.config}/autostart/gl310-wifi-powersave.desktop

refresh_menu() {
	update-desktop-database -q "$DATA/applications" 2>/dev/null || true
	gtk-update-icon-cache -q -t "$ICONS" 2>/dev/null || true
}

if [ "${1:-}" = "--remove" ]; then
	pkill -f "^/bin/sh $WIFI_BIN" 2>/dev/null || true
	rm -f "$BIN" "$DESKTOP" "$ICONS/scalable/apps/gl310-studio.svg" "$ICONS/256x256/apps/gl310-studio.png"
	rm -f "$WIFI_BIN" "$WIFI_AUTOSTART"
	rm -rf "$DATA/gl310-studio"
	refresh_menu
	echo "GL310 Studio désinstallé (réglages dans ~/.config/gl310-studio et vidéos gardés)."
	exit 0
fi

[ -f "$ROOT/vendor/gl310-driver/extracted/qpvidfwusb.bin" ] || {
	echo "Firmwares du boîtier absents (non publiés dans le dépôt) : lance d'abord tools/fetch-vendor.sh"
	exit 1
}

CARGO=${CARGO:-$HOME/.cargo/bin/cargo}
[ -x "$CARGO" ] || CARGO=$(command -v cargo) || {
	echo "Rust est absent : installe-le d'abord avec https://rustup.rs"
	exit 1
}
echo "==> Compilation"
"$CARGO" build --release --quiet --manifest-path "$ROOT/studio-rs/Cargo.toml"

echo "==> Copie de l'appli, des firmwares et de l'icône"
install -D -m 0755 "$ROOT/studio-rs/target/release/gl310-studio" "$BIN"
for fw in qpvidfwusb.bin qpaudfwusb.bin; do
	install -D -m 0644 "$ROOT/vendor/gl310-driver/extracted/$fw" "$FIRMWARE/$fw"
done
install -D -m 0644 "$ROOT/studio-rs/assets/gl310-studio.svg" "$ICONS/scalable/apps/gl310-studio.svg"
install -D -m 0644 "$ROOT/studio-rs/assets/gl310-studio.png" "$ICONS/256x256/apps/gl310-studio.png"

echo "==> Entrée du menu"
mkdir -p "$(dirname "$DESKTOP")"
cat > "$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=GL310 Studio
GenericName=Capture HDMI
Comment=Aperçu, enregistrement et caméra virtuelle du boîtier AVerMedia Live Gamer Portable Lite
Exec=$BIN
Icon=gl310-studio
Terminal=false
Categories=AudioVideo;Video;Recorder;
Keywords=capture;hdmi;console;switch;obs;avermedia;lgp;
StartupWMClass=gl310-studio
StartupNotify=true
EOF
desktop-file-validate "$DESKTOP" 2>/dev/null || true
refresh_menu

echo "==> Économie d'énergie du Wi-Fi : coupée sur secteur, remise sur batterie"
install -D -m 0755 "$ROOT/tools/gl310-wifi-powersave.sh" "$WIFI_BIN"
mkdir -p "$(dirname "$WIFI_AUTOSTART")"
cat > "$WIFI_AUTOSTART" <<EOF
[Desktop Entry]
Type=Application
Name=GL310 : Wi-Fi selon l'alimentation
Comment=Coupe l'économie d'énergie du Wi-Fi sur secteur (flux GL310 fluide), la remet sur batterie
Exec=$WIFI_BIN
NoDisplay=true
X-GNOME-Autostart-enabled=true
EOF
pkill -f "^/bin/sh $WIFI_BIN" 2>/dev/null || true
nohup "$WIFI_BIN" >/dev/null 2>&1 &

echo "==> Vérifications"
missing=""
for cmd in ffmpeg gst-launch-1.0 pactl pw-cat pw-link xdg-user-dir; do
	command -v "$cmd" >/dev/null || missing="$missing $cmd"
done
gst-inspect-1.0 pipewiresink >/dev/null 2>&1 || missing="$missing gstreamer1.0-pipewire"
[ -z "$missing" ] || echo "Manque :$missing (sudo apt install ffmpeg gstreamer1.0-pipewire pipewire-bin pulseaudio-utils)"
[ -f "$RULE" ] || echo "Accès USB sans root absent : lance une fois  bash $ROOT/tools/setup-phase1.sh"

echo "GL310 Studio est installé : menu > Son et vidéo > GL310 Studio."

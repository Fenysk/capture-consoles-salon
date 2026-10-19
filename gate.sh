#!/bin/sh
# Preuve unique qu'un changement est fini : doit sortir en 0 avant chaque commit. S'arrête au premier échec.
set -eu
ROOT=$(cd "$(dirname "$0")" && pwd)
cd "$ROOT"
CARGO=${CARGO:-$HOME/.cargo/bin/cargo}
[ -x "$CARGO" ] || CARGO=cargo

step() { printf '\n==> %s\n' "$1"; }

step "Infos perso (dépôt public)"
sh tools/check-public.sh

step "GL310 Studio : tests"
"$CARGO" test --quiet --manifest-path studio-rs/Cargo.toml

step "GL310 Studio : clippy"
"$CARGO" clippy --quiet --all-targets --manifest-path studio-rs/Cargo.toml -- -D warnings

step "Mode console OBS"
sh obs/run-tests.sh

step "Prototype Python"
python3 -m unittest -q studio.test_model

step "Mesure du délai"
(cd tools && python3 -m unittest -q test_latency_probe)

step "Pilote C : modèle"
make -s -C driver/gl310 test

printf '\ngate : vert\n'

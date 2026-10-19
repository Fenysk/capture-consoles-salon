#!/bin/sh
# Lance les tests de mode-console.lua sans OBS. L'interpréteur Lua n'est pas installé sur ce PC, mais la
# bibliothèque liblua5.4 l'est : on compile un lanceur minimal (lua54.c) la première fois.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
RUNNER=${TMPDIR:-/tmp}/gl310-lua54
[ -x "$RUNNER" ] || cc -O2 -o "$RUNNER" "$HERE/lua54.c" /usr/lib/x86_64-linux-gnu/liblua5.4.so.0
cd "$HERE"
printf 'arg_script = "%s/mode-console.lua"\n' "$HERE" > "${TMPDIR:-/tmp}/gl310-lua-prelude.lua"
cat "${TMPDIR:-/tmp}/gl310-lua-prelude.lua" test_mode_console.lua > "${TMPDIR:-/tmp}/gl310-lua-test.lua"
"$RUNNER" "${TMPDIR:-/tmp}/gl310-lua-test.lua"

/* Lanceur Lua minimal (la bibliothèque liblua5.4 est installée, pas l'interpréteur). */
#include <stdint.h>
#include <stdio.h>
typedef struct lua_State lua_State;
lua_State *luaL_newstate(void);
void luaL_openlibs(lua_State *L);
int luaL_loadfilex(lua_State *L, const char *filename, const char *mode);
int lua_pcallk(lua_State *L, int nargs, int nresults, int errfunc, intptr_t ctx, void *k);
const char *lua_tolstring(lua_State *L, int idx, size_t *len);
int main(int argc, char **argv)
{
	lua_State *L = luaL_newstate();
	luaL_openlibs(L);
	if (argc < 2 || luaL_loadfilex(L, argv[1], NULL) || lua_pcallk(L, 0, 0, 0, 0, NULL)) {
		fprintf(stderr, "%s\n", argc < 2 ? "usage : lua54 fichier.lua" : lua_tolstring(L, -1, NULL));
		return 1;
	}
	return 0;
}

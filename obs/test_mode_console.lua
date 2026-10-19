-- Tests de mode-console.lua, sans OBS : un faux « obslua » simule sources, scènes, filtres, décalages et raccourci.
-- Lancer : obs/run-tests.sh
-- Convention testée : la scène « Synchro jeu » est une liste (jamais affichée) ; chaque source qu'elle contient, même
-- dans une scène imbriquée, est retardée elle-même en mode console, donc partout où elle apparaît, sans toucher aux
-- mises en page : image asynchrone (webcam, vidéos) par « Délai vidéo (asynchrone) », qui retarde aussi son son ;
-- image synchrone (navigateur) par « Délai de rendu » + décalage du son ; son seul par décalage.
-- Le jeu (Source média qui lit …/stream.ts) n'est jamais retardé ; une seule valeur à régler, le délai.

local VIDEO, AUDIO, ASYNC = 1, 2, 4
local SYNC_SCENE = "Synchro jeu"
local GAME_URL = "http://portable.local:4848/stream.ts"
local sources, by_uuid, logs, hotkey_cb, event_cb

local function add(name, flags, opts)
	opts = opts or {}
	local s = {
		name = name, uuid = "uuid-" .. name, flags = flags, filters = {}, offset = opts.offset or 0, enabled = true,
		items = opts.items, id = opts.id or (opts.items and "scene" or "input"), settings = { values = opts.settings or {} },
	}
	sources[name], by_uuid[s.uuid] = s, s
	return s
end

local function reset_world()
	sources, by_uuid, logs, hotkey_cb, event_cb = {}, {}, {}, nil, nil
	add("Webcam", VIDEO + ASYNC + AUDIO)
	add("Micro", AUDIO, { offset = 100 * 1000000 }) -- réglage existant : 100 ms
	add("Jean-Renard", VIDEO + AUDIO, { id = "browser_source" })
	add("Bruitages", AUDIO)
	add("Musique", AUDIO)
	add("Média", VIDEO + ASYNC + AUDIO, { id = "ffmpeg_source", settings = { input = GAME_URL } })
	add("Témoin mode console", VIDEO)
	add("Toi", VIDEO, { items = { "Jean-Renard", "Micro" } })           -- Micro aussi imbriqué : décalé une fois
	add(SYNC_SCENE, VIDEO, { items = { "Webcam", "Micro", "Toi", "Bruitages" } })
	add("Jeu", VIDEO, { items = { SYNC_SCENE, "Média", "Musique" } })
end

local function new_data() return { values = {} } end

obslua = {
	OBS_SOURCE_VIDEO = VIDEO, OBS_SOURCE_AUDIO = AUDIO, OBS_SOURCE_ASYNC = ASYNC,
	LOG_INFO = 300, LOG_WARNING = 200, OBS_TEXT_DEFAULT = 0, OBS_TEXT_INFO = 3,
	OBS_FRONTEND_EVENT_FINISHED_LOADING = 26, OBS_FRONTEND_EVENT_EXIT = 17,
	OBS_FRONTEND_EVENT_SCENE_COLLECTION_CHANGING = 31,
	script_log = function(_, msg) logs[#logs + 1] = msg end,
	obs_get_source_by_name = function(name) return sources[name] end,
	obs_get_source_by_uuid = function(uuid) return by_uuid[uuid] end,
	obs_source_release = function() end,
	obs_source_get_name = function(s) return s.name end,
	obs_source_get_uuid = function(s) return s.uuid end,
	obs_source_get_unversioned_id = function(s) return s.id end,
	obs_source_get_output_flags = function(s) return s.flags end,
	obs_source_get_settings = function(s) return s.settings end,
	obs_enum_sources = function()
		local list = {}
		for _, s in pairs(sources) do if not s.items then list[#list + 1] = s end end
		return list
	end,
	source_list_release = function() end,
	obs_frontend_get_scenes = function()
		local list = {}
		for _, s in pairs(sources) do if s.items then list[#list + 1] = s end end
		return list
	end,
	obs_scene_from_source = function(s) return s.items and s or nil end,
	obs_group_from_source = function() return nil end,
	obs_scene_enum_items = function(scene)
		local list = {}
		for i, name in ipairs(scene.items) do list[i] = { source = sources[name] } end
		return list
	end,
	sceneitem_list_release = function() end,
	obs_sceneitem_get_source = function(item) return item.source end,
	obs_source_get_filter_by_name = function(s, name) return s.filters[name] end,
	obs_source_create_private = function(id, name, data) return { id = id, name = name, delay = data.values.delay_ms } end,
	obs_source_filter_add = function(s, f) s.filters[f.name] = f end,
	obs_source_filter_remove = function(s, f) s.filters[f.name] = nil end,
	obs_source_update = function(f, data) f.delay = data.values.delay_ms end,
	obs_source_get_sync_offset = function(s) return s.offset end,
	obs_source_set_sync_offset = function(s, v) s.offset = v end,
	obs_source_set_enabled = function(s, on) s.enabled = on end,
	obs_data_create = new_data,
	obs_data_release = function() end,
	obs_data_set_int = function(d, k, v) d.values[k] = v end,
	obs_data_get_int = function(d, k) return d.values[k] or d.values["default:" .. k] or 0 end,
	obs_data_set_bool = function(d, k, v) d.values[k] = v end,
	obs_data_get_bool = function(d, k) return d.values[k] == true end,
	obs_data_set_string = function(d, k, v) d.values[k] = v end,
	obs_data_get_string = function(d, k) return d.values[k] or d.values["default:" .. k] or "" end,
	obs_data_set_default_int = function(d, k, v) d.values["default:" .. k] = v end,
	obs_data_set_default_string = function(d, k, v) d.values["default:" .. k] = v end,
	obs_data_erase = function(d, k) d.values[k] = nil end,
	obs_data_get_array = function(d, k) return d.values[k] or {} end,
	obs_data_set_array = function(d, k, a) d.values[k] = a end,
	obs_data_array_release = function() end,
	obs_hotkey_register_frontend = function(_, _, cb) hotkey_cb = cb return 1 end,
	obs_hotkey_load = function() end,
	obs_hotkey_save = function() return {} end,
	obs_frontend_add_event_callback = function(cb) event_cb = cb end,
	obs_frontend_remove_event_callback = function() end,
}
bit = { band = function(a, b) return a & b end }

local failures, checks = 0, 0
local function check(what, cond)
	checks = checks + 1
	if not cond then
		failures = failures + 1
		print("ÉCHEC " .. what)
	end
end

local FILTER = "Mode console"
local MS = 1000000
local function delay_of(name, id)
	local f = sources[name].filters[FILTER]
	return f and f.id == id and f.delay or nil
end
local function scene_delay(name) return delay_of(name, "gpu_delay") end
local function logged(...)
	local words = { ... }
	for _, line in ipairs(logs) do
		local all = true
		for _, w in ipairs(words) do all = all and line:find(w, 1, true) ~= nil end
		if all then return true end
	end
	return false
end

-- Charge le script dans un monde neuf (ou celui donné), comme OBS au démarrage.
local function load(values, keep_world)
	if not keep_world then reset_world() end
	local settings = new_data()
	for k, v in pairs(values or {}) do settings.values[k] = v end
	dofile(arg_script)
	script_defaults(settings)
	script_load(settings)
	return settings
end

-- Réglages par défaut : la scène « Synchro jeu », un délai réglable.
local settings = load({})
check("scène par défaut : Synchro jeu", settings.values["default:scene"] == SYNC_SCENE)
check("délai par défaut entre 1500 et 3000 ms", (settings.values["default:delay_ms"] or 0) >= 1500
	and settings.values["default:delay_ms"] <= 3000)
check("témoin éteint au départ", sources["Témoin mode console"].enabled == false)

-- Quand on appuie sur le raccourci, chaque source de la liste est retardée par la bonne méthode, une seule fois.
settings = load({ delay_ms = 2000 })
hotkey_cb(true)
check("webcam : délai vidéo asynchrone 2000 ms", delay_of("Webcam", "async_delay_filter") == 2000)
check("webcam : pas de décalage en plus (le filtre retarde déjà son son)", sources["Webcam"].offset == 0)
check("micro (présent deux fois) : 100 + 2000 ms, une seule fois", sources["Micro"].offset == 2100 * MS)
check("Jean-Renard (scène imbriquée) : délai de rendu 2000 ms", delay_of("Jean-Renard", "gpu_delay") == 2000)
check("Jean-Renard : son décalé de 2000 ms", sources["Jean-Renard"].offset == 2000 * MS)
check("bruitages : son décalé", sources["Bruitages"].offset == 2000 * MS)
check("aucune scène retardée (mises en page intactes)", scene_delay(SYNC_SCENE) == nil and scene_delay("Toi") == nil)
check("jeu jamais touché", next(sources["Média"].filters) == nil and sources["Média"].offset == 0)
check("hors de la liste (musique, scène Jeu) : rien", sources["Musique"].offset == 0 and scene_delay("Jeu") == nil)
check("témoin allumé", sources["Témoin mode console"].enabled == true)
hotkey_cb(false)
check("raccourci relâché : rien ne change", sources["Micro"].offset == 2100 * MS)

-- Quand on rappuie, tout revient exactement comme avant.
hotkey_cb(true)
for name, s in pairs(sources) do check(name .. " : plus de filtre du mode console", s.filters[FILTER] == nil) end
check("micro : 100 ms d'origine", sources["Micro"].offset == 100 * MS)
check("webcam, Jean-Renard, bruitages : 0", sources["Webcam"].offset == 0 and sources["Jean-Renard"].offset == 0
	and sources["Bruitages"].offset == 0)
check("témoin éteint", sources["Témoin mode console"].enabled == false)

-- Quand une nouvelle caméra est ajoutée à la scène, elle est prise en compte sans rien configurer.
settings = load({ delay_ms = 2000 })
add("Caméra mobile", VIDEO + ASYNC + AUDIO)
table.insert(sources[SYNC_SCENE].items, "Caméra mobile")
hotkey_cb(true)
check("nouvelle caméra : délai vidéo asynchrone", delay_of("Caméra mobile", "async_delay_filter") == 2000)

-- Quand une source est renommée pendant le mode console, elle est quand même remise comme avant.
sources["Webcam Logitech"], sources["Webcam"] = sources["Webcam"], nil
sources["Webcam Logitech"].name = "Webcam Logitech"
sources[SYNC_SCENE].items[1] = "Webcam Logitech"
hotkey_cb(true)
check("source renommée : filtre retiré", sources["Webcam Logitech"].filters[FILTER] == nil)
check("caméra mobile : filtre retiré", sources["Caméra mobile"].filters[FILTER] == nil)

-- Quand le délai change pendant le mode console, les retards suivent sans s'additionner.
settings = load({ delay_ms = 2000 })
hotkey_cb(true)
settings.values.delay_ms = 2500
script_update(settings)
check("webcam suit le nouveau délai", delay_of("Webcam", "async_delay_filter") == 2500)
check("Jean-Renard suit le nouveau délai", delay_of("Jean-Renard", "gpu_delay") == 2500)
check("micro : 100 + 2500 ms", sources["Micro"].offset == 2600 * MS)
hotkey_cb(true)
check("micro rendu à 100 ms", sources["Micro"].offset == 100 * MS)

-- Si la scène de synchro contient le jeu, alors rien n'est retardé et le journal d'OBS l'explique.
settings = load({ delay_ms = 2000, scene = "Jeu" })
hotkey_cb(true)
check("liste contenant le jeu : rien de retardé", sources["Webcam"].filters[FILTER] == nil and sources["Musique"].offset == 0)
check("scène contenant le jeu : avertissement", logged("Jeu", "stream.ts"))
check("scène contenant le jeu : témoin reste éteint", sources["Témoin mode console"].enabled == false)

-- Si la scène de synchro n'existe pas, alors rien n'est retardé et le journal le dit.
settings = load({ delay_ms = 2000, scene = "Inexistante" })
hotkey_cb(true)
check("scène absente : avertissement", logged("Inexistante"))
check("scène absente : rien de touché", sources["Micro"].offset == 100 * MS)

-- Si OBS a été fermé en mode console, alors au rechargement tout est remis comme avant.
settings = load({ delay_ms = 2000 })
hotkey_cb(true)
local saved = settings.values
reset_world()
sources["Micro"].offset = 2100 * MS -- état laissé par la fermeture
sources["Jean-Renard"].offset = 2000 * MS
sources["Webcam"].filters[FILTER] = { id = "async_delay_filter", name = FILTER, delay = 2000 }
sources["Jean-Renard"].filters[FILTER] = { id = "gpu_delay", name = FILTER, delay = 2000 }
settings = load(saved, true)
event_cb(obslua.OBS_FRONTEND_EVENT_FINISHED_LOADING)
check("rechargement : filtres retirés", sources["Webcam"].filters[FILTER] == nil and sources["Jean-Renard"].filters[FILTER] == nil)
check("rechargement : micro rendu à 100 ms", sources["Micro"].offset == 100 * MS)
check("rechargement : Jean-Renard rendu à 0", sources["Jean-Renard"].offset == 0)
check("rechargement : mode console désactivé", settings.values.active ~= true)

print(failures == 0 and ("Tous les tests passent (" .. checks .. " vérifications).")
	or (failures .. " échec(s) sur " .. checks .. " vérifications."))
os.exit(failures == 0 and 0 or 1)

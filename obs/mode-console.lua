--[[ Mode console pour OBS : un seul raccourci qui fait suivre au reste du stream le jeu venu du boîtier GL310,
lequel arrive avec ~2 s de retard (encodage dans le boîtier, réseau, OBS).

Convention, rien d'autre à configurer :
  - la scène « Synchro jeu » est une liste, qu'on n'affiche jamais : chaque source qu'elle contient (même dans une
    scène imbriquée, une seule fois même si elle y est deux fois) est retardée elle-même en mode console, donc partout
    où elle apparaît, sans toucher aux mises en page :
      image asynchrone (webcam, vidéo, capture) : filtre « Délai vidéo (asynchrone) », qui retarde aussi son son ;
      image synchrone (navigateur, image)       : filtre « Délai de rendu » + décalage de synchronisation du son ;
      son seul (micro, sons)                    : décalage de synchronisation ;
  - le jeu (Source média qui lit …/stream.ts, c'est-à-dire GL310 Studio) n'est jamais retardé : si la scène le
    contient, le script refuse et l'écrit dans le journal d'OBS ;
  - une source nommée « Témoin mode console », si elle existe, n'est active qu'en mode console.
Nouvelle caméra : l'ajouter à la scène (Ajouter > source existante). Source renommée : aucune importance (repérée par son identifiant).
Second appui : tout revient exactement comme avant. OBS fermé en mode console : tout est remis au démarrage suivant.

Déclencheurs : raccourci « Mode console : activer / désactiver » (Paramètres > Raccourcis clavier, ou Stream Deck),
ou une app web par obs-websocket : TriggerHotkeyByName { hotkeyName = "mode_console.toggle" }.
Le « Délai de rendu » au-delà de 500 ms n'est réglable que par script ; il garde les images de la source en mémoire
vidéo (~1,2 Go pour 2,5 s d'une source 1920×1080 à 60 i/s).
Écrit pour la LuaJIT d'OBS (Lua 5.1) ; testé sans OBS par test_mode_console.lua.
]]
obs = obslua

local FILTER = "Mode console"
local INDICATOR = "Témoin mode console"
local HOTKEY = "mode_console.toggle"
local GAME_SUFFIX = "/stream.ts"
local MS = 1000000 -- les décalages de synchronisation sont en nanosecondes

local settings_ref
local hotkey_id
local live = false -- activé pendant cette session (script_update ne réapplique que dans ce cas)

local function log(level, text)
	obs.script_log(level, "[mode console] " .. text)
end

local function has(source, flag)
	return bit.band(obs.obs_source_get_output_flags(source), flag) ~= 0
end

local function is_game(source)
	if obs.obs_source_get_unversioned_id(source) ~= "ffmpeg_source" then
		return false
	end
	local data = obs.obs_source_get_settings(source)
	local input = obs.obs_data_get_string(data, "input")
	obs.obs_data_release(data)
	return input:sub(-#GAME_SUFFIX) == GAME_SUFFIX
end

local function sub_scene(source)
	return obs.obs_scene_from_source(source) or obs.obs_group_from_source(source)
end

-- Sources (hors scènes) contenues dans la scène et ses scènes imbriquées, chacune une seule fois.
local function collect(scene_source, found, seen)
	seen[obs.obs_source_get_uuid(scene_source)] = true
	local items = obs.obs_scene_enum_items(sub_scene(scene_source))
	for _, item in ipairs(items or {}) do
		local source = obs.obs_sceneitem_get_source(item)
		local uuid = obs.obs_source_get_uuid(source)
		if not seen[uuid] then
			seen[uuid] = true
			if sub_scene(source) then
				collect(source, found, seen)
			else
				found[#found + 1] = source
			end
		end
	end
	obs.sceneitem_list_release(items)
	return found
end

local function set_indicator(on)
	local source = obs.obs_get_source_by_name(INDICATOR)
	if source then
		obs.obs_source_set_enabled(source, on)
		obs.obs_source_release(source)
	end
end

local function add_delay_filter(source, id, delay_ms)
	local data = obs.obs_data_create()
	obs.obs_data_set_int(data, "delay_ms", delay_ms)
	local filter = obs.obs_source_get_filter_by_name(source, FILTER)
	if filter then
		obs.obs_source_update(filter, data)
	else
		filter = obs.obs_source_create_private(id, FILTER, data)
		obs.obs_source_filter_add(source, filter)
	end
	obs.obs_source_release(filter)
	obs.obs_data_release(data)
end

local function remove_delay_filter(source)
	local filter = obs.obs_source_get_filter_by_name(source, FILTER)
	if filter then
		obs.obs_source_filter_remove(source, filter)
		obs.obs_source_release(filter)
	end
end

-- Identifiants des sources dont le décalage d'origine est mémorisé (dans les réglages du script, pour survivre
-- à une fermeture d'OBS en mode console).
local function saved_uuids()
	local list = {}
	for uuid in obs.obs_data_get_string(settings_ref, "saved"):gmatch("[^,]+") do
		list[#list + 1] = uuid
	end
	return list
end

-- Remet tout comme avant : retire le filtre du mode console de toutes les sources et scènes (même renommées ou
-- sorties de la liste entre-temps), rend les décalages d'origine.
local function deactivate()
	for _, list in ipairs({ obs.obs_enum_sources(), obs.obs_frontend_get_scenes() }) do
		for _, source in ipairs(list or {}) do
			remove_delay_filter(source)
		end
		obs.source_list_release(list)
	end
	for _, uuid in ipairs(saved_uuids()) do
		local source = obs.obs_get_source_by_uuid(uuid)
		if source then
			obs.obs_source_set_sync_offset(source, obs.obs_data_get_int(settings_ref, "orig:" .. uuid))
			obs.obs_source_release(source)
		end
		obs.obs_data_erase(settings_ref, "orig:" .. uuid)
	end
	obs.obs_data_set_string(settings_ref, "saved", "")
	obs.obs_data_set_bool(settings_ref, "active", false)
	set_indicator(false)
	live = false
end

local function activate()
	local name = obs.obs_data_get_string(settings_ref, "scene")
	local delay = obs.obs_data_get_int(settings_ref, "delay_ms")
	local scene_source = obs.obs_get_source_by_name(name)
	if not scene_source or not sub_scene(scene_source) then
		log(obs.LOG_WARNING, "scène « " .. name .. " » introuvable : rien n'est retardé")
		if scene_source then
			obs.obs_source_release(scene_source)
		end
		return
	end
	local found = collect(scene_source, {}, {})
	for _, source in ipairs(found) do
		if is_game(source) then
			log(obs.LOG_WARNING, "la scène « " .. name .. " » contient le jeu (« " .. obs.obs_source_get_name(source)
				.. " », …" .. GAME_SUFFIX .. ") : rien n'est retardé, retire le jeu de cette scène")
			obs.obs_source_release(scene_source)
			return
		end
	end
	local saved = {}
	for _, source in ipairs(found) do
		local async_video = has(source, obs.OBS_SOURCE_VIDEO) and has(source, obs.OBS_SOURCE_ASYNC)
		if async_video then
			add_delay_filter(source, "async_delay_filter", delay) -- retarde aussi le son de la source
		elseif has(source, obs.OBS_SOURCE_VIDEO) then
			add_delay_filter(source, "gpu_delay", delay)
		end
		if has(source, obs.OBS_SOURCE_AUDIO) and not async_video then
			local uuid = obs.obs_source_get_uuid(source)
			local original = obs.obs_source_get_sync_offset(source)
			obs.obs_data_set_int(settings_ref, "orig:" .. uuid, original)
			obs.obs_source_set_sync_offset(source, original + delay * MS)
			saved[#saved + 1] = uuid
		end
	end
	obs.obs_data_set_string(settings_ref, "saved", table.concat(saved, ","))
	obs.obs_data_set_bool(settings_ref, "active", true)
	set_indicator(true)
	live = true
	log(obs.LOG_INFO, "activé : " .. #found .. " source(s) de « " .. name .. " » retardée(s) de " .. delay .. " ms")
	obs.obs_source_release(scene_source)
end

local function toggle()
	if obs.obs_data_get_bool(settings_ref, "active") then
		deactivate()
		log(obs.LOG_INFO, "désactivé : tout est remis comme avant")
	else
		activate()
	end
end

-- OBS déjà chargé (script ajouté ou rechargé à la main) : les scènes existent.
local function obs_loaded()
	local scenes = obs.obs_frontend_get_scenes()
	local loaded = scenes ~= nil and #scenes > 0
	obs.source_list_release(scenes)
	return loaded
end

-- Après une fermeture en mode console, ou avant de quitter / changer de collection de scènes : tout remettre.
local function clean_up()
	if obs.obs_data_get_bool(settings_ref, "active") then
		deactivate()
		log(obs.LOG_INFO, "mode console laissé actif : tout est remis comme avant")
	else
		set_indicator(false)
	end
end

local function on_event(event)
	if event == obs.OBS_FRONTEND_EVENT_FINISHED_LOADING then
		clean_up()
	elseif event == obs.OBS_FRONTEND_EVENT_EXIT or event == obs.OBS_FRONTEND_EVENT_SCENE_COLLECTION_CHANGING then
		if live then
			deactivate()
		end
	end
end

function script_description()
	return [[<b>Mode console</b> : un raccourci retarde chaque source rangée dans la scène <b>Synchro jeu</b> (image et
son, partout où elle apparaît), pour la caler sur le jeu du boîtier GL310, qui arrive en retard. Second appui : tout
revient comme avant. La scène « Synchro jeu » n'est qu'une liste : inutile de l'afficher.<br>
Raccourci : Paramètres &gt; Raccourcis clavier &gt; « Mode console : activer / désactiver ».<br>
App web (obs-websocket) : TriggerHotkeyByName « mode_console.toggle ».]]
end

function script_properties()
	local props = obs.obs_properties_create()
	local delay = obs.obs_properties_add_int(props, "delay_ms", "Délai", 0, 20000, 50)
	obs.obs_property_int_set_suffix(delay, " ms")
	obs.obs_properties_add_text(props, "scene", "Scène liste", obs.OBS_TEXT_DEFAULT)
	obs.obs_properties_add_button(props, "toggle", "Activer / désactiver maintenant", function()
		toggle()
		return true
	end)
	return props
end

function script_defaults(settings)
	obs.obs_data_set_default_int(settings, "delay_ms", 2000)
	obs.obs_data_set_default_string(settings, "scene", "Synchro jeu")
end

function script_update(settings)
	settings_ref = settings
	if live then -- nouveau délai ou nouvelle scène pendant le mode console : réappliquer sans cumuler
		deactivate()
		activate()
	end
end

function script_load(settings)
	settings_ref = settings
	hotkey_id = obs.obs_hotkey_register_frontend(HOTKEY, "Mode console : activer / désactiver", function(pressed)
		if pressed then
			toggle()
		end
	end)
	local keys = obs.obs_data_get_array(settings, "hotkey")
	obs.obs_hotkey_load(hotkey_id, keys)
	obs.obs_data_array_release(keys)
	obs.obs_frontend_add_event_callback(on_event)
	if obs_loaded() then
		clean_up()
	end
end

function script_save(settings)
	local keys = obs.obs_hotkey_save(hotkey_id)
	obs.obs_data_set_array(settings, "hotkey", keys)
	obs.obs_data_array_release(keys)
end

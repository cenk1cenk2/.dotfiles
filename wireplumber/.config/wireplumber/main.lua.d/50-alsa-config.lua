rule = {
	matches = {
		{
			{ "alsa.card_name", "equals", "Scarlett 8i6 USB" },
			{ "node.name", "matches", "alsa_output.*" },
		},
	},
	apply_properties = {
		-- ["api.alsa.use-acp"] = false,
		-- ["api.alsa.use-ucm"] = true,
		["audio.format"] = "S24_LE",
		["audio.rate"] = 96000,
		["clock.rate"] = 96000,
		-- ["clock.force-rate"] = 96000,
		-- ["api.alsa.period-size"] = 128,
	},
}

table.insert(alsa_monitor.rules, rule)

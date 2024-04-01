rule = {
	matches = {
		{
			{ "alsa.card_name", "equals", "Scarlett 8i6 USB" },
		},
	},
	apply_properties = {
		-- ["api.alsa.use-acp"] = false,
		-- ["api.alsa.use-ucm"] = true,
		["audio.format"] = "S32_LE",
		["audio.rate"] = 96000,
		["clock.rate"] = 96000,
		-- ["clock.force-rate"] = 96000,
		-- ["api.alsa.period-size"] = 128,
	},
}

table.insert(alsa_monitor.rules, rule)

// Adds every model the gateway lists for this API key to the kilic provider, with the
// limits the gateway reports. It hooks the model domain rather than the provider domain:
// user plugins run before the config provider plugin, so a provider transform cannot see
// kilic yet, while the model domain is rebuilt from the finished provider snapshot.
export default {
  id: "kilic.gateway-discovery",
  async setup(context) {
    const providerID = context.options.provider ?? "kilic"
    const baseURL = (context.options.baseURL ?? "https://ai.kilic.dev/v1").replace(/\/+$/, "")
    const apiKey = process.env[context.options.apiKeyEnv ?? "AI_KILIC_DEV_API_KEY"]

    let entries = []
    try {
      const response = await fetch(`${baseURL}/models`, {
        headers: apiKey ? { Authorization: `Bearer ${apiKey}` } : {},
        signal: AbortSignal.timeout(5_000),
      })
      if (!response.ok) throw new Error(`HTTP ${response.status}`)
      const body = await response.json()
      // "#" starts a variant in a "provider/model#variant" reference, and "*" is a
      // wildcard the gateway lists verbatim while its catalog has no ids for it.
      entries = (body.data ?? []).filter((e) => typeof e?.id === "string" && e.id && !/[#*]/.test(e.id))
    } catch (error) {
      // The configured models still load; a failed discovery never fails plugin setup.
      console.warn(`[${providerID}] model discovery skipped: ${error.message}`)
      return
    }

    const positive = (value) => (Number.isInteger(value) && value > 0 ? value : undefined)
    const registration = await context.model.transform((editor) => {
      for (const entry of entries) {
        // A configured model keeps its own name, settings and variants.
        if (editor.get(providerID, entry.id)) continue
        editor.update(providerID, entry.id, (model) => {
          const contextLimit = positive(entry.context_length) ?? positive(entry.max_model_len)
          const outputLimit =
            positive(entry.max_output_tokens) ?? (contextLimit && Math.min(8192, Math.floor(contextLimit / 4)))
          if (contextLimit) model.limit.context = contextLimit
          if (outputLimit) model.limit.output = outputLimit
        })
      }
    })
    return () => registration.dispose()
  },
}

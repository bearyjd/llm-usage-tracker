export interface PluginConfig {
  /** Backend URL (default: http://127.0.0.1:8080) */
  backendUrl: string
  /** Show toast on session.idle (default: true) */
  showOnIdle: boolean
  /** Show toast on session.compacted (default: true) */
  showOnCompact: boolean
  /** Toast duration in ms (default: 8000) */
  toastDurationMs: number
  /** Minimum interval between toasts in ms (default: 30000) */
  minIntervalMs: number
  /** Request timeout to backend in ms (default: 5000) */
  timeoutMs: number
}

const DEFAULT_CONFIG: PluginConfig = {
  backendUrl: "http://127.0.0.1:8080",
  showOnIdle: true,
  showOnCompact: true,
  toastDurationMs: 8000,
  minIntervalMs: 30_000,
  timeoutMs: 5000,
}

/**
 * Load config from environment variables.
 * In a future version, this could also read from opencode.json experimental config.
 */
declare const Bun: { env: Record<string, string | undefined> } | undefined

export function loadConfig(): PluginConfig {
  const env = (key: string): string | undefined => {
    try {
      if (typeof Bun !== "undefined") return Bun.env[key]
      return (globalThis as Record<string, unknown>).process
        ? ((globalThis as Record<string, unknown>).process as { env: Record<string, string | undefined> }).env[key]
        : undefined
    } catch {
      return undefined
    }
  }

  return {
    backendUrl: env("LLM_TRACKER_URL") ?? DEFAULT_CONFIG.backendUrl,
    showOnIdle: env("LLM_TRACKER_SHOW_ON_IDLE") !== "false",
    showOnCompact: env("LLM_TRACKER_SHOW_ON_COMPACT") !== "false",
    toastDurationMs: Number(env("LLM_TRACKER_TOAST_DURATION")) || DEFAULT_CONFIG.toastDurationMs,
    minIntervalMs: Number(env("LLM_TRACKER_MIN_INTERVAL")) || DEFAULT_CONFIG.minIntervalMs,
    timeoutMs: Number(env("LLM_TRACKER_TIMEOUT")) || DEFAULT_CONFIG.timeoutMs,
  }
}

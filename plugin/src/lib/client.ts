export interface Snapshot {
  id: number
  provider: string
  source: "subscription" | "api"
  collected_at: string
  // Subscription
  messages_used: number | null
  messages_limit: number | null
  messages_window_hours: number | null
  messages_reset_at: string | null
  // API
  api_spend_usd: number | null
  api_spend_period: string | null
  tokens_input: number | null
  tokens_output: number | null
  tokens_period: string | null
  // Meta
  model_tier: string | null
}

export interface Recommendation {
  provider: string
  message: string
  action: "use" | "warn" | "avoid" | "wait" | "unknown"
  priority: number
}

export interface SpendSummary {
  [provider: string]: {
    spend_usd: number | null
    period: string | null
    tokens_input: number | null
    tokens_output: number | null
    collected_at: string
  }
}

export interface BackendConfig {
  litellm_configured: boolean
  litellm_base_url: string | null
  sessions: Record<string, { subscription: boolean; api: boolean }>
  openai_key_set: boolean
  google_key_set: boolean
}

export interface HealthResponse {
  status: string
  timestamp: string
}

export class TrackerClient {
  private baseUrl: string
  private timeoutMs: number

  constructor(baseUrl: string = "http://127.0.0.1:48372", timeoutMs: number = 5000) {
    this.baseUrl = baseUrl.replace(/\/+$/, "")
    this.timeoutMs = timeoutMs
  }

  private async fetch<T>(path: string): Promise<T> {
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), this.timeoutMs)

    try {
      const resp = await fetch(`${this.baseUrl}${path}`, {
        signal: controller.signal,
        headers: { Accept: "application/json" },
      })
      if (!resp.ok) {
        throw new Error(`Backend HTTP ${resp.status}: ${resp.statusText}`)
      }
      return (await resp.json()) as T
    } catch (err: unknown) {
      if (err instanceof DOMException && err.name === "AbortError") {
        throw new Error(`Backend timeout (${this.timeoutMs}ms) — is llm-tracker serve running?`)
      }
      throw err
    } finally {
      clearTimeout(timer)
    }
  }

  /** Check if the backend is reachable. */
  async health(): Promise<HealthResponse> {
    return this.fetch<HealthResponse>("/api/health")
  }

  /** Returns true if backend is reachable, false otherwise. */
  async isAlive(): Promise<boolean> {
    try {
      await this.health()
      return true
    } catch {
      return false
    }
  }

  /** Latest snapshot per (provider, source). Primary dashboard endpoint. */
  async status(): Promise<Snapshot[]> {
    return this.fetch<Snapshot[]>("/api/status")
  }

  /** Ranked provider recommendations. */
  async recommend(): Promise<Recommendation[]> {
    return this.fetch<Recommendation[]>("/api/recommend")
  }

  /** API spend summary per provider for last N days. */
  async spendSummary(days: number = 30): Promise<SpendSummary> {
    return this.fetch<SpendSummary>(`/api/spend/summary?days=${days}`)
  }

  /** Backend configuration status. */
  async config(): Promise<BackendConfig> {
    return this.fetch<BackendConfig>("/api/config")
  }

  /** Trigger a fresh data collection in the background. */
  async triggerCollect(providers?: string[]): Promise<void> {
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), this.timeoutMs)

    try {
      const body = providers ? { providers } : undefined
      await fetch(`${this.baseUrl}/api/collect`, {
        method: "POST",
        signal: controller.signal,
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json",
        },
        body: body ? JSON.stringify(body) : undefined,
      })
    } finally {
      clearTimeout(timer)
    }
  }
}

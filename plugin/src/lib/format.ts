import type { Snapshot, Recommendation, SpendSummary } from "./client"

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function pct(used: number, limit: number): number {
  return limit > 0 ? Math.round((used / limit) * 100) : 0
}

function fmtMinutes(minutesStr: string | null): string {
  if (!minutesStr) return "?"
  const resetAt = new Date(minutesStr)
  const now = new Date()
  const diffMs = resetAt.getTime() - now.getTime()
  if (diffMs <= 0) return "now"
  const mins = Math.round(diffMs / 60_000)
  if (mins < 60) return `${mins}m`
  const h = Math.floor(mins / 60)
  const m = mins % 60
  return `${h}h ${String(m).padStart(2, "0")}m`
}

function fmtTokens(n: number | null): string {
  if (n === null || n === undefined) return "?"
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`
  return String(n)
}

function fmtSpend(usd: number | null): string {
  if (usd === null || usd === undefined) return "?"
  if (usd < 0.01) return `$${usd.toFixed(4)}`
  if (usd < 1) return `$${usd.toFixed(3)}`
  return `$${usd.toFixed(2)}`
}

function statusIcon(pctUsed: number): string {
  if (pctUsed >= 90) return "\u2717" // ✗
  if (pctUsed >= 80) return "\u26a0" // ⚠
  return "\u2713" // ✓
}

function providerName(p: string): string {
  const names: Record<string, string> = {
    claude: "Claude",
    chatgpt: "ChatGPT",
    gemini: "Gemini",
    groq: "Groq",
  }
  return names[p] ?? p.charAt(0).toUpperCase() + p.slice(1)
}

// ---------------------------------------------------------------------------
// Toast format (compact — shown as notification)
// ---------------------------------------------------------------------------

export function formatToast(snapshots: Snapshot[]): string {
  const sub = snapshots.filter((s) => s.source === "subscription")
  const api = snapshots.filter((s) => s.source === "api")

  const lines: string[] = []

  // Subscription headroom
  for (const s of sub) {
    if (s.messages_used !== null && s.messages_limit !== null) {
      const p = pct(s.messages_used, s.messages_limit)
      const remaining = s.messages_limit - s.messages_used
      const reset = fmtMinutes(s.messages_reset_at)
      lines.push(`${statusIcon(p)} ${providerName(s.provider)}: ${remaining} left (${p}%) resets ${reset}`)
    }
  }

  // API spend (one-liner summary)
  if (api.length > 0) {
    const totalSpend = api.reduce((sum, s) => sum + (s.api_spend_usd ?? 0), 0)
    if (totalSpend > 0) {
      lines.push(`API: ${fmtSpend(totalSpend)} this month`)
    }
  }

  return lines.length > 0 ? lines.join("\n") : "No usage data — run: llm-tracker status"
}

// ---------------------------------------------------------------------------
// Detailed usage format (for /usage command — injected into session)
// ---------------------------------------------------------------------------

export function formatUsageReport(snapshots: Snapshot[]): string {
  const sub = snapshots.filter((s) => s.source === "subscription")
  const api = snapshots.filter((s) => s.source === "api")

  const lines: string[] = ["LLM Usage Report", ""]

  // Subscription table
  if (sub.length > 0) {
    lines.push("Subscription Limits:")
    lines.push("Provider     Used   Limit   Left    %Used  Resets   Tier")
    lines.push("-".repeat(65))
    for (const s of sub.sort((a, b) => a.provider.localeCompare(b.provider))) {
      const used = s.messages_used ?? "?"
      const limit = s.messages_limit ?? "?"
      const left = s.messages_used !== null && s.messages_limit !== null
        ? String(s.messages_limit - s.messages_used)
        : "?"
      const p = s.messages_used !== null && s.messages_limit !== null
        ? `${pct(s.messages_used, s.messages_limit)}%`
        : "?"
      const reset = fmtMinutes(s.messages_reset_at)
      const tier = s.model_tier ?? "?"
      const name = providerName(s.provider).padEnd(12)
      lines.push(
        `${name} ${String(used).padStart(5)}  ${String(limit).padStart(6)}  ${left.padStart(5)}  ${p.padStart(6)}  ${reset.padStart(7)}   ${tier}`,
      )
    }
    lines.push("")
  } else {
    lines.push("No subscription data. Run: llm-tracker status")
    lines.push("")
  }

  // API spend table
  if (api.length > 0) {
    lines.push("API Spend (this month):")
    lines.push("Provider     Spend       In Tokens    Out Tokens")
    lines.push("-".repeat(55))
    for (const s of api.sort((a, b) => a.provider.localeCompare(b.provider))) {
      const name = providerName(s.provider).padEnd(12)
      const spend = fmtSpend(s.api_spend_usd).padStart(10)
      const tokIn = fmtTokens(s.tokens_input).padStart(12)
      const tokOut = fmtTokens(s.tokens_output).padStart(13)
      lines.push(`${name} ${spend}  ${tokIn}  ${tokOut}`)
    }
    const total = api.reduce((sum, s) => sum + (s.api_spend_usd ?? 0), 0)
    lines.push("-".repeat(55))
    lines.push(`${"Total".padEnd(12)} ${fmtSpend(total).padStart(10)}`)
    lines.push("")
  }

  return lines.join("\n")
}

// ---------------------------------------------------------------------------
// Spend summary format (for /spend command)
// ---------------------------------------------------------------------------

export function formatSpendReport(spend: SpendSummary): string {
  const entries = Object.entries(spend)
  if (entries.length === 0) {
    return "No API spend data. Is LiteLLM configured?"
  }

  const lines: string[] = ["API Spend Summary (30 days)", ""]
  lines.push("Provider     Spend       In Tokens    Out Tokens   Period     Last Updated")
  lines.push("-".repeat(80))

  let totalSpend = 0
  for (const [provider, data] of entries.sort(([a], [b]) => a.localeCompare(b))) {
    const name = providerName(provider).padEnd(12)
    const spend = fmtSpend(data.spend_usd).padStart(10)
    const tokIn = fmtTokens(data.tokens_input).padStart(12)
    const tokOut = fmtTokens(data.tokens_output).padStart(13)
    const period = (data.period ?? "?").padStart(9)
    const updated = data.collected_at
      ? new Date(data.collected_at).toLocaleString("en-US", {
          month: "short",
          day: "numeric",
          hour: "2-digit",
          minute: "2-digit",
        })
      : "?"
    lines.push(`${name} ${spend}  ${tokIn}  ${tokOut}  ${period}   ${updated}`)
    totalSpend += data.spend_usd ?? 0
  }

  lines.push("-".repeat(80))
  lines.push(`${"Total".padEnd(12)} ${fmtSpend(totalSpend).padStart(10)}`)
  lines.push("")

  return lines.join("\n")
}

// ---------------------------------------------------------------------------
// Recommendation format (for /recommend command)
// ---------------------------------------------------------------------------

export function formatRecommendations(recs: Recommendation[]): string {
  if (recs.length === 0) {
    return "No recommendations. Run: llm-tracker status"
  }

  const icons: Record<string, string> = {
    use: "\u2713",    // ✓
    warn: "\u26a0",   // ⚠
    avoid: "\u2717",  // ✗
    wait: "\u231b",   // ⏳
    unknown: "?",
  }

  const lines: string[] = ["Provider Recommendations", ""]
  for (const r of recs) {
    const icon = icons[r.action] ?? "\u2192"
    lines.push(`${icon} ${r.message}`)
  }
  lines.push("")

  return lines.join("\n")
}

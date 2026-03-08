import type { Plugin } from "@opencode-ai/plugin"
import { tool } from "@opencode-ai/plugin"
import { TrackerClient } from "./lib/client"
import { loadConfig, type PluginConfig } from "./lib/config"
import {
  formatToast,
  formatUsageReport,
  formatSpendReport,
  formatRecommendations,
} from "./lib/format"

interface OpencodeClient {
  tui: {
    showToast: (params: {
      body: {
        message: string
        variant: "info" | "success" | "warning" | "error"
        duration?: number
      }
    }) => Promise<unknown>
  }
  app: {
    log: (params: {
      body: {
        service: string
        level: "debug" | "info" | "warn" | "error"
        message: string
        extra?: Record<string, unknown>
      }
    }) => Promise<unknown>
  }
}

export const LLMUsagePlugin: Plugin = async ({ client, $ }) => {
  const typedClient = client as unknown as OpencodeClient
  const config: PluginConfig = loadConfig()
  const tracker = new TrackerClient(config.backendUrl, config.timeoutMs)

  let lastToastAt = 0
  let backendStarted = false

  async function log(level: "debug" | "info" | "warn" | "error", message: string, extra?: Record<string, unknown>): Promise<void> {
    try {
      await typedClient.app.log({ body: { service: "llm-usage", level, message, extra } })
    } catch {
      // ignore logging failures
    }
  }

  async function showToast(message: string, variant: "info" | "warning" | "error" = "info", duration?: number): Promise<void> {
    try {
      await typedClient.tui.showToast({
        body: { message, variant, duration: duration ?? config.toastDurationMs },
      })
    } catch (err) {
      await log("warn", "Failed to show toast", {
        error: err instanceof Error ? err.message : String(err),
      })
    }
  }

  function isThrottled(): boolean {
    const now = Date.now()
    if (now - lastToastAt < config.minIntervalMs) return true
    lastToastAt = now
    return false
  }

  async function showUsageToast(trigger: string): Promise<void> {
    if (isThrottled()) return

    try {
      await ensureBackend()
      const snapshots = await tracker.status()
      if (snapshots.length === 0) return
      const message = formatToast(snapshots)
      await showToast(message)
      await log("debug", "Showed usage toast", { trigger })
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err)
      if (msg.includes("timeout") || msg.includes("serve running")) {
        await log("debug", "Backend not reachable, skipping toast", { trigger })
      } else {
        await log("warn", "Toast failed", { trigger, error: msg })
      }
    }
  }

  async function ensureBackend(): Promise<boolean> {
    try {
      if (await tracker.isAlive()) return true
    } catch {
      // Network error during health check — backend is down
    }
    if (backendStarted) return false

    backendStarted = true
    await log("info", "Backend not reachable, attempting auto-start")
    try {
      $`llm-tracker serve > /dev/null 2>&1 &`.quiet().nothrow()
      // Give it a moment to bind the port
      await new Promise((r) => setTimeout(r, 3000))
      let alive = false
      try {
        alive = await tracker.isAlive()
      } catch {
        // ignore
      }
      if (alive) {
        await log("info", "Backend auto-started successfully", { url: config.backendUrl })
      } else {
        await log("warn", "Backend started but not responding yet", { url: config.backendUrl })
      }
      return alive
    } catch (err) {
      await log("warn", "Failed to auto-start backend", {
        error: err instanceof Error ? err.message : String(err),
      })
      return false
    }
  }

  // Defer startup check so it doesn't interfere with OpenCode's TUI initialization
  setTimeout(() => { void ensureBackend() }, 5000)

  return {
    event: async ({ event }) => {
      if (event.type === "session.idle" && config.showOnIdle) {
        await showUsageToast("session.idle")
      }
      if (event.type === "session.compacted" && config.showOnCompact) {
        await showUsageToast("session.compacted")
      }
    },

    config: async (input: unknown) => {
      const cfg = input as { command?: Record<string, { template: string; description: string }> }
      cfg.command ??= {}
      cfg.command["usage"] = {
        template: "/usage",
        description: "Show LLM subscription usage and API spend",
      }
      cfg.command["spend"] = {
        template: "/spend",
        description: "Show API spend summary (30 days)",
      }
      cfg.command["recommend"] = {
        template: "/recommend",
        description: "Show which LLM provider to use right now",
      }
      cfg.command["collect"] = {
        template: "/collect",
        description: "Trigger a fresh data collection from all providers",
      }
    },

    "command.execute.before": async (input) => {
      const { command } = input as { command: string; sessionID: string }

      if (command === "usage") {
        try {
          const snapshots = await tracker.status()
          const message = formatToast(snapshots)
          await showToast(message, "info", 15000)
        } catch (err) {
          await showToast(backendDownMessage(err), "error")
        }
        return
      }

      if (command === "spend") {
        try {
          const spend = await tracker.spendSummary()
          const parts: string[] = ["API Spend (30d):"]
          for (const [provider, data] of Object.entries(spend).sort(([a], [b]) => a.localeCompare(b))) {
            const name = provider.charAt(0).toUpperCase() + provider.slice(1)
            const usd = data.spend_usd !== null ? `$${data.spend_usd.toFixed(2)}` : "?"
            parts.push(`${name}: ${usd}`)
          }
          const total = Object.values(spend).reduce((s, d) => s + (d.spend_usd ?? 0), 0)
          parts.push(`Total: $${total.toFixed(2)}`)
          await showToast(parts.join(" | "), "info", 15000)
        } catch (err) {
          await showToast(backendDownMessage(err), "error")
        }
        return
      }

      if (command === "recommend") {
        try {
          const recs = await tracker.recommend()
          if (recs.length === 0) {
            await showToast("No recommendations. Run: llm-tracker status", "warning")
          } else {
            const icons: Record<string, string> = { use: "\u2713", warn: "\u26a0", avoid: "\u2717", wait: "\u231b", unknown: "?" }
            const parts = recs.map((r) => `${icons[r.action] ?? ">"} ${r.message}`)
            await showToast(parts.join(" | "), "info", 15000)
          }
        } catch (err) {
          await showToast(backendDownMessage(err), "error")
        }
        return
      }

      if (command === "collect") {
        try {
          await tracker.triggerCollect()
          await showToast("Collection triggered. Data will refresh shortly.", "info")
        } catch (err) {
          await showToast(backendDownMessage(err), "error")
        }
        return
      }
    },

    tool: {
      llm_usage: tool({
        description:
          "Show current LLM subscription usage limits and API spend across all providers. " +
          "Returns subscription headroom (messages used/remaining/reset) and API cost data.",
        args: {
          detail: tool.schema.enum(["summary", "full", "recommend"]).optional(),
        },
        async execute(args) {
          try {
            const detail = args.detail ?? "summary"
            if (detail === "recommend") {
              const recs = await tracker.recommend()
              return formatRecommendations(recs)
            }
            const snapshots = await tracker.status()
            if (detail === "full") {
              const spend = await tracker.spendSummary()
              return formatUsageReport(snapshots) + "\n" + formatSpendReport(spend)
            }
            return formatToast(snapshots)
          } catch (err) {
            return backendDownMessage(err)
          }
        },
      }),
    },
  }
}

function backendDownMessage(err: unknown): string {
  const msg = err instanceof Error ? err.message : String(err)
  return `LLM Tracker: ${msg}. Run: llm-tracker serve`
}

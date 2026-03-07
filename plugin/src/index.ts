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
  session: {
    prompt: (params: {
      path: { id: string }
      body: {
        noReply?: boolean
        parts: Array<{ type: "text"; text: string; ignored?: boolean }>
      }
    }) => Promise<unknown>
  }
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

export const LLMUsagePlugin: Plugin = async ({ client }) => {
  const typedClient = client as unknown as OpencodeClient
  const config: PluginConfig = loadConfig()
  const tracker = new TrackerClient(config.backendUrl, config.timeoutMs)

  let lastToastAt = 0

  async function log(level: "debug" | "info" | "warn" | "error", message: string, extra?: Record<string, unknown>): Promise<void> {
    try {
      await typedClient.app.log({ body: { service: "llm-usage", level, message, extra } })
    } catch {
      // ignore logging failures
    }
  }

  async function injectOutput(sessionID: string, text: string): Promise<void> {
    try {
      await typedClient.session.prompt({
        path: { id: sessionID },
        body: {
          noReply: true,
          parts: [{ type: "text", text, ignored: true }],
        },
      })
    } catch (err) {
      await log("warn", "Failed to inject output", {
        error: err instanceof Error ? err.message : String(err),
      })
    }
  }

  async function showToast(message: string, variant: "info" | "warning" | "error" = "info"): Promise<void> {
    try {
      await typedClient.tui.showToast({
        body: { message, variant, duration: config.toastDurationMs },
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

  // Verify backend connectivity on init (non-blocking)
  void (async () => {
    const alive = await tracker.isAlive()
    if (alive) {
      await log("info", "Plugin initialized, backend reachable", { url: config.backendUrl })
    } else {
      await log("warn", "Backend not reachable — run: llm-tracker serve", { url: config.backendUrl })
    }
  })()

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
      const { command, sessionID } = input as { command: string; sessionID: string }

      if (command === "usage") {
        try {
          const snapshots = await tracker.status()
          const report = formatUsageReport(snapshots)
          await injectOutput(sessionID, report)
        } catch (err) {
          await injectOutput(sessionID, backendDownMessage(err))
        }
        return
      }

      if (command === "spend") {
        try {
          const spend = await tracker.spendSummary()
          const report = formatSpendReport(spend)
          await injectOutput(sessionID, report)
        } catch (err) {
          await injectOutput(sessionID, backendDownMessage(err))
        }
        return
      }

      if (command === "recommend") {
        try {
          const recs = await tracker.recommend()
          const report = formatRecommendations(recs)
          await injectOutput(sessionID, report)
        } catch (err) {
          await injectOutput(sessionID, backendDownMessage(err))
        }
        return
      }

      if (command === "collect") {
        try {
          await tracker.triggerCollect()
          await showToast("Collection triggered. Data will refresh shortly.", "info")
        } catch (err) {
          await injectOutput(sessionID, backendDownMessage(err))
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
  return [
    "LLM Usage Tracker backend not reachable.",
    "",
    `Error: ${msg}`,
    "",
    "Start the backend with:",
    "  llm-tracker serve",
    "",
    "Or run the daemon for continuous collection:",
    "  llm-tracker daemon",
  ].join("\n")
}

/**
 * hrusha ledger query tool (Tier 3 / C2)
 *
 * Thin read-only bridge: registers a `hrusha_query` tool that shells out to the
 * existing `hrusha query` CLI and returns its JSON to the agent. No hrusha
 * logic is ported into the agent process; secrets stay in the CLI's own
 * config/env and never pass through the LLM.
 *
 * Install: copy this file to ~/.pi/agent/extensions/ or a project's
 * .pi/extensions/. Requires `hrusha` on PATH (uv run hrusha / pip install -e .).
 *
 * Security notes:
 *  - Only the read-only `query` subcommand is exposed; no write/sync path.
 *  - Output is JSON-validated before return; on parse failure the raw stderr
 *    is returned truncated so the agent sees the error without it being
 *    interpreted as data.
 *  - Hex addresses and tx hashes are NOT redacted here — they are public
 *    on-chain data, which is the whole point of an agent-queryable ledger.
 *    API keys / config values never appear in `hrusha query` output.
 */

import { spawn } from "node:child_process";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const HRUSHA_BIN = process.env.HRUSHA_BIN ?? "hrusha";
const MAX_OUTPUT = 200_000; // chars — keep tool_result payloads bounded

const QUERY_PARAMS = Type.Object({
	token: Type.Optional(Type.String({ description: "filter by token symbol/address" })),
	source: Type.Optional(Type.String({ description: "filter by source label" })),
	kind: Type.Optional(Type.String({ description: "filter by event kind (transfer_in, transfer_out, fee, ...)" })),
	address: Type.Optional(Type.String({ description: "filter by wallet address" })),
	tag: Type.Optional(Type.String({ description: "filter by tag" })),
	since: Type.Optional(Type.Integer({ description: "unix-seconds lower bound (inclusive)" })),
	until: Type.Optional(Type.Integer({ description: "unix-seconds upper bound (inclusive)" })),
	limit: Type.Optional(Type.Integer({ description: "rows to return (default 50, max 500)", minimum: 1, maximum: 500 })),
});

function runHrushaQuery(args: string[]): Promise<{ stdout: string; stderr: string; code: number }> {
	return new Promise((resolve) => {
		const child = spawn(HRUSHA_BIN, ["query", ...args], { stdio: ["ignore", "pipe", "pipe"] });
		const stdoutChunks: Buffer[] = [];
		const stderrChunks: Buffer[] = [];
		child.stdout.on("data", (d: Buffer) => stdoutChunks.push(d));
		child.stderr.on("data", (d: Buffer) => stderrChunks.push(d));
		child.on("error", (err) => {
			resolve({ stdout: "", stderr: `failed to spawn ${HRUSHA_BIN}: ${err.message}`, code: -1 });
		});
		child.on("close", (code) => {
			resolve({
				stdout: Buffer.concat(stdoutChunks).toString(),
				stderr: Buffer.concat(stderrChunks).toString(),
				code: code ?? -1,
			});
		});
	});
}

function buildArgs(params: Record<string, unknown>): string[] {
	const args: string[] = [];
	for (const [k, v] of Object.entries(params)) {
		if (v === undefined || v === null) continue;
		args.push(`--${k}`, String(v));
	}
	return args;
}

export default function hrushaQueryExtension(pi: ExtensionAPI) {
	pi.registerTool({
		name: "hrusha_query",
		label: "Hrusha Ledger Query",
		description:
			"Query the hrusha on-chain income ledger read-only. Returns JSON rows " +
			"(newest first): id, ts, kind, token, amount_native, usd_at_time, address, " +
			"counterparty, tx_hash, source, tags, token_id. Filters AND-combine. " +
			"Use this to answer questions about on-chain income, transfers, fees, " +
			"and tagged events (e.g. aerodrome-voting) without running a full sync.",
		promptSnippet: "Query the hrusha ledger for on-chain income/transfer/fee events.",
		promptGuidelines: [
			"Prefer hrusha_query over bash for ledger questions — it returns structured JSON.",
			"Filters AND-combine; use token/kind/address/tag/since/until/limit.",
		],
		parameters: QUERY_PARAMS,
		async execute(_toolCallId, params) {
			const args = buildArgs(params as Record<string, unknown>);
			const { stdout, stderr, code } = await runHrushaQuery(args);

			if (code !== 0) {
				return {
					content: [
						{
							type: "text",
							text: `hrusha query failed (exit ${code}): ${stderr.trim() || stdout.trim() || "no output"}`.slice(0, MAX_OUTPUT),
						},
					],
					details: { exitCode: code, args },
					isError: true,
				};
			}

			// Validate JSON before handing to the agent.
			const trimmed = stdout.trim();
			try {
				const parsed = JSON.parse(trimmed);
				const text = JSON.stringify(parsed);
				return {
					content: [{ type: "text", text: text.slice(0, MAX_OUTPUT) }],
					details: { exitCode: 0, rowCount: Array.isArray(parsed) ? parsed.length : null, args },
				};
			} catch {
				return {
					content: [
						{
							type: "text",
							text: `hrusha query returned non-JSON output:\n${trimmed.slice(0, MAX_OUTPUT)}`,
						},
					],
					details: { exitCode: 0, args, parseError: true },
					isError: true,
				};
			}
		},
	});

	pi.on("session_start", (_event, ctx) => {
		ctx.ui.notify("hrusha_query tool ready (read-only ledger)", "info");
	});
}

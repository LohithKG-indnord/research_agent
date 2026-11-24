import Anthropic from "@anthropic-ai/sdk";
import { recorded } from "./db.mjs";

let client;
// $ per million tokens [input, output].
const PRICES = { "claude-sonnet-5": [2, 10], "claude-sonnet-5-5": [2, 10], "claude-haiku-5-5": [0.1, 0.5], "claude-opus-5-5": [4, 20] };
export const model = () => process.env.CLAUDE_MODEL || "claude-opus-5-5";

/**
 * One Claude call that must return JSON matching `schema` (structured outputs).
 * Claude gets no tools: it only decides from the text it is given.
 * Returns { data, usage }; usage.usd is null for a model without a listed price.
 */
export async function askJSON({ system, prompt, schema, effort = "medium", maxTokens = 16000 }) {
  const key = process.env.ANTHROPIC_API_KEY || process.env.CLAUDE_API_KEY;
  if (!key) throw new Error("Set ANTHROPIC_API_KEY (or CLAUDE_API_KEY) in .env to run the agent.");
  client ??= new Anthropic({ apiKey: key, maxRetries: 4 });
  const usageOf = ({ usage: u }) => {
    const price = PRICES[model()];
    const input_tokens = u.input_tokens + (u.cache_creation_input_tokens || 0) + (u.cache_read_input_tokens || 0);
    return { input_tokens, output_tokens: u.output_tokens, usd: price ? (input_tokens * price[0] + u.output_tokens * price[1]) / 1e6 : null };
  };
  const msg = await recorded("anthropic", "decide", { model: model(), system, prompt }, () => client.messages.stream({
    model: model(), max_tokens: maxTokens, system, messages: [{ role: "user", content: prompt }],
    output_config: { effort, format: { type: "json_schema", schema } }
  }).finalMessage(), raw => { const { usd, ...tokens } = usageOf(raw); return { ...tokens, cost: usd }; });
  if (msg.stop_reason === "refusal") throw new Error("The model declined this request.");
  if (msg.stop_reason === "max_tokens") throw new Error("The model ran out of output tokens.");
  const text = msg.content.filter(b => b.type === "text").map(b => b.text).join("");
  return { data: JSON.parse(text), usage: usageOf(msg) };
}

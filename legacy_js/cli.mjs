// Usage: node cli.mjs "Razorpay" "Zoho, zoho.com" [--product "..."] [--criteria "..."]
import "./agent/env.mjs";
import { parseArgs } from "node:util";
import { runAgent, parseCompanies } from "./agent/pipeline.mjs";

const { values, positionals } = parseArgs({ allowPositionals: true, options: { product: { type: "string" }, criteria: { type: "string" } } });
if (!positionals.length) { console.error('Usage: node cli.mjs "<company>" ["<company>, domain.com" ...] [--product "..."]'); process.exit(1); }

const result = await runAgent(
  { companies: positionals.flatMap(parseCompanies), product: values.product, criteria: values.criteria },
  e => { if (e.type === "stage") console.log(`\n== ${e.message}`); else if (e.type === "log") console.log(`   ${e.message}`); }
);
for (const c of result.companies) {
  console.log(`\n#${c.rank_position} ${c.name} (${c.domain}) — ${c.fit_score}/100 ${c.tier}, willingness to buy: ${c.willingness_to_buy}`);
  console.log(`   ${c.verdict}`);
  console.log(`   Pitch: ${c.pitch_angle}`);
  for (const [k, f] of Object.entries(c.financials)) console.log(`   ${k}: ${f ? `${f.value} (${f.period}, ${f.basis}, ${f.entity}) — ${f.url}${f.others.length ? ` | others: ${f.others.map(o => `${o.value} (${o.period}, ${o.url})`).join("; ")}` : ""}` : "Unknown"}`);
  for (const e of c.evidence.filter(e => e.status === "verified").slice(0, 3)) console.log(`   • "${e.quote}" — ${e.url}`);
}
for (const f of result.failed) console.log(`\n✗ ${f.input_name}: ${f.error}`);
console.log("\nSaved to leads/leads.json");

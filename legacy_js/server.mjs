import "./agent/env.mjs";
import http from "node:http";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { join } from "node:path";
import { runAgent, leadsDir } from "./agent/pipeline.mjs";
import { reviewCompany, enrichCompany, saveLead, peopleProviders, MAX_PEOPLE } from "./agent/people.mjs";
import { syncDb } from "./agent/db.mjs";
import { warmup } from "./agent/rerank.mjs";

const port = Number(process.env.PORT || 3000);
const assets = { "/": ["index.html", "text/html"], "/app.js": ["app.js", "text/javascript"], "/styles.css": ["styles.css", "text/css"] };
const actions = { "/api/review": reviewCompany, "/api/people": enrichCompany, "/api/save-lead": saveLead };

const server = http.createServer(async (req, res) => {
  const { pathname } = new URL(req.url, "http://localhost");
  const reply = (status, body) => res.writeHead(status, { "Content-Type": "application/json", "Cache-Control": "no-store" }).end(JSON.stringify(body));
  // Results hold people's contact details and runs cost money, so other websites must not reach this server through the browser.
  if (!/^(localhost|127\.0\.0\.1)(:\d+)?$/.test(req.headers.host || "")) return reply(403, { error: "Forbidden" });
  if (req.method === "GET" && assets[pathname]) {
    const [file, type] = assets[pathname];
    res.writeHead(200, { "Content-Type": `${type}; charset=utf-8` });
    return res.end(await readFile(fileURLToPath(new URL(`./public/${file}`, import.meta.url))));
  }
  if (req.method === "GET" && pathname === "/api/config") {
    return reply(200, { product: process.env.PRODUCT_DESCRIPTION || "", criteria: process.env.IDEAL_CUSTOMER || "", ready: !!(process.env.ANTHROPIC_API_KEY || process.env.CLAUDE_API_KEY), people: peopleProviders(), max_people: MAX_PEOPLE });
  }
  if (req.method === "GET" && pathname === "/api/latest") {
    const latest = await readFile(join(leadsDir, "leads.json"), "utf8").then(JSON.parse).catch(() => null);
    return reply(200, latest);
  }
  if (req.method === "POST" && (pathname === "/api/run" || actions[pathname])) {
    if (!/^application\/json\b/.test(req.headers["content-type"] || "")) return reply(415, { error: "Send JSON." });
    let raw = "";
    for await (const chunk of req) { raw += chunk; if (raw.length > 50_000) return reply(413, { error: "Too large." }); }
    let body;
    try { body = JSON.parse(raw); } catch { return reply(400, { error: "Invalid JSON" }); }
    if (actions[pathname]) {
      try { return reply(200, await actions[pathname](body)); }
      catch (error) { return reply(400, { error: error.message }); }
    }
    // NDJSON stream: one event per line so the browser can show progress live.
    res.writeHead(200, { "Content-Type": "application/x-ndjson", "Cache-Control": "no-cache" });
    const send = event => { if (!res.writableEnded) res.write(`${JSON.stringify(event)}\n`); };
    try {
      send({ type: "result", result: await runAgent(body, send) });
    } catch (error) {
      console.error(error);
      send({ type: "error", message: error.message || "Run failed." });
    }
    return res.end();
  }
  reply(404, { error: "Not found" });
});

const synced = await syncDb(leadsDir);
server.listen(port, "localhost", () => {
  console.log(`Database leads/leads.db: ${synced} runs in sync`);
  console.log(`Account Fit Agent running at http://localhost:${port}`);
  warmup(); // download/load the reranker in the background
});

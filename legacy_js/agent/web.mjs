// Web access: search providers and page fetching.
import { execFile } from "node:child_process";
import { lookup } from "node:dns/promises";
import { BlockList, isIPv6 } from "node:net";
import { join } from "node:path";
import { promisify } from "node:util";
import { recorded } from "./db.mjs";

const run = promisify(execFile);
const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36";

const entities = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " ", "#39": "'" };
export const decode = s => String(s || "").replace(/&(#x?[0-9a-f]+|\w+);/gi, (m, e) =>
  e[0] === "#" ? String.fromCodePoint(parseInt(e.slice(1).replace(/^x/i, ""), /^#x/i.test(e) ? 16 : 10)) : entities[e.toLowerCase()] ?? m);
const strip = s => decode(String(s || "").replace(/<[^>]+>/g, " ")).replace(/\s+/g, " ").trim();

export function domainOf(value) {
  try {
    const url = new URL(/^https?:\/\//i.test(value) ? value : `https://${value}`);
    return url.hostname.toLowerCase().replace(/^www\./, "");
  } catch { return ""; }
}

// Never fetch private/internal hosts or social networks (design doc section 10).
const blockedHost = /^(localhost|.*\.local|.*\.internal|127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|169\.254\.|0\.|\[)|(^|\.)linkedin\.com$|(^|\.)facebook\.com$|(^|\.)instagram\.com$|(^|\.)x\.com$|(^|\.)twitter\.com$/i;
export const fetchable = url => { try { const u = new URL(url); return /^https?:$/.test(u.protocol) && !blockedHost.test(u.hostname); } catch { return false; } };

const privateNets = new BlockList();
for (const [net, bits] of [["0.0.0.0", 8], ["10.0.0.0", 8], ["100.64.0.0", 10], ["127.0.0.0", 8], ["169.254.0.0", 16], ["172.16.0.0", 12], ["192.168.0.0", 16]]) privateNets.addSubnet(net, bits, "ipv4");
for (const [net, bits] of [["::", 127], ["fc00::", 7], ["fe80::", 10]]) privateNets.addSubnet(net, bits, "ipv6");
export const isPrivate = ip => privateNets.check(ip, isIPv6(ip) ? "ipv6" : "ipv4");

/** Follow redirects by hand so every hop passes the host rules, not only the first URL. */
export async function safeFetch(url, options) {
  for (let hop = 0; hop <= 5; hop++) {
    // shortcut: fetch resolves DNS again after this check; pin the IP if the agent runs inside a sensitive network.
    if (!fetchable(url) || (await lookup(new URL(url).hostname, { all: true })).some(a => isPrivate(a.address))) return null;
    const res = await fetch(url, { ...options, redirect: "manual" });
    const location = res.status >= 300 && res.status < 400 && res.headers.get("location");
    if (!location) return { res, url };
    await res.body?.cancel();
    url = new URL(location, url).href;
  }
  return null;
}

/** True when `host` is `domain` or one of its subdomains. */
export const sameSite = (host, domain) => !!domain && (host === domain || host.endsWith(`.${domain}`));

async function exaSearch(query, max) {
  const data = await recorded("exa", "search", { query, max }, async () => {
    const res = await fetch("https://api.exa.ai/search", {
      method: "POST", headers: { "Content-Type": "application/json", "x-api-key": process.env.EXA_API_KEY },
      body: JSON.stringify({ query, type: "auto", numResults: max, contents: { highlights: { maxCharacters: 1200 } } }), signal: AbortSignal.timeout(20000)
    });
    if (!res.ok) throw new Error(`Exa returned ${res.status}`);
    return res.json();
  }, raw => ({ cost: raw.costDollars?.total ?? null }));
  return {
    rows: data.results.map(r => ({ title: r.title || "", url: r.url, snippet: (r.highlights || []).join(" "), date: r.publishedDate || "" })),
    cost: data.costDollars?.total ?? null
  };
}

// The npm shim is a .cmd that execFile can't run, so call the binary itself (no shell: queries hold user text).
const donsetchBin = () => process.env.DONSETCH_BINARY || join(process.env.APPDATA || "", "npm/node_modules/donsetch/binaries/donsetch.exe");

async function donsetchSearch(query, max) {
  const data = await recorded("donsetch", "search", { query, max }, async () => JSON.parse((await run(donsetchBin(), ["search", "--json", "--quiet", "--max-results", String(max), "--", query], { timeout: 45000, maxBuffer: 5e6, windowsHide: true })).stdout), () => ({ cost: 0 }));
  return { rows: data.meta.results.map(r => ({ title: r.title || "", url: r.url, snippet: r.snippet || "", date: "" })), cost: 0 };
}

export const engines = { exa: exaSearch, donsetch: donsetchSearch }; // an object so tests can replace them

const plain = s => String(s || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();

/**
 * DonSeTch (free) first. Exa (paid) runs only when DonSeTch fails or finds fewer than half `max` results that mention
 * `about` (the company); the two are then merged, results about the company first.
 * `alsoExa`: always add Exa, listed first (its longer page extracts often hold figures, e.g. revenue, that DonSeTch's snippets miss).
 */
export async function search(query, max = 8, about = "", alsoExa = false) {
  const name = plain(about).replace(/ (inc|llc|ltd|corp|corporation|company|pvt|private|limited)$/, "");
  const relevant = r => !name || plain(`${r.title} ${r.snippet} ${r.url}`).includes(name);
  const tryEngine = async provider => {
    try { const { rows, cost } = await engines[provider](query, max); return { rows: rows.filter(r => fetchable(r.url)), cost }; }
    catch (error) { console.warn(`${provider} search failed: ${error.message.slice(0, 200)}`); return { rows: [], cost: 0 }; }
  };
  const useExa = alsoExa && !!process.env.EXA_API_KEY;
  const [free, early] = await Promise.all([tryEngine("donsetch"), useExa ? tryEngine("exa") : null]);
  if (!useExa && (free.rows.filter(relevant).length >= Math.ceil(max / 2) || !process.env.EXA_API_KEY)) {
    return { rows: free.rows.slice(0, max), provider: free.rows.length ? "donsetch" : "none", cost: 0 };
  }
  const paid = early || await tryEngine("exa");
  const all = [...new Map((useExa ? [...paid.rows, ...free.rows] : [...free.rows, ...paid.rows]).map(r => [r.url, r])).values()];
  const rows = [...all.filter(relevant), ...all.filter(r => !relevant(r))].slice(0, max);
  const used = [free.rows.length && "donsetch", paid.rows.length && "exa"].filter(Boolean).join("+");
  return { rows, provider: used || "none", cost: paid.cost };
}

/** Fetch a page and reduce it to readable text plus title, description and same-site links. */
export async function fetchPage(url, maxChars = 6000) {
  if (!fetchable(url)) return null;
  for (let attempt = 1; attempt <= 3; attempt++) {
    const page = await fetchOnce(url, maxChars);
    if (page !== "retry") return page;
    if (attempt < 3) await new Promise(r => setTimeout(r, 1500 * attempt));
  }
  console.warn(`fetch failed after retries: ${url}`);
  return null;
}

// Returns a page, null (permanent: 4xx / not HTML) or "retry" (timeout, network error, 429, 5xx).
async function fetchOnce(url, maxChars) {
  try {
    const hit = await safeFetch(url, { headers: { "User-Agent": UA, Accept: "text/html,application/xhtml+xml", "Accept-Language": "en-US,en;q=0.9" }, signal: AbortSignal.timeout(20000) });
    if (!hit) return null;
    const { res, url: finalUrl } = hit;
    if (res.status === 429 || res.status >= 500) return "retry";
    if (!res.ok || !/text\/html|xhtml/i.test(res.headers.get("content-type") || "")) return null;
    const html = (await res.text()).slice(0, 1_500_000);
    const meta = name => strip((html.match(new RegExp(`<meta[^>]+(?:name|property)=["']${name}["'][^>]*content=["']([^"']*)`, "i")) || [])[1]);
    const body = html.replace(/<(script|style|noscript|svg|template|iframe)[\s\S]*?<\/\1>/gi, " ").replace(/<!--[\s\S]*?-->/g, " ");
    const links = [...body.matchAll(/<a[^>]+href=["']([^"'#]+)["']/gi)].map(m => { try { return new URL(decode(m[1]), finalUrl).href; } catch { return ""; } })
      .filter(href => href && domainOf(href) === domainOf(finalUrl));
    return {
      url: finalUrl,
      title: strip((html.match(/<title[^>]*>([\s\S]*?)<\/title>/i) || [])[1]),
      description: meta("description") || meta("og:description"),
      text: strip(body).slice(0, maxChars),
      links: [...new Set(links)]
    };
  } catch { return "retry"; }
}

/** Run `fn` over `items` with at most `limit` in flight. Keeps input order. */
export async function mapLimit(items, limit, fn) {
  const out = new Array(items.length);
  let next = 0;
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (next < items.length) { const i = next++; out[i] = await fn(items[i], i); }
  }));
  return out;
}

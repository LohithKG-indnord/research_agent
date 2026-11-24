import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

// Dependency-free .env loader; real environment variables win.
try {
  const text = readFileSync(fileURLToPath(new URL("../.env", import.meta.url)), "utf8");
  for (const line of text.split(/\r?\n/)) {
    const m = line.match(/^\s*([A-Z][A-Z0-9_]*)\s*=\s*(.*?)\s*$/);
    if (m && m[2] && !process.env[m[1]]) process.env[m[1]] = m[2].replace(/^['"]|['"]$/g, "");
  }
} catch { /* .env is optional */ }

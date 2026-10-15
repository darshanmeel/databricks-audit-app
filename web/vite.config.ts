/// <reference types="vitest/config" />
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";

const REPO = resolve(import.meta.dirname, "..");

// A hash of every tracked file under web/, written into the built page. tests/test_web_dist.py
// computes the same hash, so a source change without a rebuild fails the tests.
export function sourceHash(): string {
  const files = execFileSync("git", ["ls-files", "-z", "web/"], { cwd: REPO, encoding: "utf8" })
    .split("\0").filter(Boolean).sort();
  const h = createHash("sha256");
  for (const f of files) {
    const text = readFileSync(resolve(REPO, f)).toString("latin1").replace(/\r\n/g, "\n");
    h.update(f + "\0", "utf8");
    h.update(Buffer.from(text, "latin1"));
    h.update("\0", "utf8");
  }
  return h.digest("hex").slice(0, 16);
}

function sourceHashMeta(): Plugin {
  return {
    name: "source-hash",
    apply: "build",
    transformIndexHtml: (html) => html.replace("</head>", `  <meta name="source-hash" content="${sourceHash()}">\n</head>`),
  };
}

export default defineConfig({
  plugins: [react(), sourceHashMeta()],
  // One bundle for a page served from this machine; its size is expected.
  build: { outDir: "../app/web/dist", emptyOutDir: true, chunkSizeWarningLimit: 1500 },
  // npm run dev: the page from Vite, /api from the running app (AUDIT_API_PORT, default 8000).
  server: { proxy: { "/api": `http://127.0.0.1:${process.env.AUDIT_API_PORT || 8000}` } },
  // threads: the default forks pool times out starting its workers on Windows.
  test: { environment: "jsdom", pool: "threads" },
});

// Local preview: serves public/ and runs the two Vercel functions, like `vercel dev` but with no account.
//   BOT_URL=http://127.0.0.1:8787 PAPAYA_WEB_SECRET=... node dev-server.mjs   ->  http://localhost:3000
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { extname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";
import bot from "./api/bot.js";
import queue from "./api/queue.js";

const root = join(fileURLToPath(new URL(".", import.meta.url)), "public");
const TYPES = { ".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css", ".webp": "image/webp",
  ".png": "image/png", ".ttf": "font/ttf", ".woff2": "font/woff2", ".webmanifest": "application/manifest+json", ".txt": "text/plain" };

createServer(async (req, res) => {
  const path = new URL(req.url, "http://x").pathname;
  if (path === "/api/bot") return bot(req, res);
  if (path === "/api/queue") return queue(req, res);
  const file = normalize(join(root, path === "/" ? "index.html" : path));
  if (!file.startsWith(root)) { res.statusCode = 403; return res.end(); }
  try {
    const data = await readFile(file);
    res.setHeader("Content-Type", TYPES[extname(file)] || "application/octet-stream");
    res.end(data);
  } catch {
    res.statusCode = 404; res.end("not found");
  }
}).listen(Number(process.env.PORT || 3000), () => console.log("http://localhost:" + (process.env.PORT || 3000)));

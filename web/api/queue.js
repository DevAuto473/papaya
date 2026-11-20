// Vercel function: while the laptop is off, the phone leaves its request here ("solve this", "tell me when
// you wake up"). When the bot wakes up it takes the list (with the shared secret) and does them.
// Storage: Upstash Redis (free) through its REST API — set UPSTASH_REDIS_REST_URL and _TOKEN on Vercel.
// Only a hash of the phone's session token is stored, never the token.
import { createHash } from "node:crypto";

export const config = { maxDuration: 10 };

const KEY = "babaya:queue";
const ACTIONS = new Set(["solve", "submit", "refresh", "notify"]);
const ID = /^[A-Za-z0-9_-]{1,64}$/;
const DEVICE = /^[A-Za-z0-9_-]{1,32}$/;

function send(res, code, obj) {
  res.statusCode = code;
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.setHeader("Cache-Control", "no-store");
  res.end(JSON.stringify(obj));
}

async function readJson(req) {
  if (req.body && typeof req.body === "object") return req.body;
  const chunks = [];
  let size = 0;
  for await (const c of req) {
    size += c.length;
    if (size > 4096) throw new Error("too big");
    chunks.push(c);
  }
  const t = Buffer.concat(chunks).toString("utf8");
  return t ? JSON.parse(t) : {};
}

async function redis(path, commands) {
  const r = await fetch(process.env.UPSTASH_REDIS_REST_URL.replace(/\/+$/, "") + path, {
    method: "POST",
    headers: { Authorization: "Bearer " + process.env.UPSTASH_REDIS_REST_TOKEN, "Content-Type": "application/json" },
    body: JSON.stringify(commands),
  });
  if (!r.ok) throw new Error("redis " + r.status);
  return r.json();
}

export default async function handler(req, res) {
  if (req.method !== "POST") return send(res, 405, { error: "POST only" });
  if (!process.env.UPSTASH_REDIS_REST_URL || !process.env.UPSTASH_REDIS_REST_TOKEN) {
    return send(res, 501, { error: "queue not configured" });
  }
  const secret = process.env.PAPAYA_WEB_SECRET || "";
  try {
    // the bot taking the list
    if (secret && req.headers["x-papaya-secret"] === secret) {
      const out = await redis("/multi-exec", [["LRANGE", KEY, "0", "-1"], ["DEL", KEY]]);
      const items = (out[0]?.result || []).map((s) => {
        try { return JSON.parse(s); } catch { return null; }
      }).filter(Boolean);
      return send(res, 200, { items });
    }
    // a phone leaving a request
    const auth = String(req.headers.authorization || "");
    if (!auth.startsWith("Bearer ") || auth.length < 30) return send(res, 401, { error: "login" });
    const th = createHash("sha256").update(auth.slice(7).trim()).digest("hex");
    const b = await readJson(req);
    if (!ACTIONS.has(b.action)) return send(res, 400, { error: "bad action" });
    if (b.id !== undefined && !ID.test(String(b.id))) return send(res, 400, { error: "bad id" });
    if (b.as !== undefined && !DEVICE.test(String(b.as))) return send(res, 400, { error: "bad device" });
    const rl = await redis("/pipeline", [["INCR", "babaya:rl:" + th], ["EXPIRE", "babaya:rl:" + th, "3600"]]);
    if ((rl[0]?.result || 0) > 20) return send(res, 429, { error: "كثير، انتظر شوي" });
    const item = { th, action: b.action, at: new Date().toISOString() };
    if (b.id) item.id = String(b.id);
    if (b.as) item.as = String(b.as);
    await redis("/pipeline", [["RPUSH", KEY, JSON.stringify(item)], ["LTRIM", KEY, "-200", "-1"], ["EXPIRE", KEY, "604800"]]);
    return send(res, 200, { queued: true });
  } catch (e) {
    return send(res, 502, { error: "queue error" });
  }
}

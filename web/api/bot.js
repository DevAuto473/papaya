// Vercel function: the phone talks to this; it forwards to the bot on the laptop and adds the shared secret,
// so the laptop only serves requests that came through here. The laptop opens its own tunnel and leaves its
// current address in Upstash (key babaya:url); BOT_URL, if set, is used instead.
// If the laptop does not answer, the phone gets {asleep: true} and shows "ببايه نايم".
export const config = { maxDuration: 10 };

const PATH = /^\/api\/[a-z/]{1,40}$/;

function send(res, code, obj) {
  res.statusCode = code;
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.setHeader("Cache-Control", "no-store");
  res.end(JSON.stringify(obj));
}

async function readBody(req) {
  if (req.body !== undefined && req.body !== null) {          // Vercel may have parsed it already
    return typeof req.body === "string" ? req.body : JSON.stringify(req.body);
  }
  const chunks = [];
  let size = 0;
  for await (const c of req) {
    size += c.length;
    if (size > 65536) throw new Error("too big");
    chunks.push(c);
  }
  return Buffer.concat(chunks).toString("utf8");
}

let cached = { url: "", at: 0 };          // kept while this function instance is warm

async function botUrl(fresh) {
  if (process.env.BOT_URL) return process.env.BOT_URL.replace(/\/+$/, "");
  if (!fresh && cached.url && Date.now() - cached.at < 30000) return cached.url;
  const base = ((process.env.UPSTASH_REDIS_REST_URL || process.env.KV_REST_API_URL) || "").replace(/\/+$/, "");
  const token = (process.env.UPSTASH_REDIS_REST_TOKEN || process.env.KV_REST_API_TOKEN) || "";
  if (!base || !token) return "";
  try {
    const r = await fetch(base, { method: "POST", headers: { Authorization: "Bearer " + token, "Content-Type": "application/json" },
      body: JSON.stringify(["GET", "babaya:url"]) });
    const url = r.ok ? String((await r.json()).result || "") : "";
    cached = { url, at: Date.now() };
    return url;
  } catch {
    return cached.url;
  }
}

export default async function handler(req, res) {
  const url = new URL(req.url, "http://local");
  const p = url.searchParams.get("p") || "";
  if (!PATH.test(p) || !["GET", "POST"].includes(req.method)) return send(res, 400, { error: "bad request" });
  url.searchParams.delete("p");
  const secret = process.env.PAPAYA_WEB_SECRET || "";
  if (!secret) return send(res, 503, { asleep: true, error: "not configured" });

  let body;
  try {
    body = req.method === "POST" ? await readBody(req) : undefined;
  } catch {
    return send(res, 413, { error: "too big" });
  }
  const ip = String(req.headers["x-forwarded-for"] || "").split(",")[0].trim() || req.socket?.remoteAddress || "";
  const headers = {
    "X-Papaya-Secret": secret,
    "Content-Type": "application/json",
    "X-Client-IP": ip,
    "User-Agent": String(req.headers["user-agent"] || "").slice(0, 200),
    "ngrok-skip-browser-warning": "1",
  };
  if (req.headers.authorization) headers.Authorization = req.headers.authorization;
  if (req.headers["x-as"]) headers["X-As"] = String(req.headers["x-as"]).slice(0, 40);

  const qs = url.searchParams.toString();
  const attempt = async (base, ms) => {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), ms);
    try {
      const r = await fetch(base + p + (qs ? "?" + qs : ""), { method: req.method, headers, body, signal: ctrl.signal });
      const text = await r.text();
      try { return { status: r.status, data: JSON.parse(text) }; } catch { return null; }   // a tunnel error page
    } catch {
      return null;
    } finally {
      clearTimeout(timer);
    }
  };
  let base = await botUrl(false);
  let out = base ? await attempt(base, 4500) : null;
  if (!out && !process.env.BOT_URL) {                      // the laptop may have restarted with a new address
    const fresh = await botUrl(true);
    if (fresh && fresh !== base) out = await attempt(fresh, 4500);
  }
  if (!out) return send(res, 503, { asleep: true });
  return send(res, out.status, out.data);
}

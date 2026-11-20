// Vercel function: the phone talks to this; it forwards to the bot on the laptop (BOT_URL, through the
// tunnel) and adds the shared secret, so the laptop only serves requests that came through here.
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

export default async function handler(req, res) {
  const url = new URL(req.url, "http://local");
  const p = url.searchParams.get("p") || "";
  if (!PATH.test(p) || !["GET", "POST"].includes(req.method)) return send(res, 400, { error: "bad request" });
  url.searchParams.delete("p");
  const base = (process.env.BOT_URL || "").replace(/\/+$/, "");
  const secret = process.env.PAPAYA_WEB_SECRET || "";
  if (!base || !secret) return send(res, 503, { asleep: true, error: "not configured" });

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

  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 8000);
  try {
    const qs = url.searchParams.toString();
    const r = await fetch(base + p + (qs ? "?" + qs : ""), { method: req.method, headers, body, signal: ctrl.signal });
    const text = await r.text();
    let data;
    try {
      data = JSON.parse(text);
    } catch {
      return send(res, 503, { asleep: true });              // a tunnel error page: the laptop is off
    }
    return send(res, r.status, data);
  } catch {
    return send(res, 503, { asleep: true });
  } finally {
    clearTimeout(timer);
  }
}

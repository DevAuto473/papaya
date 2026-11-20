# موقع ببايه (PWA)

نفس البوت بالضبط، بس في موقع يتثبت على الجوال (آيفون وأندرويد) ويرسل إشعارات.

## كيف يشتغل

```
الجوال ──► Vercel (الواجهة + /api/bot) ──► نفق Cloudflare مجاني ──► البوت على اللابتوب (papaya/web.py)
```

- البوت يفتح النفق بنفسه (ينزّل cloudflared أول مرة) ويحط عنوانه في Upstash. Vercel يقرأ العنوان من هناك.
  ما فيه دومين ولا حساب Cloudflare ولا ngrok.
- `api/bot.js` يمرر الطلبات للابتوب ويضيف السر `PAPAYA_WEB_SECRET`. إذا اللابتوب ما رد يطلع «ببايه نايم».
- `api/queue.js` يحفظ الطلبات وقت النوم في نفس Upstash، والبوت يسحبها أول ما يصحى.

## الإعداد (مرة وحدة)

1. Vercel: Add New → Project → الريبو، Root Directory = `web`، Production Branch = `merged`.
2. Vercel: Storage → Create → Upstash (Redis) → اربطه بالمشروع (يضيف متغيراته لحاله).
3. Vercel: Environment Variables → `PAPAYA_WEB_SECRET` = نفس اللي في `.env`، ثم Redeploy.
4. `.env` على اللابتوب: `PAPAYA_WEB_SECRET` و `UPSTASH_REDIS_REST_URL` و `UPSTASH_REDIS_REST_TOKEN`.

## تجربة محلية

```
cd web
BOT_URL=http://127.0.0.1:8787 PAPAYA_WEB_SECRET=... node dev-server.mjs
```

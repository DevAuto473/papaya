# موقع ببايه (PWA)

نفس البوت بالضبط، بس في موقع يتثبت على الجوال (آيفون وأندرويد) ويرسل إشعارات.

## كيف يشتغل

```
الجوال ──► Vercel (الواجهة + /api/bot) ──► النفق (ngrok) ──► البوت على اللابتوب (papaya/web.py)
```

- الواجهة في `public/` وما تحتاج build.
- `api/bot.js` يمرر الطلبات للابتوب ويضيف السر `PAPAYA_WEB_SECRET`. إذا اللابتوب ما رد، يطلع «ببايه نايم».
- `api/queue.js` يحفظ الطلبات وقت النوم في Upstash (مجاني). البوت أول ما يصحى يسحبها وينفذها ويرسل إشعار.
- الإشعارات: كل رسالة يرسلها البوت في تيليجرام لجهاز توصل له إشعار بعد، وتنحفظ في محادثة الموقع.

## متغيرات Vercel

| المتغير | القيمة |
|---|---|
| `BOT_URL` | رابط النفق الثابت، مثل `https://xxxx.ngrok-free.app` |
| `PAPAYA_WEB_SECRET` | نفس اللي في `.env` |
| `UPSTASH_REDIS_REST_URL` و `UPSTASH_REDIS_REST_TOKEN` | من Upstash (اختياري، لطابور وقت النوم) |

Root Directory في Vercel = `web`.

## تجربة محلية

```
cd web
BOT_URL=http://127.0.0.1:8787 PAPAYA_WEB_SECRET=... node dev-server.mjs
```

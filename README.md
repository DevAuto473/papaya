# papaya 🍈 — مذكّر واجبات واختبارات مدرستي

## 1) التركيب (على WSL)
```bash
cd ~/papaya
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install --with-deps chromium     # يطلب sudo مرة واحدة
cp .env.example .env && nano .env           # ضع التوكن وأسماء الأجهزة
python -m pytest -q tests                   # يجب أن تنجح كلها (تستخدم نسختين من صفحاتك في tests/fixtures)
```

## 2) ربط الجهاز
1. شغّل `python -m papaya.main run` وأرسل `/start` للبوت من جوالك → يعطيك رقم محادثتك. أوقفه (Ctrl+C)، وضع الرقم في `.env` كـ `PAPAYA_ADMIN_CHAT_ID` ثم شغّله من جديد.
2. لكل حساب (مرة واحدة، تحتاج نافذة متصفح): `python -m papaya.main login papaya01` وسجّل دخولك بنفسك في النافذة.
   - إذا ما ظهرت نافذة: تحتاج Windows 11 مع WSLg. تحقق: `echo $DISPLAY` (لو فاضي، قل لي).
3. صاحب كل جوال يرسل `/start` للبوت، وأنت (المشرف) تضغط الزر لربطه بالجهاز.
4. جرّب فحصاً واحداً: `python -m papaya.main check` — يرسل لكل حساب ملخصاً.
5. التشغيل الدائم: `python -m papaya.main run` (داخل `tmux` أفضل).

لا كلمات مرور في أي ملف أو في تيليجرام: تسجيل الدخول يدوي والجلسة تُحفظ في `profiles/`.

## أوامر المستخدم في البوت
`/settings` `/list` `/remind 8` `/quiet 23 6` `/quiet off` `/help`
للمشرف: `/status` `/unlink papaya01`

## إذا تعطل
- يصلك تنبيه على تيليجرام باسم الجهاز والسبب مرة واحدة (لا تكرار).
- انتهت الجلسة → `python -m papaya.main login papaya0X`.
- "صفحة غير متوقعة" أو "ما لقيت رابط" → شغّل `python -m papaya.main diagnose papaya01` وأرسل لي محتوى `diagnose/papaya01/links.txt` (المعرّفات تُخفى تلقائياً).

## ملاحظات
- لابتوبك لازم يبقى شغالاً ومتصلاً، ووضع النوم مطفي.
- لا ترفع `.env` ولا `profiles/` ولا `papaya.db` لأي مكان (هي في `.gitignore`).
- `tests/fixtures/` فيها صفحات مدرستي الخاصة بك (فيها معرّفات حسابك). لا تشاركها ولا ترفعها.

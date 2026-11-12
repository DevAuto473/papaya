"""Ask two different models, compare their answers question by question. No I/O of its own:
`ask(model, messages) -> (text, cost_usd)` is injected (real one: ai.chat)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from concurrent.futures import ThreadPoolExecutor

from .ai import extract_json

LETTERS = "أبجدهوزحطي"


@dataclass
class Question:
    id: str
    text: str
    kind: str                      # choice (incl. true/false) | match | order | text | unknown
    options: list[str] = field(default_factory=list)   # choices / list (ب) for match / items for order
    images: list[str] = field(default_factory=list)    # https:// or data: URLs (a screenshot of the question)
    prompts: list[str] = field(default_factory=list)   # list (أ) for match
    meta: dict = field(default_factory=dict)           # page-specific ids used to fill the answer


@dataclass
class Answer:
    qid: str
    choice: int | None             # 1-based option number (choice questions)
    text: str | None               # free text (text questions)
    why: str
    agree: bool
    alt_choice: int | None = None  # the other model's answer when they disagree
    alt_text: str | None = None
    seq: list[int] | None = None   # match: option number for each (أ) item / order: item numbers in order
    alt_seq: list[int] | None = None
    votes: int = 0                 # how many models gave this answer
    tiebreak: bool = False         # the two first models disagreed; the third model decided (2 of 3)


SYSTEM = (
    "أنت معلم سعودي دقيق جداً للمرحلة الثانوية (مناهج وزارة التعليم). "
    "حل كل سؤال بعناية، تحقق من الحساب خطوة بخطوة، ولا تخمّن. "
    "مع كل سؤال صورة له؛ إذا اختلف النص عن الصورة (معادلات، رموز، رسومات) فاعتمد الصورة. "
    "الخيارات مرقّمة بنفس ترتيب ظهورها من الأعلى للأسفل.\n"
    "أنواع الأسئلة:\n"
    "- اختيار / صح وخطأ: choice = رقم الخيار الصحيح.\n"
    "- مزاوجة: seq = لكل عنصر من القائمة (أ) بالترتيب، رقم العنصر المناسب من القائمة (ب).\n"
    "- ترتيب: seq = أرقام العناصر بالترتيب الصحيح (مثال [3,1,2]).\n"
    "- مقالي: text = الإجابة.\n"
    "قبل كل إجابة اكتب في work خطوات الحل باختصار (حتى 3 أسطر)، ثم راجعها، ثم اختر الإجابة.\n"
    "أعد JSON فقط بهذا الشكل بدون أي كلام قبله أو بعده:\n"
    '{"answers":[{"id":"<id>","work":"<خطوات مختصرة>","choice":<رقم أو null>,"seq":[<أرقام>] أو null,'
    '"text":"<نص أو null>","why":"<سبب مختصر في جملة أو جملتين>"}]}'
)

TIEBREAK_NOTE = ("هذه أسئلة اختلف فيها نموذجان. حلها بنفسك من الصفر وبدقة، "
                 "وابحث في الويب إذا احتجت معلومة من المنهج.")

KIND_AR = {"choice": "اختيار", "match": "مزاوجة", "order": "ترتيب", "text": "مقالي", "unknown": "غير معروف"}


def build_messages(questions: list[Question], subject: str, title: str, note: str = "") -> list:
    parts = [{"type": "text", "text": f"المادة: {subject}\nالواجب: {title}\n" + (note + "\n" if note else "")}]
    for q in questions:
        if q.kind == "unknown":
            continue
        block = f"\n[id={q.id}] ({KIND_AR.get(q.kind, q.kind)})\n{q.text}\n"
        if q.kind == "match":
            block += "القائمة (أ):\n" + "".join(f"  {n}) {p}\n" for n, p in enumerate(q.prompts, 1))
            block += "القائمة (ب):\n"
        elif q.kind == "order":
            block += "العناصر (بترتيبها الحالي):\n"
        for n, o in enumerate(q.options, 1):
            block += f"  {n}) {o}\n"
        parts.append({"type": "text", "text": block})
        for img in q.images:
            parts.append({"type": "image_url", "image_url": {"url": img}})
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": parts}]


def _has(r, q: Question) -> bool:
    if not r:
        return False
    if q.kind == "choice":
        return r["choice"] is not None
    if q.kind in ("match", "order"):
        return r["seq"] is not None
    return bool(_norm(r["text"]))


def _norm(s: str | None) -> str:
    return re.sub(r"[\sً-ْـ.,،:؛]+", "", (s or "")).lower()


def _parse(text: str, questions: list[Question]) -> dict:
    data = extract_json(text)
    out = {}
    valid = {q.id: q for q in questions}
    for a in data.get("answers", []):
        qid = str(a.get("id", ""))
        q = valid.get(qid)
        if not q:
            continue
        c = a.get("choice")
        try:
            c = int(c) if c is not None else None
        except (TypeError, ValueError):
            c = None
        if c is not None and not (1 <= c <= max(len(q.options), 1)):
            c = None
        seq = _valid_seq(a.get("seq"), q)
        out[qid] = {"choice": c, "seq": seq, "text": a.get("text"), "why": str(a.get("why") or "")[:300]}
    return out


def _valid_seq(seq, q: Question):
    """Only accept a complete, in-range answer; anything partial counts as no answer."""
    if q.kind not in ("match", "order") or not isinstance(seq, list):
        return None
    try:
        seq = [int(x) for x in seq]
    except (TypeError, ValueError):
        return None
    n = len(q.options)
    if q.kind == "order":
        return seq if sorted(seq) == list(range(1, n + 1)) else None
    if len(seq) != len(q.prompts) or not all(1 <= x <= n for x in seq):
        return None
    if len(set(seq)) != len(seq):          # matching is one-to-one: the same answer twice is a mix-up
        return None
    return seq


@dataclass
class Result:
    answers: list[Answer]
    cost: float
    errors: list[str]

    @property
    def all_agree(self) -> bool:
        return bool(self.answers) and all(a.agree for a in self.answers)

    def to_dict(self):
        return {"answers": [asdict(a) for a in self.answers], "errors": self.errors, "cost": self.cost}


def _same(q: Question, x, y) -> bool:
    if not x or not y:
        return False
    if q.kind == "choice":
        return x["choice"] is not None and x["choice"] == y["choice"]
    if q.kind in ("match", "order"):
        return x["seq"] is not None and x["seq"] == y["seq"]
    return _norm(x["text"]) != "" and _norm(x["text"]) == _norm(y["text"])


def solve(questions: list[Question], subject: str, title: str, ask, model_a: str, model_b: str,
          model_c: str | None = None, tiebreak_ok=lambda: True) -> Result:
    """Two cheap models answer everything. Only questions they disagree on go to model C
    (stronger, with web search); its answer decides by majority. All three different -> ⚠️."""
    msgs = build_messages(questions, subject, title)
    replies, errors, cost = {}, [], 0.0
    with ThreadPoolExecutor(2) as ex:
        futs = {m: ex.submit(ask, m, msgs) for m in (model_a, model_b)}
        for m, f in futs.items():
            try:
                text, c = f.result()
                cost += c
                replies[m] = _parse(text, questions)
            except Exception as e:                 # one model failing still leaves the other
                errors.append(f"{m}: {e}")
    A, B = replies.get(model_a, {}), replies.get(model_b, {})
    C = {}
    disputed = [q for q in questions if q.kind != "unknown" and not _same(q, A.get(q.id), B.get(q.id))]
    if model_c and disputed and tiebreak_ok():
        try:
            text, c = ask(model_c, build_messages(disputed, subject, title, TIEBREAK_NOTE))
            cost += c
            C = _parse(text, disputed)
        except Exception as e:
            errors.append(f"{model_c}: {e}")
    answers = []
    for q in questions:
        if q.kind == "unknown":
            answers.append(Answer(q.id, None, None, "نوع سؤال ما أعرف أعبيه، حلّه بنفسك", False))
            continue
        a, b, c3 = A.get(q.id), B.get(q.id), C.get(q.id)
        if _same(q, a, b):
            votes = 3 if _same(q, a, c3) else 2
            answers.append(Answer(q.id, a["choice"], a["text"], a["why"], True, seq=a["seq"], votes=votes))
            continue
        if _has(c3, q) and (_same(q, a, c3) or _same(q, b, c3)):      # majority of three
            win, lose = (a, b) if _same(q, a, c3) else (b, a)
            answers.append(Answer(q.id, win["choice"], win["text"], win["why"] or c3["why"], True,
                                  seq=win["seq"], votes=2, tiebreak=True))
            continue
        main, other = (a, b) if _has(a, q) else (b, None)
        if _has(c3, q) and not _has(main, q):
            main = c3
        if not _has(main, q):
            answers.append(Answer(q.id, None, None, "ما قدر أي نموذج يحل هذا السؤال", False))
            continue
        if q.kind == "choice":
            agree = bool(other) and main["choice"] is not None and main["choice"] == other["choice"]
        elif q.kind in ("match", "order"):
            agree = bool(other) and main["seq"] is not None and main["seq"] == other["seq"]
        else:
            agree = bool(other) and _norm(main["text"]) != "" and _norm(main["text"]) == _norm(other["text"])
        diff = not agree and bool(other)
        answers.append(Answer(q.id, main["choice"], main["text"], main["why"], agree,
                              other["choice"] if diff else None, other["text"] if diff else None,
                              main["seq"], other["seq"] if diff else None))
    return Result(answers, cost, errors)

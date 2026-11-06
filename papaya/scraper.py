"""Playwright side: one persistent browser profile per account. Only reads pages.

Setup (login) is done by the user in a visible browser: they log in, open
'الواجبات' and 'الاختبارات' themselves and mark each one (a / e). We record those
two page addresses and revisit them on every check."""
from __future__ import annotations

import re
import threading
from collections import defaultdict
from pathlib import Path

from .parser import page_title

BASE = "https://schools.madrasati.sa"
NAV_ANY = "a[href*='/Student/']"          # present on every logged-in Madrasati page
TITLES = {"assign": "واجباتي", "exams": "اختباراتي"}


class PapayaError(Exception):
    """Carries an Arabic text (Telegram) and an English text (terminal)."""
    def __init__(self, ar: str, en: str):
        super().__init__(en)
        self.ar, self.en = ar, en


class SessionExpired(PapayaError):
    def __init__(self, why_ar: str = "", why_en: str = ""):
        super().__init__("انتهت الجلسة، يلزم تسجيل دخول جديد" + (f" ({why_ar})" if why_ar else ""),
                         "session expired, login needed" + (f" ({why_en})" if why_en else ""))


class ScrapeError(PapayaError):
    pass


# A browser profile can only be open once at a time: the periodic check and a solve
# request for the same account must take turns.
_PROFILE_LOCKS: dict[str, threading.Lock] = defaultdict(threading.Lock)


def profile_lock(profile: Path) -> threading.Lock:
    return _PROFILE_LOCKS[str(Path(profile).resolve())]


def _prepare_ctx(ctx):
    """Hook (tests replace it to block outside requests)."""
    return ctx


def _ctx(p, profile: Path, headless: bool):
    profile.mkdir(parents=True, exist_ok=True)
    ctx = p.chromium.launch_persistent_context(
        str(profile), headless=headless, locale="ar-SA", timezone_id="Asia/Riyadh",
        viewport={"width": 1280, "height": 900})
    return _prepare_ctx(ctx)


def _title_of(page) -> str:
    try:
        loc = page.locator(".mainTitlePage")
        if loc.count() == 0:
            return ""
        return loc.first.inner_text(timeout=1000).strip()
    except Exception:
        return ""


def _current_tab(url: str) -> str:
    """Always watch the 'Current' assignments tab even if the user was on another one."""
    if "searchByStatus=" in url:
        return re.sub(r"searchByStatus=[^&#]*", "searchByStatus=Current", url)
    return url + ("&" if "?" in url else "?") + "searchByStatus=Current"


def _active_page(ctx):
    """The tab the user is looking at (visible), else the newest tab."""
    pages = list(ctx.pages)
    for pg in reversed(pages):
        try:
            if pg.evaluate("document.visibilityState") == "visible":
                return pg
        except Exception:
            pass
    return pages[-1] if pages else None


HELP_EN = ("Commands (type then Enter):\n"
           "  a  = the page open now is my ASSIGNMENTS page\n"
           "  e  = the page open now is my EXAMS page\n"
           "  s  = show what is saved\n"
           "  q  = finish and close the browser")


def setup_manual(profile: Path, start_url: str = BASE, headless: bool = False,
                 ask=input, driver=None) -> dict:
    """User logs in and browses freely. They tell us which page is which with 'a' / 'e'.
    Each choice is checked (page heading + how many items were read) before it is kept.
    `ask`/`driver` let tests play the user's part."""
    from playwright.sync_api import sync_playwright
    from .parser import parse_assignments, parse_exams
    found: dict = {}
    with sync_playwright() as p:
        ctx = _ctx(p, profile, headless)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(start_url, wait_until="domcontentloaded")
            print("Log in inside the browser window, then open your pages.\n" + HELP_EN)
            while True:
                if driver:
                    driver(page)
                try:
                    cmd = ask("> ").strip().lower()
                except EOFError:
                    break
                if cmd in ("a", "e"):
                    key = "assign" if cmd == "a" else "exams"
                    kind = url_kind(TITLES[key])
                    pg = _active_page(ctx)
                    if pg is None:
                        print("[warn] no open tab"); continue
                    if _title_of(pg) != TITLES[key]:
                        print(f"[warn] this tab is not the {kind} page (heading does not match). not saved.")
                        continue
                    html = pg.content()
                    n = len(parse_assignments(html) if key == "assign" else parse_exams(html))
                    found[key] = _current_tab(pg.url) if key == "assign" else pg.url
                    extra = " (will always use the Current tab)" if key == "assign" else ""
                    print(f"[ok] saved {kind} page: {n} {kind} found{extra}")
                elif cmd == "s":
                    for k in TITLES:
                        print(f"  {url_kind(TITLES[k])}: {'saved' if k in found else 'not saved'}")
                elif cmd == "q":
                    break
                elif cmd:
                    print(HELP_EN)
            page.wait_for_timeout(1500)          # let cookies settle before closing
        finally:
            ctx.close()
    return found


def _load(page, url: str, title: str) -> str:
    from playwright.sync_api import TimeoutError as PWTimeout
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    try:
        page.wait_for_selector(".mainTitlePage", timeout=20000)
    except PWTimeout:
        if page.locator(NAV_ANY).count() == 0:
            raise SessionExpired()
        raise ScrapeError(f"صفحة {title} ما اكتمل تحميلها", f"page did not finish loading: {url_kind(title)}")
    try:                                       # cards may load a moment after the heading
        page.wait_for_selector("div.dga-defualt-card", timeout=8000)
    except PWTimeout:
        pass                                   # genuinely empty list is fine
    page.wait_for_timeout(800)
    html = page.content()
    got = page_title(html)
    if got != title:
        if page.locator(NAV_ANY).count() == 0:
            raise SessionExpired()
        raise ScrapeError(f"صفحة غير متوقعة: '{got}' بدل '{title}'", f"unexpected page (expected {url_kind(title)})")
    return html


# ---- automatic re-login -------------------------------------------------------
# Madrasati's own session ends often, but Microsoft usually still remembers the account
# ("مُسجل الدخول"). Then logging in again is only clicks: pick the account, "stay signed in".
# No password is ever typed. If Microsoft wants a password or a code, a person must do it.
MS_HOSTS = ("login.microsoftonline.com", "login.live.com", "login.microsoft.com")
RELOGGED: dict = {}                      # profile -> time of the last automatic re-login (read by main)
_LAST_TRY: dict = {}
_LAST_WHY: dict = {}                     # same message while waiting, so the admin is told once
RETRY_EVERY = 30 * 60                    # at most one automatic try per account every 30 minutes


class NeedsPerson(Exception):
    pass


def _click_first(page, selectors) -> bool:
    for sel in selectors:
        loc = page.locator(sel)
        try:
            for i in range(min(loc.count(), 5)):
                el = loc.nth(i)
                if el.is_visible():
                    el.click(timeout=5000)
                    return True
        except Exception:
            continue
    return False


def _ms_step(page) -> str:
    """One Microsoft screen. Returns what it did."""
    if page.locator("input[name='passwd']:visible, input[type='password']:visible").count():
        raise NeedsPerson("password")
    if page.locator("input[name='otc']:visible, #idDiv_SAOTCC_Title:visible, #idDiv_SAOTCAS_Title:visible").count():
        raise NeedsPerson("code")
    if page.locator("input[name='loginfmt']:visible").count():
        raise NeedsPerson("email")           # Microsoft forgot the account
    kmsi = page.locator("#KmsiCheckboxField, input[name='DontShowAgain']")
    if kmsi.count() or page.get_by_text("البقاء", exact=False).count() or page.get_by_text("Stay signed in").count():
        try:
            if kmsi.count() and not kmsi.first.is_checked():
                kmsi.first.check(timeout=3000)
        except Exception:
            pass
        if _click_first(page, ["#idSIButton9", "button[data-testid='primaryButton']", "input[type='submit']"]):
            return "kmsi"
    # account picker: prefer the tile that says it is signed in
    for txt in ("مُسجل الدخول", "مسجل الدخول", "Signed in"):
        t = page.get_by_text(txt, exact=False)
        try:
            if t.count() and t.first.is_visible():
                t.first.click(timeout=5000)
                return "account"
        except Exception:
            pass
    if _click_first(page, ["#tilesHolder [role='button']", "div[data-test-id][role='button']", "div.table[role='button']",
                           "[data-test-id]"]):
        return "account"
    if _click_first(page, ["#idSIButton9", "button[data-testid='primaryButton']"]):
        return "next"
    return ""


def _madrasati_login_click(page) -> bool:
    # schools.madrasati.sa/login has two cards; students use "الدخول بحساب مايكروسوفت".
    # The green "تسجيل الدخول" in the header only reopens /login, so the card comes first.
    for txt in ("الدخول بحساب مايكروسوفت", "Sign in with Microsoft", "Microsoft account"):
        t = page.get_by_text(txt, exact=False)
        try:
            if t.count() and t.first.is_visible():
                t.first.click(timeout=5000)
                return True
        except Exception:
            pass
    if "/login" in page.url.lower():
        return False                     # on the login page but no Microsoft card: do not loop on the header
    return _click_first(page, ["a:has-text('تسجيل الدخول')", "button:has-text('تسجيل الدخول')", "a:has-text('دخول')",
                               "a[href*='Login']", "a[href*='login']", "button:has-text('Login')"])


def auto_login(page, target_url: str, steps: int = 12) -> bool:
    """Walk the login screens by clicking only. True when the Madrasati page is back."""
    page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
    for _ in range(steps):
        page.wait_for_timeout(2000)
        host = (page.url.split("/")[2] if "://" in page.url else "").lower()
        if host.endswith("madrasati.sa"):
            if page.locator(".mainTitlePage").count() and page.locator(NAV_ANY).count():
                return True
            if page.locator(NAV_ANY).count():          # logged in, but on another page
                page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
                continue
            if not _madrasati_login_click(page):
                page.wait_for_timeout(2000)
        elif any(h in host for h in MS_HOSTS) or "microsoft" in host or "adfs" in host or "sts" in host:
            _ms_step(page)
        else:
            page.wait_for_timeout(1000)
    return False


def _diagnose(profile: Path, page, tag: str):
    try:
        d = Path(profile).resolve().parent.parent / "diagnose"
        d.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(d / f"{tag}.png"), full_page=True)
        (d / f"{tag}.html").write_text(page.content(), encoding="utf-8")
    except Exception:
        pass


def _relogin(page, profile: Path, url: str, now: float) -> None:
    """Called when the session ended. Raises SessionExpired (with the reason) if a person is needed."""
    key = str(Path(profile).resolve())
    if now - _LAST_TRY.get(key, float("-inf")) < RETRY_EVERY:
        raise SessionExpired(*_LAST_WHY.get(key, ("", "")))
    _LAST_TRY[key] = now
    try:
        ok = auto_login(page, url)
    except NeedsPerson as e:
        _diagnose(profile, page, "relogin")
        why = {"password": ("مايكروسوفت يطلب الباسوورد", "Microsoft asks for the password"),
               "code": ("مايكروسوفت يطلب رمز تحقق", "Microsoft asks for a code"),
               "email": ("مايكروسوفت نسي الحساب", "Microsoft forgot the account")}[str(e)]
    except Exception as e:
        _diagnose(profile, page, "relogin")
        why = ("ما قدرت أدخل لحالي", f"auto login failed: {type(e).__name__}")
    else:
        why = None if ok else ("ما قدرت أدخل لحالي", "auto login did not finish")
        if why:
            _diagnose(profile, page, "relogin")
    if why:
        _LAST_WHY[key] = why
        raise SessionExpired(*why)
    _LAST_TRY.pop(key, None); _LAST_WHY.pop(key, None)
    RELOGGED[key] = now


def finished_ids(html: str) -> set:
    """Exam ids listed in the 'Completed' (المكتملة) tab. Empty unless the page really IS that tab:
    if the platform shows 'Current' instead, every open exam would look solved."""
    if not html:
        return set()
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    if not any("searchByStatus=Finished" in (a.get("href") or "") for a in soup.select("a.nav-link.active")):
        return set()
    return set(re.findall(r"InfoDiv(\d+)", html))


def _finished_url(exams_url: str) -> str:
    from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
    u = urlsplit(exams_url)
    q = [(k, v) for k, v in parse_qsl(u.query) if k.lower() != "searchbystatus"] + [("searchByStatus", "Finished")]
    return urlunsplit((u.scheme, u.netloc, u.path, urlencode(q), ""))


def _load_finished(page, exams_url: str) -> str:
    """Best effort: the Completed tab. Never opens an exam, only the list. '' on any problem."""
    try:
        a = page.locator("a[href*='searchByStatus=Finished']")
        href = a.first.get_attribute("href") if a.count() else None
        url = href if href and href.startswith("http") else (BASE + "/" + href.lstrip("/") if href else _finished_url(exams_url))
        return _load(page, url, TITLES["exams"])
    except Exception:
        return ""


def fetch(profile: Path, urls: dict, headless: bool = True, finished: bool = True) -> tuple[str, str, str]:
    """Return (assignments_html, exams_html, finished_exams_html). Raises SessionExpired / ScrapeError."""
    from playwright.sync_api import sync_playwright, Error as PWError
    if not urls.get("assign") or not urls.get("exams"):
        raise ScrapeError("ما عندي عناوين الصفحات، شغّل login لهذا الحساب", "page addresses missing, run login for this account")
    with profile_lock(profile), sync_playwright() as p:
        ctx = _ctx(p, profile, headless)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                a = _load(page, urls["assign"], TITLES["assign"])
            except SessionExpired:
                import time
                _relogin(page, profile, urls["assign"], time.time())      # clicks through, or raises
                a = _load(page, urls["assign"], TITLES["assign"])
            e = _load(page, urls["exams"], TITLES["exams"])
            return a, e, (_load_finished(page, urls["exams"]) if finished else "")
        except (SessionExpired, ScrapeError):
            raise
        except PWError as e:
            first = str(e).splitlines()[0][:120]
            raise ScrapeError(f"خطأ متصفح: {first}", f"browser error: {first}")
        finally:
            ctx.close()


def with_page(profile: Path, url: str, fn, headless: bool = True):
    """Open `url` with the account's saved session and return fn(page). Used by the solver."""
    from playwright.sync_api import sync_playwright, Error as PWError
    if url and not url.startswith("http"):
        url = BASE + "/" + url.lstrip("/")
    with profile_lock(profile), sync_playwright() as p:
        ctx = _ctx(p, profile, headless)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2500)
            if page.locator(NAV_ANY).count() == 0 and "Solve" not in page.url:
                raise SessionExpired()
            return fn(page)
        except PapayaError:
            raise
        except PWError as e:
            first = str(e).splitlines()[0][:120]
            raise ScrapeError(f"خطأ متصفح: {first}", f"browser error: {first}")
        finally:
            ctx.close()


def url_kind(title: str) -> str:
    return "assignments" if title == TITLES["assign"] else "exams"


def mask(url: str) -> str:
    return re.sub(r"(Id|UId|SchoolId|SubjectId)=[0-9A-Fa-f]+", r"\1=XXX", re.sub(r"/[0-9A-F]{24,}", "/XXX", url))

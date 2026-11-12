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


class NetworkDown(ScrapeError):
    """The laptop has no internet (or WSL lost DNS): nobody's fault, nothing to report per account."""
    def __init__(self, detail: str = ""):
        super().__init__("ما فيه نت", f"no internet ({detail})" if detail else "no internet")


NET_ERRORS = ("ERR_NAME_NOT_RESOLVED", "ERR_INTERNET_DISCONNECTED", "ERR_NETWORK_CHANGED", "ERR_CONNECTION_RESET",
              "ERR_CONNECTION_REFUSED", "ERR_CONNECTION_TIMED_OUT", "ERR_TIMED_OUT", "ERR_ADDRESS_UNREACHABLE",
              "ERR_NAME_RESOLUTION_FAILED", "ERR_PROXY_CONNECTION_FAILED")


def net_ok(hosts=("schools.madrasati.sa", "api.telegram.org"), timeout: float = 5) -> bool:
    """Can we resolve and reach the platform and Telegram right now?"""
    import socket
    for h in hosts:
        try:
            with socket.create_connection((h, 443), timeout=timeout):
                pass
        except OSError:
            return False
    return True


class WrongAccount(PapayaError):
    def __init__(self):
        super().__init__("المتصفح فاتح حساب طالب ثاني غير حساب هذا الجهاز، وقفت عشان ما أخلط بين المستخدمين",
                         "the browser is logged in to a different student - stopped so users are never mixed up")


# ---- who is this? ---------------------------------------------------------------
# Every Madrasati link carries the student's id (UId=...). The saved page addresses hold the
# right one, so a page that shows a different student is refused before anything is read.
_UID = re.compile(r"UId=([0-9A-Fa-f]{16,})")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
BOUND = "papaya_account.txt"             # in each profile: the Microsoft account that belongs to it


def check_identity(html: str, url: str):
    want = _UID.search(url or "")
    if not want:
        return
    seen = {u.upper() for u in _UID.findall(html or "")}
    if seen and want.group(1).upper() not in seen:
        raise WrongAccount()


def bound_email(profile: Path) -> str:
    try:
        return (Path(profile) / BOUND).read_text(encoding="utf-8").strip().lower()
    except OSError:
        return ""


def bind_email(profile: Path, email: str):
    try:
        Path(profile).mkdir(parents=True, exist_ok=True)
        (Path(profile) / BOUND).write_text(email.strip().lower(), encoding="utf-8")
    except OSError:
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
    try:
        (Path(profile) / BOUND).unlink()  # a new manual login may be a new account: learn it again
    except OSError:
        pass
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
    check_identity(html, url)
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


def _ms_step(page, expect: str = "", info: dict | None = None) -> str:
    """One Microsoft screen. Returns what it did. Only ever picks this profile's own account."""
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
    # account picker: pick by email, never a guess
    try:
        shown = {e.lower(): e for e in EMAIL.findall(page.locator("body").inner_text())}
    except Exception:
        shown = {}
    emails = sorted(shown)
    if emails:
        if expect and expect not in emails:
            raise NeedsPerson("other")
        if not expect and len(emails) > 1:
            raise NeedsPerson("several")
        pick = expect or emails[0]
        t = page.get_by_text(shown[pick], exact=False)
        try:
            if t.count():
                t.first.click(timeout=5000)
                if info is not None:
                    info["email"] = pick
                return "account"
        except Exception:
            pass
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


def auto_login(page, target_url: str, steps: int = 12, expect: str = "", info: dict | None = None) -> bool:
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
            _ms_step(page, expect, info)
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
    expect, info = bound_email(profile), {}
    try:
        ok = auto_login(page, url, expect=expect, info=info)
    except NeedsPerson as e:
        _diagnose(profile, page, "relogin")
        why = {"password": ("مايكروسوفت يطلب الباسوورد", "Microsoft asks for the password"),
               "code": ("مايكروسوفت يطلب رمز تحقق", "Microsoft asks for a code"),
               "email": ("مايكروسوفت نسي الحساب", "Microsoft forgot the account"),
               "other": ("مايكروسوفت فاتح حساب ثاني غير حساب هذا الجهاز", "Microsoft shows another account"),
               "several": ("فيه أكثر من حساب في المتصفح وما أختار عشوائي", "several accounts, will not guess")}[str(e)]
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
    if info.get("email") and not expect:
        bind_email(profile, info["email"])  # first automatic login: remember whose profile this is
    RELOGGED[key] = now


def _fill_first(page, selectors, value: str) -> bool:
    for sel in selectors:
        loc = page.locator(sel)
        try:
            if loc.count() and loc.first.is_visible():
                loc.first.fill(value, timeout=5000)
                return True
        except Exception:
            continue
    return False


_MS_NEXT = ["#idSIButton9", "button[data-testid='primaryButton']", "input[type='submit']"]


def auto_first_login(profile: Path, email: str, password: str, start_url: str = BASE) -> dict:
    """Fully automatic first-ever login for a brand-new account: used right after someone
    finishes the /start signup, so no person has to open a browser. The bot types the email
    and password itself - both are only ever held in memory by the caller for a few seconds,
    never written to disk. Then it finds its own 'واجباتي' / 'اختباراتي' pages, no person
    needed. Raises NeedsPerson if Microsoft asks for something only a person can give
    (a code, a forgotten account, several accounts), or ScrapeError/SessionExpired on
    anything else that went wrong."""
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    found: dict = {}
    try:
        (Path(profile) / BOUND).unlink()
    except OSError:
        pass
    with sync_playwright() as p:
        ctx = _ctx(p, profile, headless=True)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(start_url, wait_until="domcontentloaded", timeout=45000)
            if page.locator(NAV_ANY).count() == 0:
                if not _madrasati_login_click(page):
                    raise ScrapeError("ما قدرت أفتح صفحة الدخول", "could not open the login page")
                page.wait_for_timeout(1500)
                host = (page.url.split("/")[2] if "://" in page.url else "").lower()
                if not (any(h in host for h in MS_HOSTS) or "microsoft" in host or "adfs" in host or "sts" in host):
                    raise ScrapeError("ما طلعت صفحة مايكروسوفت", "did not reach a Microsoft login page")
                if not _fill_first(page, ["input[name='loginfmt']", "input[type='email']"], email):
                    raise NeedsPerson("email")
                _click_first(page, _MS_NEXT)
                page.wait_for_timeout(1500)
                if not _fill_first(page, ["input[name='passwd']", "input[type='password']"], password):
                    raise NeedsPerson("password")
                _click_first(page, _MS_NEXT)
                for _ in range(8):
                    page.wait_for_timeout(1500)
                    host = (page.url.split("/")[2] if "://" in page.url else "").lower()
                    if host.endswith("madrasati.sa") and page.locator(NAV_ANY).count():
                        break
                    if any(h in host for h in MS_HOSTS) or "microsoft" in host or "adfs" in host or "sts" in host:
                        _ms_step(page, email, {})
                else:
                    raise ScrapeError("ما رجع لمدرستي بعد تسجيل الدخول", "did not return to madrasati after sign-in")
            for key, title in TITLES.items():
                link = page.get_by_text(title, exact=True)
                if link.count() == 0:
                    raise ScrapeError(f"ما لقيت رابط {title}", f"nav link for {url_kind(title)} not found")
                link.first.click(timeout=8000)
                page.wait_for_load_state("domcontentloaded")
                try:
                    page.wait_for_selector(".mainTitlePage", timeout=15000)
                except PWTimeout:
                    raise ScrapeError(f"صفحة {title} ما حمّلت", f"{url_kind(title)} page did not load")
                if _title_of(page) != title:
                    raise ScrapeError(f"صفحة {title} غير متوقعة", f"unexpected {url_kind(title)} page")
                found[key] = _current_tab(page.url) if key == "assign" else page.url
            page.wait_for_timeout(1200)
        finally:
            ctx.close()
    bind_email(profile, email)
    return found


_PAGES = re.compile(r"صفحة\s*(\d+)\s*من\s*(\d+)")
MAX_PAGES = 10


def _all_pages(page, html: str) -> str:
    """The list may be split into pages ("صفحة 1 من 3"). Walk them with the page's own 'next'
    arrow and return all of them together. If a page will not turn, keep what we have."""
    m = _PAGES.search(re.sub(r"<[^>]+>", " ", html))
    if not m or int(m.group(2)) <= 1:
        return html
    parts, total = [html], min(int(m.group(2)), MAX_PAGES)
    for k in range(2, total + 1):
        try:
            nxt = page.locator("ul.pagination li.page-item:last-child a.page-link").first
            if not nxt.count():
                break
            nxt.click(timeout=8000)
            got = None
            for _ in range(30):                  # works whether the site reloads or redraws in place
                page.wait_for_timeout(500)
                try:
                    h = page.content()
                except Exception:
                    continue
                mm = _PAGES.search(re.sub(r"<[^>]+>", " ", h))
                if mm and int(mm.group(1)) == k:
                    got = h
                    break
            if got is None:
                break
            page.wait_for_timeout(400)
            parts.append(page.content())
        except Exception:
            break
    return "\n".join(parts)


def _load_list(page, url: str, title: str) -> str:
    return _all_pages(page, _load(page, url, title))


# e-course ("المقررات الإلكترونية") lists live in their own tab next to the school ones
ECOURSE = {"assign": "EsubjectAssignments", "exams": "EsubjectExams"}


def _ecourse_url(page, kind: str, base_url: str) -> str:
    path = ECOURSE[kind]
    try:
        a = page.locator(f"a[href*='/{path}']")
        href = a.first.get_attribute("href") if a.count() else None
    except Exception:
        href = None
    if href:
        from urllib.parse import urlsplit
        full = href if href.startswith("http") else BASE + "/" + href.lstrip("/")
        if urlsplit(full).netloc == urlsplit(base_url).netloc:      # same site as the saved page only
            return full
    return re.sub(r"/(Assignments|Exams)\?", f"/{path}?", base_url, count=1)


def _load_ecourse(page, kind: str, base_url: str) -> str:
    """Best effort: '' when the tab is missing or does not load. Only lists, never an exam."""
    from playwright.sync_api import TimeoutError as PWTimeout
    url = _ecourse_url(page, kind, base_url)
    if ECOURSE[kind] not in url:
        return ""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_selector(".mainTitlePage", timeout=20000)
        try:
            page.wait_for_selector("div.dga-defualt-card", timeout=6000)
        except PWTimeout:
            return ""                       # empty tab
        page.wait_for_timeout(600)
        html = page.content()
        check_identity(html, url)
        return _all_pages(page, html)
    except WrongAccount:
        raise
    except Exception:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass
        return ""


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
                a = _load_list(page, urls["assign"], TITLES["assign"])
            except SessionExpired:
                import time
                _relogin(page, profile, urls["assign"], time.time())      # clicks through, or raises
                a = _load_list(page, urls["assign"], TITLES["assign"])
            a_e = _load_ecourse(page, "assign", urls["assign"])
            e = _load_list(page, urls["exams"], TITLES["exams"])
            fin = _load_finished(page, urls["exams"]) if finished else ""
            e_e = _load_ecourse(page, "exams", urls["exams"])
            return "\n".join(x for x in (a, a_e) if x), "\n".join(x for x in (e, e_e) if x), fin
        except (SessionExpired, ScrapeError):
            raise
        except PWError as e:
            first = str(e).splitlines()[0][:120]
            if any(k in first for k in NET_ERRORS):
                raise NetworkDown(first)
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

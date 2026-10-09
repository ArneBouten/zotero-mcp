"""The fetcher's browser step: a real Chrome window with the user's logins.

Some copies only come through a browser: ResearchGate's and Academia.edu's
"Download" buttons, and publisher pages behind bot protection or an
institutional login. This step drives its own Chrome window, with its own
profile in ``~/.config/zotero-mcp/fetch-browser`` where the user logs in once
(``zotero-mcp fetch-fulltext --browser-login``). It works at a human pace and
never solves captchas: when one appears, or a login page, it waits for the
user to deal with it in the window and then carries on.

Needs Playwright (``pip install playwright``) and Google Chrome; with
``browser_channel: "chromium"`` it uses Playwright's own Chromium instead
(``python -m playwright install chromium``).
"""

from __future__ import annotations

import random
import re
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

from zotero_mcp import fulltext_fetch as ff

_LOGIN_HINTS = ("/login", "signin", "sign-in", "/sso", "idp.", "shibboleth", "wayf", "authenticate")
_VISIBLE_CHALLENGE = (
    "verify you are human", "are you a robot", "just a moment", "unusual traffic",
    "complete the security check", "press & hold", "press and hold", "i'm not a robot",
    "please complete the captcha", "checking your browser",
)
#: Challenges that check whether a script drives the browser (Cloudflare Turnstile and the like):
#: solving them in the fetcher's window starts the same check again, while the user's own browser
#: passes them. Such pages go to the user's browser instead (OWN_BROWSER).
_ROBOT_CHECK = ("performing security verification", "checking your browser", "just a moment",
                "verify you are human", "challenges.cloudflare.com", "cf-turnstile", "ray id")
OWN_BROWSER = "bot check that only your own browser passes"
_DOWNLOAD_TEXT = re.compile(r"^\s*(download( full-text)?( pdf)?|pdf|download paper|view pdf)\s*$", re.I)


def profile_dir() -> Path:
    return ff.config_dir() / "fetch-browser"


class BrowserUnavailable(RuntimeError):
    pass


class BrowserSession:
    """One Chrome window for a whole run. Started on first use."""

    def __init__(self, settings: ff.Settings, log: Callable[[str], None] = print, *, wait_for_user: float = 300.0):
        self.settings = settings
        self.log = log
        self.wait_for_user = wait_for_user
        self._pw = None
        self.ctx = None
        self.page = None
        self._last_nav = 0.0
        self.researchgate_pages = 0
        self.researchgate_flagged = False
        self.robot_hosts: set[str] = set()     # sites whose bot check refused this window

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> BrowserSession:
        if self.ctx is not None:
            return self
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise BrowserUnavailable(
                "the browser step needs Playwright: run `py -3.12 -m pip install playwright`"
            ) from e
        profile_dir().mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        channel = self.settings.browser_channel or "chrome"
        # Keep Chrome's sandbox on: Playwright's default --no-sandbox is a
        # security downgrade this window does not need.
        # Minimised, a window's pages would be throttled like background tabs: these switches keep
        # them loading at full speed.
        args = ["--start-maximized", "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding", "--disable-backgrounding-occluded-windows"]
        kwargs = dict(user_data_dir=str(profile_dir()), headless=False, accept_downloads=True,
                      viewport=None, args=args, ignore_default_args=["--no-sandbox"])
        try:
            if channel == "chromium":
                self.ctx = self._pw.chromium.launch_persistent_context(**kwargs)
            else:
                self.ctx = self._pw.chromium.launch_persistent_context(channel=channel, **kwargs)
        except Exception as e:
            self._pw.stop()
            self._pw = None
            raise BrowserUnavailable(
                f"could not open Chrome ({type(e).__name__}). Install Google Chrome, or set "
                '"browser_channel": "chromium" in fulltext_fetch and run '
                "`py -3.12 -m playwright install chromium`"
            ) from e
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        if self.settings.browser_minimized:
            self.set_window("minimized")
        return self

    # -- the window ------------------------------------------------------------

    def set_window(self, state: str) -> bool:
        """"minimized", "maximized" or "normal", through Chrome's DevTools protocol. False when
        that is not possible (the window then stays as it is)."""
        try:
            cdp = self.ctx.new_cdp_session(self.page)
            window = cdp.send("Browser.getWindowForTarget")["windowId"]
            cdp.send("Browser.setWindowBounds", {"windowId": window, "bounds": {"windowState": state}})
            return True
        except Exception:
            return False

    def window_state(self) -> str:
        try:
            cdp = self.ctx.new_cdp_session(self.page)
            window = cdp.send("Browser.getWindowForTarget")["windowId"]
            return cdp.send("Browser.getWindowBounds", {"windowId": window})["bounds"].get("windowState", "")
        except Exception:
            return ""

    def call_user(self) -> None:
        """Bring the window forward, with a sound: a captcha or login page needs the user."""
        self.set_window("normal")          # a minimised window cannot go straight to maximised
        self.set_window("maximized")
        try:
            self.page.bring_to_front()
        except Exception:
            pass
        try:
            import winsound

            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        except Exception:
            sys.stderr.write("\a")

    def dismiss(self) -> None:
        """Back to the taskbar once the user is done."""
        if self.settings.browser_minimized:
            self.set_window("minimized")

    def close(self) -> None:
        try:
            if self.ctx is not None:
                self.ctx.close()
        finally:
            self.ctx = None
            if self._pw is not None:
                self._pw.stop()
                self._pw = None

    # -- navigation --------------------------------------------------------

    def _pace(self, url: str = "") -> None:
        low, high = self.settings.browser_delay
        if "researchgate.net" in url:
            low, high = self.settings.researchgate_delay
        wait = random.uniform(low, high) - (time.monotonic() - self._last_nav)
        if wait > 0:
            time.sleep(wait)
        self._last_nav = time.monotonic()

    def _blocked_reason(self) -> str | None:
        """"captcha" or "login" when the page shows one, judged by what is
        visible: many ordinary pages load a captcha script without showing it."""
        try:
            url = self.page.url.lower()
            title = (self.page.title() or "").lower()
            text = (self.page.inner_text("body", timeout=3000) or "")[:4000].lower()
        except Exception:
            return None
        if "unusual activity from your network" in text or "access to this page is temporarily restricted" in text:
            if "researchgate" in url:
                self.researchgate_flagged = True
            return "captcha"
        if any(m in title or m in text for m in _VISIBLE_CHALLENGE):
            return "captcha"
        if any(h in url for h in _LOGIN_HINTS) and len(text) < 3000:
            return "login"
        return None

    def _robot_check(self) -> bool:
        try:
            html = (self.page.content() or "")[:200000].lower()
            text = (self.page.inner_text("body", timeout=3000) or "")[:3000].lower()
        except Exception:
            return False
        return ("cloudflare" in html or "turnstile" in html) and any(m in text or m in html for m in _ROBOT_CHECK)

    def wait_if_blocked(self) -> bool:
        """When a captcha or login page shows, wait for the user. True if clear.

        A check of whether a script drives the browser (Cloudflare) is given a few
        seconds to pass on its own; after that the page is for the user's own
        browser, where it passes (BrowserUnavailable with OWN_BROWSER)."""
        reason = self._blocked_reason()
        if not reason:
            return True
        host = _host(self.page.url)
        if reason == "captcha" and (host in self.robot_hosts or self._robot_check()):
            for _ in range(0 if host in self.robot_hosts else 5):
                time.sleep(3)
                if not self._blocked_reason():
                    time.sleep(2)
                    return True
            url = self.page.url
            self.robot_hosts.add(host)
            self.log("        !! this site's bot check does not accept the fetcher's Chrome; "
                     "it is left for your own browser")
            raise BrowserUnavailable(f"{OWN_BROWSER}: {url}")
        self.log(f"        !! a {reason} page is showing in the fetcher's Chrome window. "
                 f"Please deal with it there; waiting up to {self.wait_for_user / 60:.0f} min ...")
        self.call_user()
        deadline = time.monotonic() + self.wait_for_user
        try:
            while time.monotonic() < deadline:
                time.sleep(4)
                if not self._blocked_reason():
                    self.log("        ok, continuing")
                    time.sleep(2)
                    return True
            return False
        finally:
            self.dismiss()

    def researchgate_allowed(self) -> str | None:
        """Why ResearchGate is off for the rest of this run, or None."""
        if self.researchgate_flagged:
            return "ResearchGate has flagged this network; skipped for the rest of the run"
        if self.researchgate_pages >= self.settings.researchgate_per_run:
            return f"ResearchGate limit for one run reached ({self.settings.researchgate_per_run} pages)"
        return None

    def goto(self, url: str) -> bool:
        if _host(url) in self.robot_hosts:
            raise BrowserUnavailable(f"{OWN_BROWSER}: {url}")
        if "researchgate.net" in url:
            if self.researchgate_allowed():
                return False
            self.researchgate_pages += 1
        self._pace(url)
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
            try:
                self.page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
        except Exception as e:
            if "Download is starting" in str(e):
                return True
            self.log(f"        could not open {url[:80]} ({type(e).__name__})")
            return False
        return self.wait_if_blocked()

    # -- getting files -------------------------------------------------------

    def get_pdf(self, url: str, referer: str | None = None) -> ff.Fetched | None:
        """Download with the browser's cookies and identity."""
        try:
            resp = self.ctx.request.get(url, headers={"Referer": referer} if referer else None,
                                        timeout=90000, max_redirects=10)
            body = resp.body()
            return ff.Fetched(resp.status, resp.headers.get("content-type", ""), body, resp.url)
        except Exception as e:
            return ff.Fetched(0, "", b"", url, f"browser download failed ({type(e).__name__})")

    def click_download(self, locator) -> ff.Fetched | None:
        try:
            with self.page.expect_download(timeout=60000) as info:
                locator.click()
            download = info.value
            path = download.path()
            data = Path(path).read_bytes() if path else b""
            return ff.Fetched(200, "application/pdf", data, download.url)
        except Exception:
            return None

    def pdf_from_page(self) -> ff.Fetched | None:
        """The PDF behind the page now open: its citation_pdf_url, then a
        visible PDF/Download link, then a click on a Download button."""
        try:
            html = self.page.content()
        except Exception:
            return None
        here = self.page.url
        page = ff.Fetched(200, "text/html", html.encode("utf-8", "replace"), here)
        tried = set()
        pdf = ff._pdf_link_in_page(page)
        if pdf:
            tried.add(pdf)
            got = self.get_pdf(pdf, referer=here)
            if got and got.is_pdf:
                return got
        links = self.page.locator("a[href]")
        try:
            count = min(links.count(), 400)
        except Exception:
            count = 0
        candidates = []
        for i in range(count):
            a = links.nth(i)
            try:
                href = a.get_attribute("href") or ""
                text = (a.inner_text(timeout=500) or "").strip()
            except Exception:
                continue
            full = urljoin(here, href)
            low = full.lower()
            if full in tried:
                continue
            if _DOWNLOAD_TEXT.match(text) or low.endswith(".pdf") or "/pdf/" in low or "pdfdirect" in low \
                    or "/links/" in low and "researchgate" in low:
                candidates.append((a, full))
        for a, full in candidates[:4]:
            if full.startswith("http"):
                got = self.get_pdf(full, referer=here)
                if got and got.is_pdf:
                    return got
            got = self.click_download(a)
            if got and got.is_pdf:
                return got
        buttons = self.page.get_by_role("button", name=_DOWNLOAD_TEXT)
        try:
            if buttons.count():
                got = self.click_download(buttons.first)
                if got and got.is_pdf:
                    return got
        except Exception:
            pass
        return None

    # -- the three routes ------------------------------------------------------

    def researchgate_page(self, item: ff.ItemInfo) -> str | None:
        """The item's ResearchGate publication page, found by title."""
        if not self.goto("https://www.researchgate.net/search/publication?q=" + quote(item.title[:200])):
            return None
        best, best_score = None, 0.0
        links = self.page.locator("a[href*='/publication/']")
        try:
            count = min(links.count(), 60)
        except Exception:
            return None
        for i in range(count):
            a = links.nth(i)
            try:
                text = (a.inner_text(timeout=500) or "").strip()
                href = a.get_attribute("href") or ""
            except Exception:
                continue
            score = ff.title_similarity(item.title, text)
            if score > best_score:
                best, best_score = urljoin("https://www.researchgate.net/", href.split("?")[0]), score
        return best if best_score >= 0.85 else None


def _fetcher(fn: Callable[[], ff.Fetched | None]) -> Callable[[], ff.Fetched | None]:
    """Wrap a browser action so a failure leaves a reason for the log."""

    def run():
        try:
            got = fn()
        except ff_browser_errors() as e:
            run.reason = f"browser: {e}"
            return None
        except Exception as e:
            run.reason = f"browser error ({type(e).__name__})"
            return None
        if got is None:
            run.reason = run.reason or "the browser found no PDF on the page"
        return got

    run.reason = ""
    return run


def ff_browser_errors():
    return (BrowserUnavailable,)


def _session(http: ff.Http, settings: ff.Settings) -> BrowserSession:
    if http.browser is None:
        http.browser = BrowserSession(settings, log=http.log)
    return http.browser.start()


def src_browser(item: ff.ItemInfo, http: ff.Http, settings: ff.Settings, budget: ff.Budget) -> Iterator[ff.Candidate]:
    """Candidates the browser fetches itself: links that blocked a plain
    download, ResearchGate's copy, and the publisher's page with the user's
    logins (or library proxy)."""
    seen: set[str] = set()

    # 1. Links that refused a plain request earlier in this run or in an earlier one
    #    (ResearchGate, Academia.edu, bot-protected sites).
    for cand in list(http.blocked.get(item.key, [])) + ff.remembered_blocked(item.key):
        if cand.url in seen:
            continue
        seen.add(cand.url)

        def open_link(url=cand.url):
            b = _session(http, settings)
            if "researchgate.net" in url and b.researchgate_allowed():
                raise BrowserUnavailable(b.researchgate_allowed())
            if "researchgate.net" not in url:
                got = b.get_pdf(url)
                if got and got.is_pdf:
                    return got
            return b.pdf_from_page() if b.goto(url) else None

        yield ff.Candidate(cand.url, f"{cand.source}, in your browser", cand.version,
                           cand.by_identifier, fetcher=_fetcher(open_link))

    # 2. ResearchGate, searched by title.
    if item.title and item.item_type not in ("book",):
        def researchgate():
            b = _session(http, settings)
            if b.researchgate_allowed():
                raise BrowserUnavailable(b.researchgate_allowed())
            page_url = b.researchgate_page(item)
            if not page_url or page_url in seen:
                return None
            return b.pdf_from_page() if b.goto(page_url) else None

        yield ff.Candidate(f"https://www.researchgate.net/search?q={quote(item.title[:80])}",
                           "ResearchGate, in your browser", None, fetcher=_fetcher(researchgate))

    # 3. The publisher's page, through the library proxy if one is set.
    if item.doi:
        target = f"https://doi.org/{quote(item.doi, safe='/')}"
        if settings.proxy_prefix:
            target = settings.proxy_prefix + quote(target, safe="")
        if target not in seen:
            def publisher(url=target):
                b = _session(http, settings)
                return b.pdf_from_page() if b.goto(url) else None

            yield ff.Candidate(target, "publisher, in your browser", "published", by_identifier=True,
                               fetcher=_fetcher(publisher))


def _chrome_exe() -> str | None:
    import os
    import shutil

    for base in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"), os.environ.get("LOCALAPPDATA")):
        if base:
            path = Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"
            if path.exists():
                return str(path)
    for name in ("google-chrome", "google-chrome-stable", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    mac = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    return str(mac) if mac.exists() else None


def login_session(settings: ff.Settings | None = None, log: Callable[[str], None] = print) -> None:
    """Open the fetcher's browser profile so the user can log in once.

    Chrome is started as an ordinary program here, not under automation:
    Google refuses "Sign in with Google" in an automated browser, and the
    logins made now are what the automated runs use later.
    """
    import subprocess

    settings = settings or ff.Settings.load()
    pages = ["https://www.researchgate.net/login", "https://www.academia.edu/login"]
    if settings.proxy_prefix:
        pages.append(settings.proxy_prefix + quote("https://www.sciencedirect.com/", safe=""))
    exe = _chrome_exe() if settings.browser_channel != "chromium" else None
    if exe:
        profile_dir().mkdir(parents=True, exist_ok=True)
        log("Opening Chrome with the fetcher's own profile. Log in to ResearchGate and Academia.edu,")
        log("and to publisher sites through UGent ('Access through your institution').")
        log("Close that Chrome window when you are done; the logins are kept in " + str(profile_dir()) + ".")
        proc = subprocess.Popen([exe, f"--user-data-dir={profile_dir()}", "--new-window", *pages])
        proc.wait()
        return
    b = BrowserSession(settings, log=log).start()
    b.set_window("normal")                 # logging in needs the window, also when runs keep it minimised
    b.set_window("maximized")
    pages = ["https://www.researchgate.net/login", "https://www.academia.edu/login"]
    if settings.proxy_prefix:
        pages.append(settings.proxy_prefix + quote("https://www.sciencedirect.com/", safe=""))
    b.page.goto(pages[0])
    for url in pages[1:]:
        p = b.ctx.new_page()
        try:
            p.goto(url)
        except Exception:
            pass
    log("The fetcher's Chrome window is open. Log in to ResearchGate and Academia.edu in its tabs,")
    log("and to any publisher sites you use through UGent ('Access through your institution').")
    log("The logins are kept in " + str(profile_dir()) + ".")
    try:
        input("Press Enter here when you are done ... ")
    except EOFError:
        time.sleep(600)
    b.close()


def _host(url: str) -> str:
    return (urlparse(url or "").hostname or "").removeprefix("www.")

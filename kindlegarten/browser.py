"""Browser helpers shared by the Amazon and Comipo sign-in flows."""

import sys
from contextlib import contextmanager

from kindlegarten.i18n import tr
from kindlegarten.network import browser_options
from kindlegarten.cli import KindleError, is_amazon_callback


@contextmanager
def playwright_session(command, extra="browser"):
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError:
        raise KindleError(tr("Install 'kindlegarten[{extra}]' in the current Python environment.", extra=extra)) from None
    try:
        with sync_playwright() as playwright:
            yield playwright, Error
    except Error as error:
        if "Executable doesn't exist" in str(error):
            raise KindleError(tr('Chromium was not found. Run python -m playwright install chromium and try again.')) from None
        raise KindleError(tr('The browser or network request failed. Check the connection and retry {command}.', command=command)) from None


def launch_browser(playwright, error_type, *, headless):
    try:
        return playwright.chromium.launch(channel="chrome", headless=headless, **browser_options())
    except error_type as error:
        if "not found" not in str(error).lower():
            raise
        return playwright.chromium.launch(headless=headless, **browser_options())


@contextmanager
def browser_context(playwright, error_type, *, profile=None, headless=False):
    if profile is None:
        with launch_browser(playwright, error_type, headless=headless) as browser:
            yield browser.new_context()
        return

    profile.mkdir(mode=0o700, parents=True, exist_ok=True)
    launch = playwright.chromium.launch_persistent_context
    try:
        context = launch(profile, channel="chrome", headless=headless, **browser_options())
    except error_type as error:
        if "not found" not in str(error).lower():
            raise
        context = launch(profile, headless=headless, **browser_options())
    try:
        yield context
    finally:
        context.close()


def open_amazon_login(url, site, command, *, profile=None):
    with playwright_session(command) as (playwright, error_type):
        from playwright.sync_api import TimeoutError

        with browser_context(playwright, error_type, profile=profile) as context:
            page = context.new_page()
            print(tr('Finish signing in on {site}. The login redirect is captured automatically.', site=site),
                  file=sys.stderr, flush=True)
            try:
                with context.expect_event(
                    "request",
                    predicate=lambda request: (
                        request.is_navigation_request() and is_amazon_callback(request.url, site)
                    ),
                    timeout=600_000,
                ) as callback:
                    page.goto(url, wait_until="commit")
                return callback.value.url
            except TimeoutError:
                raise KindleError(tr('Amazon sign-in timed out. Try again or use --method callback.')) from None

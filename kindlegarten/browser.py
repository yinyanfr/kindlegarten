"""Browser helpers shared by the Amazon and Comipo sign-in flows."""

import sys
from contextlib import contextmanager

from kindlegarten.cli import KindleError, is_amazon_callback


@contextmanager
def playwright_session(command, extra="browser"):
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError:
        raise KindleError(f"请在当前 Python 环境中安装 'kindlegarten[{extra}]'。") from None
    try:
        with sync_playwright() as playwright:
            yield playwright, Error
    except Error as error:
        if "Executable doesn't exist" in str(error):
            raise KindleError("找不到 Chromium。请运行 python -m playwright install chromium 后重试。") from None
        raise KindleError(f"浏览器或网络请求失败。请检查网络后重试 {command}。") from None


def launch_browser(playwright, error_type, *, headless):
    try:
        return playwright.chromium.launch(channel="chrome", headless=headless)
    except error_type as error:
        if "not found" not in str(error).lower():
            raise
        return playwright.chromium.launch(headless=headless)


def open_amazon_login(url, site, command):
    with playwright_session(command) as (playwright, error_type):
        from playwright.sync_api import TimeoutError

        with launch_browser(playwright, error_type, headless=False) as browser:
            context = browser.new_context()
            page = context.new_page()
            print(f"请在打开的 {site} 页面完成登录。终端会自动接收跳转结果。",
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
                raise KindleError("等待 Amazon 登录跳转超时。请重试，或使用 --method callback。") from None

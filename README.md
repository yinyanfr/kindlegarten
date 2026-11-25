# kindlegarten

[简体中文](https://github.com/yinyanfr/kindlegarten/blob/main/README.zh-CN.md) · [Changelog](https://github.com/yinyanfr/kindlegarten/blob/main/CHANGELOG.md)

> **Scope of use**
>
> Use this project only to back up Kindle books you have legally purchased from Amazon and comics you have legally purchased from Comipo.

Download books from your Amazon Kindle library and save them locally as EPUB or CBZ. kindlegarten downloads the Kindle for iPad edition and preserves the color illustrations Amazon supplies for that edition.

- Automatically exports text-based books as EPUB and fixed-layout comics and magazines as CBZ when their pages can be extracted as images.
- Names output files after the book title, replaces characters disallowed in filenames, and shortens long titles. One progress bar tracks the entire download.
- Supports Amazon Japan and Amazon US and saves your sign-in locally for future downloads.

Comipo comics are saved as CBZ. A series download exports the purchased volumes in volume order to a folder named after the series.

## Installation

You need Python 3.11 or later. Kindle downloads also require [Calibre](https://calibre-ebook.com/download) and a browser for Amazon sign-in. The CLI is designed for macOS, Windows, and Linux and has been tested on macOS.

```sh
python3 -m pip install kindlegarten
kindlegarten setup
```

On Windows, use `py -3 -m pip install kindlegarten`. If your Python installation is managed by your system package manager, install kindlegarten in a virtual environment or run `pipx install kindlegarten`. Run `kindlegarten setup` once to install the components used for export.

## Login methods

Running `kindlegarten login` with no options signs in to Amazon Japan. The default login method depends on the service.

| Service | Default when `--method` is omitted | Explicit methods |
| --- | --- | --- |
| Amazon Kindle | Opens your system browser and prompts you to paste the final redirect URL. | `browser` captures the redirect automatically. `callback` prints the login URL and accepts the redirect in the terminal. |
| Comipo (`--source comipo`) | Opens a Playwright browser. Sign in, return to Comipo Play, then press Enter in the terminal. | `browser` uses the default flow. `credentials` uses headless Chromium. `cookie` imports an existing Comipo Play session and needs no browser. |

Run `kindlegarten login --help` for the full list.

## Amazon Kindle

```sh
kindlegarten login --region jp
kindlegarten d B0EXAMPLE0
```

After you paste the redirect URL, the CLI registers a Kindle for iPad client. If it cannot open your system browser, it prints the sign-in URL. Over SSH, use `--method callback` to print the sign-in URL directly. Open it on another device, then paste the final redirect URL into the remote terminal. The terminal hides the pasted URL.

To let Playwright detect the redirect automatically, install the browser extra and select `--method browser`. Complete Amazon's sign-in and any verification in the window the CLI opens.

```sh
python3 -m pip install 'kindlegarten[browser]'
kindlegarten login --region jp --method browser
kindlegarten login --region jp --method callback
```

The browser method uses Chrome or Playwright's Chromium. If neither is installed, run `python -m playwright install chromium` in the CLI's Python environment.

`d` is shorthand for `download` and accepts the same options. Replace `B0EXAMPLE0` with the book's ten-character ASIN. You can also pass an Amazon product URL, which selects the region from its domain. A bare ASIN defaults to Amazon Japan. For an Amazon US account, pass `--region us` to both `login` and ASIN-based downloads.

```sh
kindlegarten download "https://www.amazon.co.jp/dp/B0EXAMPLE0"
kindlegarten download B0EXAMPLE0 --output "My Book.epub"
kindlegarten download --help
```

By default, books are saved as `books/<title>.epub` or `books/<title>.cbz` under the current directory. If the title is missing, the filename uses the ASIN. Use `--output` to specify a filename and format. Relative paths start from the current directory. CBZ export requires pages that can be safely extracted as images. The CLI reports a conflict if the output file already exists.

## Comipo

Install the Comipo extra and sign in with your viviON ID. The CLI saves a local session for later downloads.

```sh
python3 -m pip install 'kindlegarten[comipo]'
kindlegarten login --source comipo
kindlegarten work BJ123456
kindlegarten series TITLE12345678
kindlegarten logout --source comipo
```

On servers without a GUI, run `kindlegarten login --source comipo --method credentials` from an interactive terminal. This method uses viviON's official form in a headless browser. It prompts in the terminal for your login ID, password, and six-digit authenticator code when requested. Password and code input are hidden.

If you are already signed in on another machine, import that Comipo Play session from the terminal. In your browser's developer tools, open **Network**, reload Comipo Play, and copy the **Cookie** value under **Request Headers** for the `/api/comipo/authorize` request to `play.comipo.app`. Paste it when prompted. You can also import the `comipo-session.json` saved by kindlegarten. The CLI checks the session with Comipo Play before saving it. Both options work without launching a browser.

```sh
kindlegarten login --source comipo --method cookie
kindlegarten login --source comipo --method cookie --session-file ~/comipo-session.json
```

If viviON requests interactive verification such as a Cloudflare challenge, complete it in the browser before copying the Cookie or exporting the session. Keep Cookies and session files private. The final address-bar URL does not carry the Comipo Play session.

Use your own work ID or series ID. `w` is shorthand for `work`, and `s` is shorthand for `series`. The `download` and `d` commands still accept `work:ID`, `series:ID`, bare Comipo IDs such as `BJ123456` or `TITLE12345678`, Comipo Play reader and series links, and Comipo store product links.

A work is saved to `books/<work title>.cbz`. A series is saved under `books/<series name>/` with one CBZ per purchased volume. Re-running a series skips files previously saved for the same work ID. For a work, `--output` sets the CBZ filename. For a series, it sets the parent of the series folder. The `browser` and `credentials` methods use Chrome or Playwright's Chromium. If neither is available, run `python -m playwright install chromium` in the CLI's Python environment. Comipo downloads do not require Calibre or `kindlegarten setup`.

## Releasing a version

Update `__version__` in `kindlegarten/__init__.py` and add a matching `## X.Y.Z` section to `CHANGELOG.md`. A push to `main` publishes the version to PyPI and creates a GitHub Release with that changelog section. A version already available on both services is skipped. If publishing fails after the version tag is created, rerun the workflow for the tagged commit.

## Local data

The pip-installed CLI stores sign-in state and export dependencies in a per-user directory.

- macOS uses `~/Library/Application Support/kindlegarten/`.
- Windows uses `%LOCALAPPDATA%\kindlegarten\`.
- Linux uses `$XDG_DATA_HOME/kindlegarten/`, which defaults to `~/.local/share/kindlegarten/`.

Set `KINDLEGARTEN_DATA_DIR` to use a different directory. To reuse sign-in state and dependencies from a source checkout, set it to the absolute path to that checkout's `.kindle-data/` directory. Running `python3 kindle.py` directly from a checkout uses its own `.kindle-data/` and `books/` directories.

## Troubleshooting

- **Calibre cannot be found.** Set `KINDLE_CALIBRE_BIN` to the directory containing `calibre-debug` and `calibre-customize`. On Windows, these files have the `.exe` extension.
- **Amazon sign-in has expired or you want to switch accounts.** Run `kindlegarten logout --region jp` to clear the local sign-in state for Japan, then sign in again. You can deregister the device from Amazon's **Manage Your Content and Devices** page.
- **Comipo rejects a session.** Sign in again with `kindlegarten login --source comipo` or import a fresh session. Use `kindlegarten logout --source comipo` to clear the saved session.
- **A book fails to download or convert.** Check that you can open it in your Kindle library for the selected region. Format and encryption compatibility depend on the files Amazon delivers for the book.
- **Setting up Kindle downloads on another computer.** Install Calibre and kindlegarten, then run `kindlegarten setup` to install the export dependencies.

kindlegarten uses [kindl](https://github.com/bGFiMTI2/kindl) for downloading, [DeDRM Tools](https://github.com/Satsuoni/DeDRM_tools) for decryption, and [KFX Input](https://www.mobileread.com/forums/showthread.php?t=291290) for conversion. The project is licensed under the [MIT License](https://github.com/yinyanfr/kindlegarten/blob/main/LICENSE).

# kindlegarten

[简体中文](https://github.com/yinyanfr/kindlegarten/blob/main/README.zh-CN.md)

> **Scope of use**
>
> Use this project only to back up Kindle books you have legally purchased from Amazon.

Download books from your Amazon Kindle library and save them locally as EPUB or CBZ. kindlegarten downloads the Kindle for iPad edition and preserves the color illustrations Amazon supplies for that edition.

- Automatically exports text-based books as EPUB and fixed-layout comics and magazines as CBZ when their pages can be extracted as images.
- Names output files after the book title, replaces characters disallowed in filenames, and shortens long titles. One progress bar tracks the entire download.
- Supports Amazon Japan and Amazon US and saves your sign-in locally for future downloads.

## Installation

You need Python 3.11 or later, [Calibre](https://calibre-ebook.com/download), and a browser for Amazon sign-in. The CLI is designed for macOS, Windows, and Linux and has been tested on macOS.

```sh
python3 -m pip install kindlegarten
kindlegarten setup
```

On Windows, use `py -3 -m pip install kindlegarten`. If your Python installation is managed by your system package manager, install kindlegarten in a virtual environment or run `pipx install kindlegarten`. Run `kindlegarten setup` once to install the components used for export.

## Sign in and download

```sh
kindlegarten login --region jp
kindlegarten download B0EXAMPLE0
```

The login command opens Amazon in your browser and registers a Kindle for iPad client. After signing in, copy the full redirect URL from the address bar, paste it into the terminal, and press Enter. The terminal hides the pasted URL.

Replace `B0EXAMPLE0` with the book's ten-character ASIN. You can also pass an Amazon product URL, which selects the region from its domain. A bare ASIN defaults to Amazon Japan. For an Amazon US account, pass `--region us` to both `login` and ASIN-based downloads.

```sh
kindlegarten download "https://www.amazon.co.jp/dp/B0EXAMPLE0"
kindlegarten download B0EXAMPLE0 --output "My Book.epub"
kindlegarten download --help
```

By default, books are saved as `books/<title>.epub` or `books/<title>.cbz` under the current directory. If the title is missing, the filename uses the ASIN. Use `--output` to specify a filename and format. Relative paths start from the current directory. CBZ export requires pages that can be safely extracted as images. The CLI reports a conflict if the output file already exists.

## Local data

The pip-installed CLI stores sign-in state and export dependencies in a per-user directory.

- macOS uses `~/Library/Application Support/kindlegarten/`.
- Windows uses `%LOCALAPPDATA%\kindlegarten\`.
- Linux uses `$XDG_DATA_HOME/kindlegarten/`, which defaults to `~/.local/share/kindlegarten/`.

Set `KINDLEGARTEN_DATA_DIR` to use a different directory. To reuse sign-in state and dependencies from a source checkout, set it to the absolute path to that checkout's `.kindle-data/` directory. Running `python3 kindle.py` directly from a checkout uses its own `.kindle-data/` and `books/` directories.

## Troubleshooting

- **Calibre cannot be found.** Set `KINDLE_CALIBRE_BIN` to the directory containing `calibre-debug` and `calibre-customize`. On Windows, these files have the `.exe` extension.
- **Sign-in has expired or you want to switch accounts.** Run `kindlegarten logout --region jp` to clear the local sign-in state for Japan, then sign in again. You can deregister the device from Amazon's **Manage Your Content and Devices** page.
- **A book fails to download or convert.** Check that you can open it in your Kindle library for the selected region. Format and encryption compatibility depend on the files Amazon delivers for the book.
- **Setting up on another computer.** Install Calibre and kindlegarten, then run `kindlegarten setup` to install the dependencies on that computer.

kindlegarten uses [kindl](https://github.com/bGFiMTI2/kindl) for downloading, [DeDRM Tools](https://github.com/Satsuoni/DeDRM_tools) for decryption, and [KFX Input](https://www.mobileread.com/forums/showthread.php?t=291290) for conversion. The project is licensed under the [MIT License](https://github.com/yinyanfr/kindlegarten/blob/main/LICENSE).

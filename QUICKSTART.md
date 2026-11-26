# Kindlegarten CLI quick start

[简体中文](QUICKSTART.zh-CN.md)

Install Kindlegarten, sign in to the store where you bought your book, and download one title. Use Kindlegarten only to back up ebooks and comics you have legally purchased.

## 1. Install the CLI

You need Python 3.11 or later. Open a terminal in the folder where you want to keep your downloads, then install Kindlegarten in a virtual environment.

**macOS or Linux**

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install kindlegarten
kindlegarten --version
```

**Windows PowerShell**

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install kindlegarten
kindlegarten --version
```

If PowerShell blocks activation, install with `.\.venv\Scripts\python.exe -m pip install kindlegarten`. Run the examples below using `.\.venv\Scripts\kindlegarten.exe` in place of `kindlegarten` and `.\.venv\Scripts\python.exe` in place of `python`.

## 2. Sign in to Kindle

Install Calibre for Kindle export, then run setup and sign in. [Download Calibre](https://calibre-ebook.com/download)

```sh
kindlegarten setup
kindlegarten login --region jp
```

Complete sign-in in the browser window and return to the terminal. The examples use Amazon Japan. For Amazon US, use `--region us` for sign-in, listing, and download.

If browser sign-in reports that Chromium is missing, run `python -m playwright install chromium` from the same environment and try signing in again.

## 3. Download one Kindle book

List your purchases to find the book's ten-character ASIN in brackets after its title.

```sh
kindlegarten library --source amazon --region jp
```

Replace `B0EXAMPLE0` with your book's ASIN.

```sh
kindlegarten download B0EXAMPLE0 --region jp
```

The CLI prints the saved path. By default, the file goes into the `books/` folder under your current directory. A text ebook usually becomes an EPUB, and a supported image-only comic becomes a CBZ.

## Comipo and DLsite

After step 1, use the commands for your store. Replace `BJ123456` with a book or comic ID shown by `library`. Sign in in the browser window before running `library` and `work`.

**Comipo**

```sh
python -m pip install "kindlegarten[comipo]"
kindlegarten login --source comipo
kindlegarten library --source comipo
kindlegarten work BJ123456 --source comipo
```

**DLsite**

```sh
python -m pip install "kindlegarten[dlsite]"
kindlegarten login --source dlsite
kindlegarten library --source dlsite
kindlegarten work BJ123456 --source dlsite
```

These downloads also go into `books/` by default. Run `kindlegarten help download` or `kindlegarten help work` for output options. The README covers browser sign-in alternatives and series downloads. [Full documentation](README.md)

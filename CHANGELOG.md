# Changelog

## 0.3.0

- Add a native desktop bookshelf, provider sign-in, proxy settings, downloads, tutorials and GitHub release checks.
- Add a local bookshelf and cover cache, background refresh, mixed book and series sorting, and hover actions.
- Add a separate Kindle store selector with 13 stores and a bookshelf sign-in button for signed-out providers.
- Add purchased-book listing for Amazon Kindle, Comipo and DLsite, including verified series and book IDs.
- Add EPUB, PDF, CBZ and comic ZIP format selection to the desktop app and CLI.
- Localize the desktop app and CLI in English, Simplified Chinese and Japanese.
- Add Windows portable, Linux AppImage and macOS application build workflows.

## 0.2.0

### Added

- Back up purchased Comipo manga as CBZ after signing in to Comipo Play with a viviON ID.
- Download a single manga with `work` (`w`) or save every purchased volume in a series with `series` (`s`) to a folder named after the series.
- Use `d` as shorthand for `download` with Amazon and Comipo books.
- Select an Amazon login method to capture the browser redirect automatically or paste it from a separate device.
- Enter viviON ID credentials in an interactive terminal for headless Comipo sign-in when the official form is available.
- Import an existing Comipo Play session by pasting a Cookie header or loading a session file.
- Publish new versions to PyPI and GitHub Releases automatically from `main`.

### Fixed

- Use the Japanese title when Comipo returns a localized title map, with a fallback to another available title or the work ID.

## 0.1.0 - 2026-11-18

- Initial PyPI and GitHub release of the command-line tool.
- Back up books from Amazon Japan and Amazon US using the Kindle for iPad edition.
- Export text-based books as EPUB and supported image-only books as CBZ.
- Save files under their book titles and display a single download progress bar with transfer speed.

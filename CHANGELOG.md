# Changelog

## 0.6.0 - 2026-11-26

### Added

- Open Settings from the toolbar gear. Manage accounts, downloads and network settings, or open the guide and About & updates. Returning to the bookshelf preserves the current search, selection, scroll position and download queue.
- Check for stable desktop updates and download them through the configured proxy. Verify each package's size and SHA-256 before offering installation.
- Update and restart eligible desktop installations after book downloads finish. Restore the previous installation if the new app fails to start. Provide a manual installation option for other packages.
- Show the official Calibre download link and path configuration when required tools are missing. The desktop startup reminder can be turned off in Settings.

### Changed

- Consolidate DLsite and Comipo purchases under DLsite in the bookshelf. Keep `comipo` as a CLI alias and continue accepting existing Comipo links and IDs.
- Offer an opt-in fallback for missing Comipo purchases when both saved sessions belong to the same viviON ID.
- Draw series covers with tighter spacing and lighter, muted green backing sheets.

### Fixed

- Blend settings form backgrounds into their panels and keep dropdown borders fully visible on macOS.

## 0.5.0 - 2026-11-26

### Added

- Mark saved books and completed series on their covers. Partially downloaded series show the number of saved volumes.
- Add book and series context menus with download and store links, plus an action to open a series.
- Toggle the Downloads panel from the toolbar and add tasks by pasting one book or series ID per line.
- Add a Calibre path setting and the CLI `--calibre-path` flag. Kindle downloads can preserve the original `.kfx-zip` without Calibre by turning off DRM removal or passing `--no-dedrm`.
- Add English and Simplified Chinese CLI quick-start guides and show the version next to the app name and in the window title.

### Changed

- Save individual series volumes in the same folder as the complete series. The bookshelf counts series downloads from that folder.
- Update the app icon to a book in an archive box and refresh the README screenshots.
- Start with the Downloads panel collapsed and open it automatically when new tasks are queued.

### Fixed

- Translate purchased-book counts when loading a cached library at startup.
- Keep cover loading and download status independent so loading a cover leaves the downloaded badge accurate.

## 0.4.0 - 2026-11-26

- Fix desktop multi-selection so every selected book or series enters the download queue as its own task.
- Add a right-hand download queue with per-task progress, speed, cancellation, retry, and a shortcut to open saved files' folder.
- Allow 1–10 concurrent desktop downloads, with a default of 3. Series retries skip volumes already saved in the requested format.

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

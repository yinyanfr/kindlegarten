# Build the desktop app

Build on a machine or runner matching the artifact. The desktop bundle includes Python, Qt and the Kindlegarten backend. Kindle conversion uses your installed Calibre. Browser sign-in uses Chrome or Playwright Chromium.

| Target | Artifact | Build host |
| --- | --- | --- |
| Windows x64 | Portable ZIP | Windows x64 |
| Linux x86_64 | AppImage | Ubuntu 24.04 x86_64 |
| macOS Apple Silicon | ZIP containing `Kindlegarten.app` | macOS arm64 |

Install Python 3.12, then create and activate a virtual environment.

```sh
python -m venv .venv
```

Activate it with `. .venv/bin/activate` on macOS or Linux, or `.venv\Scripts\Activate.ps1` in Windows PowerShell. Run these commands from the repository root.

```sh
python -m pip install ".[gui]" "pyinstaller>=6.12,<7"
python -m unittest discover -s tests
```

On macOS and Windows, run the build directly.

```sh
python scripts/build_desktop.py
```

On Ubuntu 24.04, install the shared libraries and AppImage tools.

```sh
sudo apt-get install libpython3.12 binutils desktop-file-utils squashfs-tools fonts-dejavu-core \
  libgtk-3-0t64 libatomic1 libegl1 libgl1 libxcb-cursor0 libxkbcommon-x11-0 \
  libxcb-icccm4 libxcb-keysyms1 libxcb-shape0
```

Download the x86_64 appimagetool from the [AppImage project](https://github.com/AppImage/appimagetool/releases/tag/1.9.0), make it executable, and run:

```sh
python scripts/build_desktop.py --appimagetool /absolute/path/to/appimagetool
```

The build script verifies the pinned Kindle backend archive and installs it into the build environment as needed. Bundles are written to `dist-desktop/`. The script runs a startup smoke test against the packaged application. Linux builds target the system libraries provided by Ubuntu 24.04. The macOS build targets Apple Silicon with macOS 15 or later and uses ad hoc signing. Users distributing a notarized macOS build can sign it with their Developer ID and submit it to Apple.

During development, run `python -m kindlegarten.gui` from the checkout or `kindlegarten-gui` after installing the project. If Chrome is unavailable, install Playwright Chromium with `python -m playwright install chromium` in the source environment. For a packaged build, run `playwright install chromium` using a Python environment with the Playwright version used to build it.

The **Build native desktop apps** workflow can produce the three artifacts on demand. The release workflow uses the same build and checks before attaching artifacts to a new GitHub Release.

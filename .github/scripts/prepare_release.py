"""Plan a release without changing PyPI or GitHub."""

import json
import os
import re
import subprocess
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from kindlegarten import __version__ as version


PYPI = "https://pypi.org/pypi/kindlegarten"
STABLE_VERSION = r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){2}"


def get_json(url, headers=None):
    try:
        with urlopen(Request(url, headers=headers or {}), timeout=25) as response:
            return json.load(response)
    except HTTPError as error:
        if error.code == 404:
            return None
        raise


def release_notes():
    changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
    heading = rf"^## {re.escape(version)}(?: - \d{{4}}-\d{{2}}-\d{{2}})?[ \t]*$"
    sections = re.split(heading, changelog, flags=re.MULTILINE)
    if len(sections) == 2:
        notes = re.split(r"^##[ \t]+", sections[1], maxsplit=1, flags=re.MULTILINE)[0].strip()
        if notes:
            return f"{notes}\n\n[PyPI release](https://pypi.org/project/kindlegarten/{version}/)\n"
    raise SystemExit(f"Add one nonempty '## {version}' section to CHANGELOG.md.")


def main():
    if not re.fullmatch(STABLE_VERSION, version):
        raise SystemExit("Set kindlegarten.__version__ to a stable X.Y.Z version.")

    tag = f"v{version}"
    pypi = get_json(f"{PYPI}/{version}/json")
    filenames = {file["filename"] for file in pypi["urls"]} if pypi else set()
    expected = {f"kindlegarten-{version}.tar.gz", f"kindlegarten-{version}-py3-none-any.whl"}
    upload_pypi = not expected.issubset(filenames)
    headers = {"Accept": "application/vnd.github+json"}
    if token := os.environ.get("GH_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    github_api = os.environ["GITHUB_API_URL"]
    repository = os.environ["GITHUB_REPOSITORY"]
    github = get_json(f"{github_api}/repos/{repository}/releases/tags/{tag}", headers)
    release_github = github is None or github.get("draft", False)
    create_tag = False

    if upload_pypi or release_github:
        tagged_commit = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}"],
            capture_output=True, text=True, check=False,
        ).stdout.strip()
        if tagged_commit and tagged_commit != os.environ["GITHUB_SHA"]:
            raise SystemExit(f"{tag} points to {tagged_commit}. Rerun the workflow for that commit.")
        if not tagged_commit and (filenames or not release_github):
            raise SystemExit(f"{tag} is missing for an existing publication. Restore its original tag.")
        create_tag = not tagged_commit
        Path("release-notes.md").write_text(release_notes(), encoding="utf-8")
    else:
        print(f"{tag} is already published on PyPI and GitHub.")

    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        print(f"tag={tag}", file=output)
        print(f"create_tag={str(create_tag).lower()}", file=output)
        print(f"upload_pypi={str(upload_pypi).lower()}", file=output)
        print(f"release_github={str(release_github).lower()}", file=output)


if __name__ == "__main__":
    main()

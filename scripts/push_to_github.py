#!/usr/bin/env python3
"""One-shot script: create a GitHub repo (if missing) and push the working tree
to it as a single commit using the GitHub Git Data API. No git CLI required."""

from __future__ import annotations

import base64
import os
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
TOKEN = os.environ.get("GITHUB_TOKEN")
REPO_NAME = os.environ.get("REPO_NAME", "workspace")
REPO_PRIVATE = os.environ.get("REPO_PRIVATE", "true").lower() == "true"
REPO_DESC = os.environ.get(
    "REPO_DESC",
    "Replit project: smart-money crypto day-trading signal bot + monorepo artifacts.",
)
BRANCH = "main"

API = "https://api.github.com"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

SKIP_DIRS = {
    ".git",
    "node_modules",
    ".cache",
    ".local",
    ".agents",
    ".pythonlibs",
    ".upm",
    "__pycache__",
    ".expo",
    ".expo-shared",
    "dist",
    "out-tsc",
    "tmp",
    ".replit-artifact",
    ".vscode",
    ".idea",
    ".sass-cache",
}
SKIP_SUFFIXES = {".pyc", ".pyo", ".tsbuildinfo", ".log", ".lock"}
SKIP_FILES = {".DS_Store", "Thumbs.db", "connect.lock", "libpeerconnection.log"}
# Always include these even if suffix matches SKIP_SUFFIXES
KEEP_FILES = {"pnpm-lock.yaml", "uv.lock", "package-lock.json", "yarn.lock"}
MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MB hard cap per blob


def should_skip(path: Path) -> bool:
    rel_parts = path.relative_to(ROOT).parts
    for part in rel_parts:
        if part in SKIP_DIRS:
            return True
    name = path.name
    if name in SKIP_FILES:
        return True
    if name in KEEP_FILES:
        return False
    if path.suffix in SKIP_SUFFIXES:
        return True
    return False


def gather_files() -> list[Path]:
    out: list[Path] = []
    for p in ROOT.rglob("*"):
        if not p.is_file():
            continue
        if should_skip(p):
            continue
        try:
            if p.stat().st_size > MAX_FILE_BYTES:
                print(f"  skip (too large): {p.relative_to(ROOT)}")
                continue
        except OSError:
            continue
        out.append(p)
    return out


def get_user_login() -> str:
    r = requests.get(f"{API}/user", headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()["login"]


def ensure_repo(owner: str) -> dict:
    r = requests.get(f"{API}/repos/{owner}/{REPO_NAME}", headers=HEADERS, timeout=30)
    if r.status_code == 200:
        print(f"Repo exists: {owner}/{REPO_NAME}")
        return r.json()
    if r.status_code != 404:
        r.raise_for_status()
    print(f"Creating repo {owner}/{REPO_NAME} (private={REPO_PRIVATE})")
    r = requests.post(
        f"{API}/user/repos",
        headers=HEADERS,
        json={
            "name": REPO_NAME,
            "private": REPO_PRIVATE,
            "description": REPO_DESC,
            "auto_init": False,
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def create_blob(owner: str, repo: str, path: Path) -> str:
    data = path.read_bytes()
    payload = {
        "content": base64.b64encode(data).decode("ascii"),
        "encoding": "base64",
    }
    r = requests.post(
        f"{API}/repos/{owner}/{repo}/git/blobs",
        headers=HEADERS,
        json=payload,
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["sha"]


def get_branch_head(owner: str, repo: str) -> str | None:
    r = requests.get(
        f"{API}/repos/{owner}/{repo}/git/ref/heads/{BRANCH}",
        headers=HEADERS,
        timeout=30,
    )
    if r.status_code == 200:
        return r.json()["object"]["sha"]
    if r.status_code == 404 or r.status_code == 409:
        return None
    r.raise_for_status()
    return None


def main() -> int:
    if not TOKEN:
        print("GITHUB_TOKEN env var is required", file=sys.stderr)
        return 1

    owner = get_user_login()
    print(f"Authenticated as: {owner}")

    repo_info = ensure_repo(owner)
    repo = repo_info["name"]
    html_url = repo_info["html_url"]

    files = gather_files()
    print(f"Uploading {len(files)} files...")

    tree_entries = []
    t0 = time.time()
    for i, f in enumerate(files, 1):
        rel = f.relative_to(ROOT).as_posix()
        try:
            sha = create_blob(owner, repo, f)
        except requests.HTTPError as e:
            print(f"  blob FAILED {rel}: {e}")
            continue
        tree_entries.append(
            {"path": rel, "mode": "100644", "type": "blob", "sha": sha}
        )
        if i % 25 == 0 or i == len(files):
            print(f"  {i}/{len(files)} blobs uploaded ({time.time()-t0:.1f}s)")

    parent_sha = get_branch_head(owner, repo)
    print(f"Existing {BRANCH} head: {parent_sha or '(none, fresh repo)'}")

    tree_payload = {"tree": tree_entries}
    if parent_sha:
        # build tree from scratch (not based on parent) so deleted files vanish
        tree_payload["base_tree"] = None
    r = requests.post(
        f"{API}/repos/{owner}/{repo}/git/trees",
        headers=HEADERS,
        json={"tree": tree_entries},
        timeout=120,
    )
    r.raise_for_status()
    tree_sha = r.json()["sha"]
    print(f"Tree created: {tree_sha}")

    commit_payload = {
        "message": "Sync from Replit workspace",
        "tree": tree_sha,
    }
    if parent_sha:
        commit_payload["parents"] = [parent_sha]
    else:
        commit_payload["parents"] = []
    r = requests.post(
        f"{API}/repos/{owner}/{repo}/git/commits",
        headers=HEADERS,
        json=commit_payload,
        timeout=60,
    )
    r.raise_for_status()
    commit_sha = r.json()["sha"]
    print(f"Commit created: {commit_sha}")

    if parent_sha is None:
        r = requests.post(
            f"{API}/repos/{owner}/{repo}/git/refs",
            headers=HEADERS,
            json={"ref": f"refs/heads/{BRANCH}", "sha": commit_sha},
            timeout=30,
        )
    else:
        r = requests.patch(
            f"{API}/repos/{owner}/{repo}/git/refs/heads/{BRANCH}",
            headers=HEADERS,
            json={"sha": commit_sha, "force": True},
            timeout=30,
        )
    r.raise_for_status()
    print(f"Branch {BRANCH} updated -> {commit_sha[:7]}")
    print(f"\nDone. Repo URL: {html_url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

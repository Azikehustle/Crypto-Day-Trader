#!/usr/bin/env python3
"""Create / update a GitHub repo with the current working tree using the
GitHub Git Data API. Uploads blobs in parallel for speed.

Env vars:
  GITHUB_TOKEN   required
  REPO_NAME      default: workspace
  REPO_PRIVATE   "true" / "false", default: "true"
  REPO_DESC      optional description
"""

from __future__ import annotations

import base64
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
TOKEN = os.environ.get("GITHUB_TOKEN")
REPO_NAME = os.environ.get("REPO_NAME", "workspace")
REPO_PRIVATE = os.environ.get("REPO_PRIVATE", "true").lower() == "true"
REPO_DESC = os.environ.get(
    "REPO_DESC",
    "Smart-money crypto day-trading signal bot (synced from Replit).",
)
BRANCH = "main"
WORKERS = 16

API = "https://api.github.com"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

SKIP_DIRS = {
    ".git", "node_modules", ".cache", ".local", ".agents", ".pythonlibs",
    ".upm", "__pycache__", ".expo", ".expo-shared", "dist", "out-tsc",
    "tmp", ".replit-artifact", ".vscode", ".idea", ".sass-cache",
    "attached_assets",
}
SKIP_SUFFIXES = {".pyc", ".pyo", ".tsbuildinfo", ".log"}
SKIP_FILES = {".DS_Store", "Thumbs.db", "connect.lock", "libpeerconnection.log"}
MAX_FILE_BYTES = 50 * 1024 * 1024


def gather_files() -> list[Path]:
    out: list[Path] = []
    for root, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f in SKIP_FILES:
                continue
            if any(f.endswith(s) for s in SKIP_SUFFIXES):
                continue
            p = Path(root) / f
            try:
                if p.stat().st_size > MAX_FILE_BYTES:
                    print(f"  skip (too large): {p.relative_to(ROOT)}")
                    continue
            except OSError:
                continue
            out.append(p)
    return out


def get_user_login(sess: requests.Session) -> str:
    r = sess.get(f"{API}/user", timeout=30)
    r.raise_for_status()
    return r.json()["login"]


def ensure_repo(sess: requests.Session, owner: str) -> dict:
    r = sess.get(f"{API}/repos/{owner}/{REPO_NAME}", timeout=30)
    if r.status_code == 200:
        print(f"Repo exists: {owner}/{REPO_NAME}")
        return r.json()
    if r.status_code != 404:
        r.raise_for_status()
    print(f"Creating repo {owner}/{REPO_NAME} (private={REPO_PRIVATE})")
    r = sess.post(
        f"{API}/user/repos",
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


def create_blob(sess: requests.Session, owner: str, repo: str, path: Path) -> tuple[Path, str | None, str | None]:
    try:
        data = path.read_bytes()
        payload = {
            "content": base64.b64encode(data).decode("ascii"),
            "encoding": "base64",
        }
        r = sess.post(
            f"{API}/repos/{owner}/{repo}/git/blobs",
            json=payload,
            timeout=60,
        )
        r.raise_for_status()
        return path, r.json()["sha"], None
    except Exception as e:  # noqa: BLE001
        return path, None, str(e)


def get_branch_head(sess: requests.Session, owner: str, repo: str) -> str | None:
    r = sess.get(
        f"{API}/repos/{owner}/{repo}/git/ref/heads/{BRANCH}", timeout=30
    )
    if r.status_code == 200:
        return r.json()["object"]["sha"]
    if r.status_code in (404, 409):
        return None
    r.raise_for_status()
    return None


def main() -> int:
    if not TOKEN:
        print("GITHUB_TOKEN env var is required", file=sys.stderr)
        return 1

    sess = requests.Session()
    sess.headers.update(HEADERS)

    owner = get_user_login(sess)
    print(f"Authenticated as: {owner}")

    repo_info = ensure_repo(sess, owner)
    repo = repo_info["name"]
    html_url = repo_info["html_url"]

    files = gather_files()
    print(f"Uploading {len(files)} files with {WORKERS} workers...")

    results: dict[Path, str] = {}
    failures: list[tuple[Path, str]] = []
    t0 = time.time()
    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(create_blob, sess, owner, repo, f) for f in files]
        for fut in as_completed(futs):
            path, sha, err = fut.result()
            done += 1
            if sha:
                results[path] = sha
            else:
                failures.append((path, err or "?"))
            if done % 25 == 0 or done == len(files):
                print(f"  {done}/{len(files)} blobs ({time.time()-t0:.1f}s)")

    if failures:
        print(f"\nWARNING: {len(failures)} blob failures:")
        for p, e in failures[:10]:
            print(f"  {p.relative_to(ROOT)}: {e}")

    tree_entries = [
        {
            "path": p.relative_to(ROOT).as_posix(),
            "mode": "100644",
            "type": "blob",
            "sha": sha,
        }
        for p, sha in results.items()
    ]

    parent_sha = get_branch_head(sess, owner, repo)
    print(f"Existing {BRANCH} head: {parent_sha or '(none, fresh repo)'}")

    r = sess.post(
        f"{API}/repos/{owner}/{repo}/git/trees",
        json={"tree": tree_entries},
        timeout=120,
    )
    r.raise_for_status()
    tree_sha = r.json()["sha"]
    print(f"Tree created: {tree_sha}")

    commit_payload = {
        "message": "Sync from Replit workspace",
        "tree": tree_sha,
        "parents": [parent_sha] if parent_sha else [],
    }
    r = sess.post(
        f"{API}/repos/{owner}/{repo}/git/commits",
        json=commit_payload,
        timeout=60,
    )
    r.raise_for_status()
    commit_sha = r.json()["sha"]
    print(f"Commit created: {commit_sha}")

    if parent_sha is None:
        r = sess.post(
            f"{API}/repos/{owner}/{repo}/git/refs",
            json={"ref": f"refs/heads/{BRANCH}", "sha": commit_sha},
            timeout=30,
        )
    else:
        r = sess.patch(
            f"{API}/repos/{owner}/{repo}/git/refs/heads/{BRANCH}",
            json={"sha": commit_sha, "force": True},
            timeout=30,
        )
    r.raise_for_status()
    print(f"\nBranch {BRANCH} updated -> {commit_sha[:7]}")
    print(f"Repo URL: {html_url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

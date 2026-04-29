"""Background GitHub auto-sync for the crypto bot.

Periodically pushes the working tree to a GitHub repo using the Git Data API.
No git CLI required. Controlled via env vars:

  GITHUB_TOKEN          required (fine-grained PAT with Contents: write)
  GITHUB_REPO           "owner/repo", default "Azikehustle/Crypto-Day-Trader"
  GITHUB_SYNC_MINUTES   interval in minutes, default 60. Set to 0 to disable.
"""
from __future__ import annotations

import base64
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import requests

from logger_setup import get_logger

log = get_logger("github_sync")

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.github.com"
BRANCH = "main"
WORKERS = 16

SKIP_DIRS = {
    ".git", "node_modules", ".cache", ".local", ".agents", ".pythonlibs",
    ".upm", "__pycache__", ".expo", ".expo-shared", "dist", "out-tsc",
    "tmp", ".replit-artifact", ".vscode", ".idea", ".sass-cache",
    "attached_assets",
}
SKIP_SUFFIXES = {".pyc", ".pyo", ".tsbuildinfo", ".log"}
SKIP_FILES = {".DS_Store", "Thumbs.db", "connect.lock", "libpeerconnection.log"}
MAX_FILE_BYTES = 50 * 1024 * 1024


def _gather_files() -> list[Path]:
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
                    continue
            except OSError:
                continue
            out.append(p)
    return out


def _create_blob(sess: requests.Session, owner: str, repo: str, path: Path):
    try:
        data = path.read_bytes()
        r = sess.post(
            f"{API}/repos/{owner}/{repo}/git/blobs",
            json={
                "content": base64.b64encode(data).decode("ascii"),
                "encoding": "base64",
            },
            timeout=60,
        )
        r.raise_for_status()
        return path, r.json()["sha"], None
    except Exception as e:  # noqa: BLE001
        return path, None, str(e)


def _push_once(token: str, full_repo: str, message: str) -> Optional[str]:
    owner, repo = full_repo.split("/", 1)
    sess = requests.Session()
    sess.headers.update({
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })

    # current head
    r = sess.get(f"{API}/repos/{owner}/{repo}/git/ref/heads/{BRANCH}", timeout=30)
    parent_sha = r.json()["object"]["sha"] if r.status_code == 200 else None

    files = _gather_files()
    results: dict[Path, str] = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(_create_blob, sess, owner, repo, f) for f in files]
        for fut in as_completed(futs):
            path, sha, err = fut.result()
            if sha:
                results[path] = sha
            else:
                log.warning("blob fail %s: %s", path.name, err)

    tree_entries = [
        {
            "path": p.relative_to(ROOT).as_posix(),
            "mode": "100644",
            "type": "blob",
            "sha": sha,
        }
        for p, sha in results.items()
    ]
    r = sess.post(
        f"{API}/repos/{owner}/{repo}/git/trees",
        json={"tree": tree_entries},
        timeout=120,
    )
    r.raise_for_status()
    tree_sha = r.json()["sha"]

    # Skip commit if tree unchanged
    if parent_sha:
        rp = sess.get(
            f"{API}/repos/{owner}/{repo}/git/commits/{parent_sha}", timeout=30
        )
        if rp.status_code == 200 and rp.json().get("tree", {}).get("sha") == tree_sha:
            log.info("github_sync: no changes, skipping commit")
            return None

    r = sess.post(
        f"{API}/repos/{owner}/{repo}/git/commits",
        json={
            "message": message,
            "tree": tree_sha,
            "parents": [parent_sha] if parent_sha else [],
        },
        timeout=60,
    )
    r.raise_for_status()
    commit_sha = r.json()["sha"]

    if parent_sha is None:
        sess.post(
            f"{API}/repos/{owner}/{repo}/git/refs",
            json={"ref": f"refs/heads/{BRANCH}", "sha": commit_sha},
            timeout=30,
        ).raise_for_status()
    else:
        sess.patch(
            f"{API}/repos/{owner}/{repo}/git/refs/heads/{BRANCH}",
            json={"sha": commit_sha, "force": True},
            timeout=30,
        ).raise_for_status()
    return commit_sha


def _loop(token: str, full_repo: str, interval_sec: int) -> None:
    log.info(
        "github_sync started: %s every %d min", full_repo, interval_sec // 60
    )
    while True:
        try:
            sha = _push_once(token, full_repo, "Auto-sync from Replit bot")
            if sha:
                log.info("github_sync: pushed %s", sha[:7])
        except Exception as e:  # noqa: BLE001
            log.error("github_sync error: %s", e)
        time.sleep(interval_sec)


def start_in_background() -> None:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        log.info("github_sync disabled: no GITHUB_TOKEN")
        return
    full_repo = os.environ.get("GITHUB_REPO", "Azikehustle/Crypto-Day-Trader")
    minutes = int(os.environ.get("GITHUB_SYNC_MINUTES", "60"))
    if minutes <= 0:
        log.info("github_sync disabled: GITHUB_SYNC_MINUTES=%d", minutes)
        return
    t = threading.Thread(
        target=_loop, args=(token, full_repo, minutes * 60), daemon=True
    )
    t.start()

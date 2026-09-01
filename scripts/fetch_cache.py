#!/usr/bin/env python3
"""GitHub 릴리즈에서 cache/ 아카이브를 내려받아 배치한다.

새 장비 셋업 시 corpus 파싱·색인(build_index)을 다시 돌리는 대신,
릴리즈로 배포된 완성 캐시를 그대로 받는다.

    uv run python scripts/fetch_cache.py           # cache/ 없을 때만 다운로드
    uv run python scripts/fetch_cache.py --force   # 기존 cache/ 를 지우고 다시

비공개 레포라면 GITHUB_TOKEN 환경변수(repo 읽기 권한)를 설정하거나,
`gh auth login` 된 gh CLI가 PATH에 있으면 그걸로 받는다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

REPO = "yojun313/miraeasset_agent"
TAG = "cache-v1"
ASSET = "cache-v1.tar.zst"
CHECKSUM_ASSET = "SHA256SUMS"

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "cache"
API = f"https://api.github.com/repos/{REPO}"


def _log(msg: str) -> None:
    print(f"[fetch_cache] {msg}", flush=True)


def _gh_cli() -> str | None:
    """인증된 gh CLI가 있으면 경로를 돌려준다 (비공개 레포 대응이 가장 쉽다)."""
    gh = shutil.which("gh")
    if not gh:
        return None
    try:
        subprocess.run([gh, "auth", "status"], capture_output=True, check=True, timeout=15)
        return gh
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None


def _api_request(url: str, accept: str) -> urllib.request.Request:
    req = urllib.request.Request(url, headers={
        "Accept": accept,
        "User-Agent": "fetch-cache-script",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    return req


def _download_with_gh(gh: str, dest_dir: Path) -> None:
    _log(f"gh CLI로 다운로드: {REPO} {TAG}")
    subprocess.run(
        [gh, "release", "download", TAG, "--repo", REPO,
         "--pattern", ASSET, "--pattern", CHECKSUM_ASSET,
         "--dir", str(dest_dir), "--clobber"],
        check=True,
    )


def _download_with_api(dest_dir: Path) -> None:
    """API로 자산 목록을 조회해 스트리밍 다운로드한다.

    공개 레포는 토큰 없이도 되고, 비공개 레포는 GITHUB_TOKEN이 필요하다.
    """
    _log(f"GitHub API로 다운로드: {REPO} {TAG}")
    try:
        with urllib.request.urlopen(
            _api_request(f"{API}/releases/tags/{TAG}", "application/vnd.github+json"),
            timeout=30,
        ) as resp:
            release = json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404):
            sys.exit(
                f"[fetch_cache] 릴리즈 조회 실패 (HTTP {e.code}). 비공개 레포라면 "
                "repo 읽기 권한이 있는 GITHUB_TOKEN 환경변수를 설정하거나 "
                "`gh auth login` 후 다시 실행하세요."
            )
        raise

    assets = {a["name"]: a for a in release.get("assets", [])}
    for name in (ASSET, CHECKSUM_ASSET):
        asset = assets.get(name)
        if asset is None:
            sys.exit(f"[fetch_cache] 릴리즈에 자산 {name} 이 없습니다: {list(assets)}")
        # 자산 다운로드는 asset API 엔드포인트 + octet-stream 이 공개/비공개
        # 모두에서 동작하는 유일한 조합이다 (browser_download_url은 비공개에서 404).
        req = _api_request(asset["url"], "application/octet-stream")
        out = dest_dir / name
        _log(f"  {name} ({asset['size'] / 1e6:,.0f} MB)")
        with urllib.request.urlopen(req, timeout=60) as resp, out.open("wb") as f:
            shutil.copyfileobj(resp, f, length=1 << 20)


def _verify(dest_dir: Path) -> None:
    sums = (dest_dir / CHECKSUM_ASSET).read_text()
    expected = None
    for line in sums.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == ASSET:
            expected = parts[0]
    if not expected:
        sys.exit(f"[fetch_cache] {CHECKSUM_ASSET} 에서 {ASSET} 해시를 찾지 못했습니다")

    h = hashlib.sha256()
    with (dest_dir / ASSET).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    if h.hexdigest() != expected:
        sys.exit(
            f"[fetch_cache] 체크섬 불일치 — 다운로드가 손상됐습니다.\n"
            f"  기대: {expected}\n  실제: {h.hexdigest()}"
        )
    _log("체크섬 확인 완료")


def _extract(dest_dir: Path) -> None:
    if not shutil.which("zstd") and not shutil.which("unzstd"):
        sys.exit("[fetch_cache] zstd 가 필요합니다: sudo apt install zstd")
    _log("압축 해제 중...")
    # 아카이브는 저장소 루트 기준 'cache/...' 경로를 담고 있다
    subprocess.run(
        ["tar", "--use-compress-program=unzstd", "-xf", str(dest_dir / ASSET), "-C", str(ROOT)],
        check=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true",
                        help="기존 cache/ 를 삭제하고 다시 받는다")
    args = parser.parse_args()

    if CACHE_DIR.exists() and any(CACHE_DIR.iterdir()):
        if not args.force:
            _log(f"cache/ 가 이미 있습니다 ({CACHE_DIR}). 다시 받으려면 --force")
            return
        _log("--force: 기존 cache/ 삭제")
        shutil.rmtree(CACHE_DIR)

    with tempfile.TemporaryDirectory(prefix="cache-fetch-") as tmp:
        tmp_dir = Path(tmp)
        gh = _gh_cli()
        if gh:
            _download_with_gh(gh, tmp_dir)
        else:
            _download_with_api(tmp_dir)
        _verify(tmp_dir)
        _extract(tmp_dir)

    n_files = sum(1 for _ in CACHE_DIR.rglob("*") if _.is_file())
    _log(f"완료: {CACHE_DIR} (파일 {n_files:,}개)")


if __name__ == "__main__":
    main()

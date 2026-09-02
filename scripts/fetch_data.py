#!/usr/bin/env python3
"""GitHub 릴리즈에서 데이터(corpus/ + cache/)를 내려받아 배치한다.

corpus/ (DART 공시 원문 5.3GB)와 cache/ (파싱·색인 산출물 3.2GB)는 git에
포함되지 않는다. 새 장비에서는 이 스크립트 하나로 둘 다 복원한다.

    uv run python scripts/fetch_data.py            # 없는 디렉토리만 다운로드
    uv run python scripts/fetch_data.py --force    # 전부 지우고 다시
    uv run python scripts/fetch_data.py --only cache

비공개 레포이므로 인증이 필요하다: `gh auth login` 된 gh CLI가 PATH에 있으면
그걸 쓰고, 없으면 GITHUB_TOKEN 환경변수(repo 읽기 권한)로 API에서 받는다.
압축 해제는 시스템 zstd가 있으면 tar 파이프, 없으면 파이썬 zstandard로
동작한다 (macOS 등 zstd 미설치 환경 대응).
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
TAG = "data-v1"
CHECKSUM_ASSET = "SHA256SUMS"
# 대상 디렉토리 이름 → 릴리즈 자산 이름 (publish_data.sh 의 명명 규칙과 동일).
# 아카이브는 저장소 루트 기준 'corpus/...', 'cache/...' 경로를 담고 있다.
_VERSION = TAG.split("-", 1)[1]  # "data-v1" → "v1"
ARCHIVES = {
    "corpus": f"corpus-{_VERSION}.tar.zst",
    "cache": f"cache-{_VERSION}.tar.zst",
}

ROOT = Path(__file__).resolve().parent.parent
API = f"https://api.github.com/repos/{REPO}"


def _log(msg: str) -> None:
    print(f"[fetch_data] {msg}", flush=True)


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
        "User-Agent": "fetch-data-script",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    return req


def _download_with_gh(gh: str, dest_dir: Path, assets: list[str]) -> None:
    _log(f"gh CLI로 다운로드: {REPO} {TAG} — {', '.join(assets)}")
    cmd = [gh, "release", "download", TAG, "--repo", REPO,
           "--dir", str(dest_dir), "--clobber"]
    for name in [*assets, CHECKSUM_ASSET]:
        cmd += ["--pattern", name]
    subprocess.run(cmd, check=True)


def _download_with_api(dest_dir: Path, assets: list[str]) -> None:
    """API로 자산 목록을 조회해 스트리밍 다운로드한다.

    비공개 레포 자산은 asset API 엔드포인트 + octet-stream 조합만 동작한다
    (browser_download_url은 404).
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
                f"[fetch_data] 릴리즈 조회 실패 (HTTP {e.code}). 비공개 레포이므로 "
                "repo 읽기 권한이 있는 GITHUB_TOKEN 환경변수를 설정하거나 "
                "`gh auth login` 후 다시 실행하세요."
            )
        raise

    available = {a["name"]: a for a in release.get("assets", [])}
    for name in [*assets, CHECKSUM_ASSET]:
        asset = available.get(name)
        if asset is None:
            sys.exit(f"[fetch_data] 릴리즈에 자산 {name} 이 없습니다: {list(available)}")
        req = _api_request(asset["url"], "application/octet-stream")
        out = dest_dir / name
        _log(f"  {name} ({asset['size'] / 1e6:,.0f} MB)")
        with urllib.request.urlopen(req, timeout=60) as resp, out.open("wb") as f:
            shutil.copyfileobj(resp, f, length=1 << 20)


def _verify(dest_dir: Path, asset: str) -> None:
    expected = None
    for line in (dest_dir / CHECKSUM_ASSET).read_text().splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == asset:
            expected = parts[0]
    if not expected:
        sys.exit(f"[fetch_data] {CHECKSUM_ASSET} 에서 {asset} 해시를 찾지 못했습니다")

    h = hashlib.sha256()
    with (dest_dir / asset).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    if h.hexdigest() != expected:
        sys.exit(
            f"[fetch_data] {asset} 체크섬 불일치 — 다운로드가 손상됐습니다.\n"
            f"  기대: {expected}\n  실제: {h.hexdigest()}"
        )
    _log(f"체크섬 확인 완료: {asset}")


def _extract(archive: Path) -> None:
    _log(f"압축 해제 중: {archive.name}")
    if shutil.which("zstd") or shutil.which("unzstd"):
        subprocess.run(
            ["tar", "--use-compress-program", "zstd -d", "-xf", str(archive), "-C", str(ROOT)],
            check=True,
        )
        return

    import tarfile

    import zstandard

    with archive.open("rb") as compressed:
        reader = zstandard.ZstdDecompressor().stream_reader(compressed)
        with tarfile.open(fileobj=reader, mode="r|") as tar:
            tar.extractall(ROOT, filter="data")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true",
                        help="기존 디렉토리를 삭제하고 다시 받는다")
    parser.add_argument("--only", choices=sorted(ARCHIVES),
                        help="corpus 또는 cache 하나만 받는다")
    args = parser.parse_args()

    targets: dict[str, str] = {}
    for name, asset in ARCHIVES.items():
        if args.only and name != args.only:
            continue
        target_dir = ROOT / name
        if target_dir.exists() and any(target_dir.iterdir()):
            if not args.force:
                _log(f"{name}/ 가 이미 있습니다 — 건너뜀 (다시 받으려면 --force)")
                continue
            _log(f"--force: 기존 {name}/ 삭제")
            shutil.rmtree(target_dir)
        targets[name] = asset

    if not targets:
        _log("받을 것이 없습니다.")
        return

    # 시스템 /tmp는 tmpfs(램)인 서버가 많아 1GB급 아카이브에 "disk quota
    # exceeded"가 난다 (1GB 램 AWS 실측) — 레포와 같은 디스크에 임시 디렉토리를 만든다.
    with tempfile.TemporaryDirectory(prefix=".data-fetch-", dir=ROOT) as tmp:
        tmp_dir = Path(tmp)
        gh = _gh_cli()
        if gh:
            _download_with_gh(gh, tmp_dir, list(targets.values()))
        else:
            _download_with_api(tmp_dir, list(targets.values()))
        for name, asset in targets.items():
            _verify(tmp_dir, asset)
            _extract(tmp_dir / asset)
            n_files = sum(1 for p in (ROOT / name).rglob("*") if p.is_file())
            _log(f"완료: {name}/ (파일 {n_files:,}개)")


if __name__ == "__main__":
    main()

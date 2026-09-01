#!/usr/bin/env bash
# cache/ 를 압축해 GitHub 릴리즈로 배포한다 (fetch_cache.py 의 반대편).
#
#   bash scripts/publish_cache.sh            # 태그 cache-v1
#   bash scripts/publish_cache.sh cache-v2   # 캐시를 다시 만들었을 때 새 버전
#
# 필요: gh CLI 인증(gh auth login), zstd
set -euo pipefail

TAG="${1:-cache-v1}"
REPO="yojun313/miraeasset_agent"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ASSET="${TAG}.tar.zst"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

[ -d "$ROOT/cache" ] || { echo "cache/ 가 없습니다 — 먼저 build_index를 실행하세요" >&2; exit 1; }

echo "[publish_cache] 압축: cache/ → $ASSET"
tar -C "$ROOT" -cf - cache | zstd -T0 -8 -q -o "$WORK/$ASSET"
(cd "$WORK" && sha256sum "$ASSET" > SHA256SUMS)
ls -lh "$WORK"

echo "[publish_cache] 릴리즈 생성: $REPO @ $TAG"
gh release create "$TAG" --repo "$REPO" \
  --title "Parsed corpus cache ($TAG)" \
  --notes "$(cat <<EOF
build_index 산출물 배포본 — 새 장비에서 corpus 재파싱 없이 사용.

- cache/text/ : 평문 + .sections.json (ATOC 오프셋)
- cache/fulltext.sqlite3 : FTS5 trigram 색인

받기: \`uv run python scripts/fetch_cache.py\`
EOF
)" \
  "$WORK/$ASSET" "$WORK/SHA256SUMS"

echo "[publish_cache] 완료"

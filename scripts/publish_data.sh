#!/usr/bin/env bash
# corpus/ + cache/ 를 압축해 GitHub 릴리즈로 배포한다 (fetch_data.py 의 반대편).
#
#   bash scripts/publish_data.sh            # 태그 data-v1
#   bash scripts/publish_data.sh data-v2    # 데이터를 갱신했을 때 새 버전
#
# 필요: gh CLI 인증(gh auth login), zstd
set -euo pipefail

TAG="${1:-data-v1}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# 레포는 git remote에서 자동 감지 (GITHUB_REPO 환경변수로 강제 가능)
REPO="${GITHUB_REPO:-$(git -C "$ROOT" remote get-url origin | sed -E 's#.*github\.com[:/]##; s#\.git$##')}"
[ -n "$REPO" ] || { echo "레포를 감지하지 못했습니다 — GITHUB_REPO를 설정하세요" >&2; exit 1; }
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

for dir in corpus cache; do
  [ -d "$ROOT/$dir" ] || { echo "$dir/ 가 없습니다" >&2; exit 1; }
  asset="${dir}-${TAG#data-}.tar.zst"
  echo "[publish_data] 압축: $dir/ → $asset"
  tar -C "$ROOT" -cf - "$dir" | zstd -T0 -8 -q -o "$WORK/$asset"
done
(cd "$WORK" && sha256sum -- *.tar.zst > SHA256SUMS)
ls -lh "$WORK"

echo "[publish_data] 릴리즈 생성: $REPO @ $TAG"
gh release create "$TAG" --repo "$REPO" \
  --title "Corpus + parsed cache ($TAG)" \
  --notes "$(cat <<EOF
새 장비 셋업용 데이터 배포본 — \`uv run python scripts/fetch_data.py\` 로 받는다.

- corpus-*.tar.zst : DART 공시 원문 + manifest.jsonl + universe.csv (5.3GB)
- cache-*.tar.zst : build_index 산출물 — 평문/섹션 오프셋 + FTS5 색인 (3.2GB)
EOF
)" \
  "$WORK"/*.tar.zst "$WORK/SHA256SUMS"

echo "[publish_data] 완료"

#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
uv sync
uv run playwright install chromium
mkdir -p tools
version=2.5.1
case "$(uname -s)-$(uname -m)" in
  Darwin-arm64) platform=macos-arm64 ;;
  Darwin-x86_64) platform=macos-x64 ;;
  Linux-x86_64) platform=linux-x64 ;;
  Linux-aarch64) platform=linux-arm64 ;;
  *) echo 'Unsupported SiteOne platform' >&2; exit 1 ;;
esac
if [[ ! -x tools/siteone/siteone-crawler/siteone-crawler ]]; then
  curl --fail --location --retry 2 "https://github.com/janreges/siteone-crawler/releases/download/v${version}/siteone-crawler-v${version}-${platform}.tar.gz" -o tools/siteone.tar.gz
  mkdir -p tools/siteone
  tar -xzf tools/siteone.tar.gz -C tools/siteone
  rm tools/siteone.tar.gz
fi
if [[ ! -s tools/axe.min.js ]]; then
  curl --fail --location --retry 2 https://unpkg.com/axe-core@4.10.3/axe.min.js -o tools/axe.min.js.part
  mv tools/axe.min.js.part tools/axe.min.js
fi
echo 'Setup complete. Run: uv run ams-assess modules'

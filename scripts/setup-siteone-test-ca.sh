#!/usr/bin/env bash
# Test-only SiteOne 2.5.1 build with a fixture CA hook. Never replaces the installed crawler.
# macOS reqwest uses system trust and ignores SSL_CERT_FILE; do not change the operator keychain.
# Requires cargo >= 1.94. All source/build artifacts remain in gitignored tools/.
set -euo pipefail
cd "$(dirname "$0")/.."
source_dir=tools/siteone-test-source
if [[ ! -d "$source_dir" ]]; then
  git clone --depth 1 --branch v2.5.1 https://github.com/janreges/siteone-crawler.git "$source_dir"
fi
[[ "$(git -C "$source_dir" rev-parse HEAD)" == 12b49168679dd67934d3743895a2fabb50088c02 ]]
python3 - <<'PY'
from pathlib import Path
path = Path('tools/siteone-test-source/src/engine/http_client.rs')
text = path.read_text()
anchor = '        if let Some(proxy_str) = proxy {'
if 'AMS_SITEONE_TEST_CA' not in text:
    assert text.count(anchor) == 1
    text = text.replace(anchor, '''        // Test-only additional root; all real TLS verification stays enabled.
        if let Ok(path) = std::env::var("AMS_SITEONE_TEST_CA") {
            let pem = std::fs::read(path).expect("fixture CA file");
            let cert = reqwest::Certificate::from_pem(&pem).expect("fixture CA PEM");
            builder = builder.tls_certs_merge([cert]);
        }

''' + anchor)
    path.write_text(text)
PY
cargo build --locked --no-default-features --manifest-path "$source_dir/Cargo.toml"
mkdir -p tools/siteone-test-ca/siteone/siteone-crawler
cp "$source_dir/target/debug/siteone-crawler" tools/siteone-test-ca/siteone/siteone-crawler/siteone-crawler

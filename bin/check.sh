#!/usr/bin/env bash
# 本地和 CI 共用检查入口；普通测试不访问业务外部服务。
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-python3}"
"$PYTHON" -m unittest discover -s tests -p 'test_*.py' -v
"$PYTHON" -m py_compile app.py worker.py image_refs.py bin/check_containerd.py bin/sync_manifest.py
for script in bin/*.sh entrypoint.sh; do
    bash -n "$script"
done
node --check static/app.js
node --test tests/test_frontend.js
"$PYTHON" bin/sync_manifest.py --check

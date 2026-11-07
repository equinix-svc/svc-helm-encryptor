#!/usr/bin/env bash
# validate.sh - build encryptor.yaml, YAML-parse it, build the three images, syntax-check Day 0.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
"$ROOT/scripts/build.py"
python3 -I -c 'import yaml,sys;[d for d in yaml.safe_load_all(open(sys.argv[1]))];print("manifest parses")' "$ROOT/encryptor.yaml"
python3 -I -m py_compile "$ROOT/src/qkd-exchange/exchange.py"
for i in qkd-exchange strongswan macsec; do docker build -q -t "enc-$i:test" "$ROOT/src/$i" >/dev/null && echo "image $i builds"; done
for f in day0-encryptor-advanced.yaml day0-encryptor-basic.yaml; do
  DAY0_FILE=$f hostname=x userName=u "$ROOT/scripts/render-day0.sh" | python3 -I -c 'import yaml,sys;yaml.safe_load(sys.stdin);print("parses")' | sed "s/^/$f /"
done
echo OK

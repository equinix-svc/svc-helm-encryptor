#!/usr/bin/env bash
# push-images.sh [tag] - build the three images and push them to ghcr.io/equinix-svc/svc-helm-encryptor/<name>:<tag>.
# Needs `gh auth refresh -h github.com -s write:packages,read:packages` once. Default tag: 0.1.0 (the one encryptor.yaml uses).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TAG="${1:-0.1.0}"; REG=ghcr.io/equinix-svc/svc-helm-encryptor
gh auth token | docker login ghcr.io -u "$(gh api user -q .login)" --password-stdin
for i in qkd-exchange strongswan macsec; do
  docker build -q -t "$REG/$i:$TAG" "$ROOT/src/$i" >/dev/null
  docker push "$REG/$i:$TAG"
done
echo "pushed. Make each package public once: github.com/orgs/equinix-svc/packages -> <name> -> Package settings -> Change visibility"

#!/usr/bin/env bash
# Phase 4.1 (G1): reproducible image build (+ publish) pipeline for the three
# production images — control-plane, runtime, frontend.
#
# The output contract for deployment is *digests*, not tags: every consumer
# (Helm golden values, kind L3 gate, GitOps) references images by
# `repository@sha256:...`, so a rebuilt image is discoverable and a stale tag
# can never be reused by mistake.
#
# Usage:
#   scripts/build-images.sh                 # build only (clean room, no push)
#   scripts/build-images.sh --push          # build + push to the registry
#   scripts/build-images.sh --push --update-prod-values
#                                          # ... and bake digests into deploy/prod/values.yaml
#
# Environment:
#   AGENT_PLATFORM_REGISTRY          registry host[:port] (default localhost:5001)
#   AGENT_PLATFORM_REGISTRY_USERNAME docker login user (optional; with _PASSWORD)
#   AGENT_PLATFORM_REGISTRY_PASSWORD docker login password (optional)
#   AGENT_PLATFORM_IMAGE_TAG         image tag (default $(date +%s)-$$)
#   UV_INDEX_URL                     pip index override for the backend builds
#   NPM_REGISTRY_URL                 npm registry override for the frontend build
#   AGENT_PLATFORM_REFS_OUT          refs JSON output path (default deploy/prod/image-refs.json)
set -euo pipefail
cd "$(cd -- "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
  echo "Usage: build-images.sh [--push] [--update-prod-values]"
  echo "Build (and optionally publish) control-plane + runtime + frontend images;"
  echo "with --update-prod-values, bake the real digests into deploy/prod/values.yaml."
}

push_images=0
update_prod_values=0
case "${1:-}" in
  --help|-h) usage; exit 0 ;;
  *) 
    for arg in "$@"; do
      case "$arg" in
        --push) push_images=1 ;;
        --update-prod-values) update_prod_values=1 ;;
        *) usage >&2; exit 2 ;;
      esac
    done
    ;;
esac

for command_name in docker; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "Required command is missing: $command_name" >&2
    exit 69
  }
done

registry="${AGENT_PLATFORM_REGISTRY:-localhost:5001}"
tag="${AGENT_PLATFORM_IMAGE_TAG:-$(date +%s)-$$}"
refs_out="${AGENT_PLATFORM_REFS_OUT:-deploy/prod/image-refs.json}"
# Each repository root is the known enterprise-agent-platform namespace.
repo_root="$registry/enterprise-agent-platform"

mkdir -p "$(dirname "$refs_out")"

build_args=()
if [ -n "${UV_INDEX_URL:-}" ]; then
  build_args+=(--build-arg "UV_INDEX_URL=$UV_INDEX_URL")
fi
if [ -n "${NPM_REGISTRY_URL:-}" ]; then
  build_args+=(--build-arg "NPM_REGISTRY_URL=$NPM_REGISTRY_URL")
fi

if [ "$push_images" = "1" ]; then
  if [ -n "${AGENT_PLATFORM_REGISTRY_USERNAME:-}" ]; then
    echo "$AGENT_PLATFORM_REGISTRY_PASSWORD" | \
      docker login "$registry" --username "$AGENT_PLATFORM_REGISTRY_USERNAME" --password-stdin
  fi
fi

declare -A refs
for image_name in control-plane runtime frontend; do
  dockerfile="deploy/images/${image_name}.Dockerfile"
  image_ref="$repo_root/$image_name"
  image_tag="$image_ref:$tag"
  echo "== building $image_tag =="
  docker build "${build_args[@]}" -f "$dockerfile" -t "$image_tag" .
  if [ "$push_images" = "1" ]; then
    echo "== pushing $image_tag =="
    docker push "$image_tag"
  fi
  # RepoDigests materializes after push; before that, inspect the image ID and
  # synthesize repository@digest from the image's own sha256.
  digest="$(docker image inspect --format '{{if index .RepoDigests 0}}{{index .RepoDigests 0}}{{else}}{{.Id}}{{end}}' "$image_tag")"
  digest="${digest##*@}"
  refs["$image_name"]="$image_ref@$digest"
done

# Write the canonical refs file consumed by the prod values updater, CI
# artifacts and GitOps tooling.
python_backend="backend/.venv/bin/python"
[ -x "$python_backend" ] || python_backend="backend/.venv/Scripts/python.exe"
"$python_backend" - "$refs_out" "${refs[control-plane]}" "${refs[runtime]}" "${refs[frontend]}" <<'PY'
import json
import sys

out, control_ref, runtime_ref, frontend_ref = sys.argv[1:]


def split(ref: str) -> dict:
    repo, digest = ref.rsplit("@", 1)
    return {"repository": repo, "digest": digest}


payload = {
    "controlPlane": split(control_ref),
    "runtime": split(runtime_ref),
    "frontend": split(frontend_ref),
}
with open(out, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2)
    handle.write("\n")
print(f"image refs written to {out}")
PY

if [ "$update_prod_values" = "1" ]; then
  "$python_backend" scripts/update_image_refs.py \
    --values deploy/prod/values.yaml --refs "$refs_out"
fi

echo "== done =="
echo "  control-plane: ${refs[control-plane]}"
echo "  runtime:       ${refs[runtime]}"
echo "  frontend:      ${refs[frontend]}"
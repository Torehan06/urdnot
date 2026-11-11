#!/usr/bin/env bash
# Week 8: deploy one image of urdnot-api to one environment with Helm.
#
#   scripts/deploy.sh <dev|staging> <image-tag> [digest]
#
#   scripts/deploy.sh staging sha-66e9028 sha256:…    # an image CI published
#   IMAGE_REPOSITORY=urdnot-api scripts/deploy.sh dev week8-local   # a local build
#
# CI runs the same script (see .github/workflows/ci.yml), so a deployment that
# fails there can be reproduced locally. The image is an argument, not an edit
# to values.yaml: the pipeline knows which image it just built, the chart only
# holds a default.
#
# What makes it safe to run on every push:
#   upgrade --install   one command for the first install and every later one
#   --wait              return only when the new Pods are Ready (or time out)
#   rollback on failure if they never get Ready, Helm puts the previous
#                       revision back and this script exits non-zero
#
# Optional environment variables:
#   NAMESPACE         default urdnot-<environment>
#   IMAGE_REPOSITORY  default: the chart's (ghcr.io/torehan06/urdnot-api)
#   VALUES_EXTRA      extra values files, space-separated, applied last.
#                     Default: infra/week7/values-local.yaml (the secrets)
#   TIMEOUT           how long to wait for the rollout, default 3m
#   KUBE_CONTEXT      default: kubectl's current context

set -euo pipefail

usage() { sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }
[[ $# -ge 2 && $# -le 3 ]] || usage

environment="$1" tag="$2" digest="${3:-}"
case "$environment" in dev|staging) ;; *) usage ;; esac

cd "$(dirname "$0")/.."
chart=infra/week7/urdnot
release=urdnot
namespace="${NAMESPACE:-urdnot-$environment}"
timeout="${TIMEOUT:-3m}"

# Helm reads the context from this variable, like --kube-context.
if [[ -n "${KUBE_CONTEXT:-}" ]]; then export HELM_KUBECONTEXT="$KUBE_CONTEXT"; fi

values=(-f "infra/week7/values-$environment.yaml")
for file in ${VALUES_EXTRA-infra/week7/values-local.yaml}; do
  [[ -f "$file" ]] || { echo "values file $file not found" >&2; exit 1; }
  values+=(-f "$file")
done

# The digest is always set, even when empty: an empty value clears the digest
# pinned in values.yaml, which belongs to a different tag.
image=(--set-string "api.image.tag=$tag" --set-string "api.image.digest=$digest")
if [[ -n "${IMAGE_REPOSITORY:-}" ]]; then
  image+=(--set-string "api.image.repository=$IMAGE_REPOSITORY")
fi

# Helm 4 renamed --atomic to --rollback-on-failure. Use the one this Helm has.
# (Help text goes into a variable first: with pipefail, `helm … | grep -q` can
# fail because grep exits before helm has finished writing.)
upgrade_help="$(helm upgrade --help)"
if grep -q -- '--rollback-on-failure' <<<"$upgrade_help"; then
  rollback=--rollback-on-failure
else
  rollback=--atomic
fi

echo "deploying $tag${digest:+@$digest} to $namespace (timeout $timeout, $rollback)"

status=0
helm upgrade --install "$release" "$chart" \
  --namespace "$namespace" --create-namespace \
  "${values[@]}" "${image[@]}" \
  --wait "$rollback" --timeout "$timeout" || status=$?

# Printed on success and on failure: after a failed deploy the history shows
# the failed revision and the rollback that followed it.
helm --namespace "$namespace" history "$release" --max 5 || true

if (( status != 0 )); then
  echo "deploy of $tag to $namespace FAILED; Helm rolled back to the previous revision" >&2
  exit "$status"
fi
echo "deployed $tag to $namespace"

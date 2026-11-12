#!/usr/bin/env bash
# Week 8: measure whether a deployment loses requests.
#
#   scripts/rollout-probe.sh start [namespace]   # begin sending requests
#   scripts/deploy.sh staging <new-tag>          # ...deploy while it runs...
#   scripts/rollout-probe.sh stop  [namespace]   # count, clean up, judge
#
# `check` prints the same count as `stop` but leaves the prober running.
# `stop` and `check` exit 1 if any request failed (or none were sent), so CI
# can use them as the zero-downtime assertion. The prober itself is
# infra/week8/prober.yaml. Namespace defaults to $NAMESPACE, then urdnot-staging.

set -euo pipefail

action="${1:-}"
namespace="${2:-${NAMESPACE:-urdnot-staging}}"
pod=rollout-prober
cd "$(dirname "$0")/.."

kube() { kubectl ${KUBE_CONTEXT:+--context "$KUBE_CONTEXT"} --namespace "$namespace" "$@"; }

report() {
  local log total failed
  log="$(kube logs "$pod")"
  total="$(grep -c . <<<"$log" || true)"
  failed="$(grep -c -v ' 200$' <<<"$log" || true)"
  [[ -n "$log" ]] || { total=0; failed=0; }
  echo "rollout probe in $namespace: $total requests, $failed failed"
  if (( failed > 0 )); then
    echo "failed requests (time, HTTP status; 000 = no answer):"
    grep -v ' 200$' <<<"$log" | head -20
    return 1
  fi
  (( total > 0 )) || { echo "the prober sent no requests" >&2; return 1; }
}

case "$action" in
  start)
    kube delete pod "$pod" --ignore-not-found --wait >/dev/null
    kube apply -f infra/week8/prober.yaml >/dev/null
    kube wait --for=condition=Ready "pod/$pod" --timeout=90s >/dev/null
    # Don't start measuring before the first answers are in.
    sleep 2
    report
    ;;
  check)
    report
    ;;
  stop)
    status=0
    report || status=$?
    kube delete pod "$pod" --ignore-not-found --wait=false >/dev/null
    exit "$status"
    ;;
  *)
    sed -n '2,6p' "$0" | sed 's/^# \{0,1\}//' >&2
    exit 2
    ;;
esac

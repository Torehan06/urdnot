#!/usr/bin/env bash
# Week 9: put load on urdnot-api and watch the HorizontalPodAutoscaler.
#
#   scripts/load-test.sh [namespace]            # default: $NAMESPACE, then urdnot-staging
#   VUS=20 HOLD=5m scripts/load-test.sh
#
# It starts k6 as a Pod in the cluster (infra/week9/k6.yaml running
# infra/week9/load-test.js), prints one line every 10 seconds with the HPA's
# CPU reading and the number of Pods, prints k6's result, and then keeps
# watching until the Pods are back at the minimum.
#
# Exit status 1 if k6 missed a threshold (failed requests, or a session that
# a replica didn't know), if the number of Pods never went up, or if it
# didn't come back down in time.
#
# Optional environment variables:
#   VUS, RAMP_UP, HOLD, RAMP_DOWN, CHARGE_MS, THINK_MS   see load-test.js
#   EXPECT_SCALE_UP    1 (default) = fail if no Pod was added; 0 = don't check
#                      (a cluster without metrics-server, or a light test)
#   WAIT_SCALE_DOWN    1 (default) = afterwards wait for minReplicas; 0 = don't
#   SCALE_DOWN_TIMEOUT seconds to wait for that, default 900
#   KUBE_CONTEXT       default: kubectl's current context

set -euo pipefail

namespace="${1:-${NAMESPACE:-urdnot-staging}}"
pod=load-test
interval=10
cd "$(dirname "$0")/.."

kube() { kubectl ${KUBE_CONTEXT:+--context "$KUBE_CONTEXT"} --namespace "$namespace" "$@"; }

# "replicas ready cpu% target% desired" from the Deployment and the HPA.
# cpu is "?" while the HPA has no reading (no metrics-server, or new Pods).
snapshot() {
  local deploy hpa
  deploy="$(kube get deployment urdnot-api \
    -o jsonpath='{.status.replicas} {.status.readyReplicas}')"
  hpa="$(kube get hpa urdnot-api --ignore-not-found \
    -o jsonpath='{.status.currentMetrics[0].resource.current.averageUtilization} {.spec.metrics[0].resource.target.averageUtilization} {.status.desiredReplicas}')"
  read -r replicas ready <<<"$deploy"
  read -r cpu target desired <<<"${hpa:-}"
  # Before the first reading the HPA status has no cpu field at all.
  if [[ -z "${desired:-}" ]]; then desired="${target:-}"; target="${cpu:-}"; cpu=""; fi
  replicas="${replicas:-0}" ready="${ready:-0}"
}

line() {
  printf '%s  %-10s cpu %4s%% of request (target %s%%)  pods %s ready %s  hpa wants %s\n' \
    "$(date +%T)" "$1" "${cpu:-?}" "${target:-?}" "$replicas" "$ready" "${desired:-?}"
}

min_replicas="$(kube get hpa urdnot-api --ignore-not-found -o jsonpath='{.spec.minReplicas}')"
if [[ -z "$min_replicas" ]]; then
  echo "no HorizontalPodAutoscaler urdnot-api in $namespace (api.autoscaling.enabled=false?)" >&2
  [[ "${EXPECT_SCALE_UP:-1}" == 0 ]] || exit 1
fi

settings=()
for name in VUS RAMP_UP HOLD RAMP_DOWN CHARGE_MS THINK_MS; do
  [[ -n "${!name:-}" ]] && settings+=("--from-literal=$name=${!name}")
done

kube delete pod "$pod" --ignore-not-found --wait >/dev/null
kube create configmap load-test-script --from-file=infra/week9/load-test.js \
  --dry-run=client -o yaml | kube apply -f - >/dev/null
# ${settings[@]+…}: an empty array is an "unbound variable" in bash 3.2 (macOS).
kube create configmap load-test-settings ${settings[@]+"${settings[@]}"} \
  --dry-run=client -o yaml | kube apply -f - >/dev/null
kube apply -f infra/week9/k6.yaml >/dev/null

snapshot
start_replicas="$replicas" peak_replicas="$replicas"
line "start"

# Phase 1: until the k6 Pod has finished.
phase=""
while true; do
  phase="$(kube get pod "$pod" -o jsonpath='{.status.phase}')"
  [[ "$phase" == Succeeded || "$phase" == Failed ]] && break
  sleep "$interval"
  snapshot
  (( replicas > peak_replicas )) && peak_replicas="$replicas"
  line "load"
done

echo
echo "k6 result:"
kube logs "$pod" | sed 's/^/  /'
echo

status=0
if [[ "$phase" != Succeeded ]]; then
  echo "FAIL: k6 missed a threshold or could not run (Pod phase $phase)" >&2
  status=1
fi
if [[ "${EXPECT_SCALE_UP:-1}" != 0 ]]; then
  if (( peak_replicas > start_replicas )); then
    echo "ok: scaled up from $start_replicas to $peak_replicas Pods under load"
  else
    echo "FAIL: still $start_replicas Pods at the end of the load; the HPA never scaled up" >&2
    status=1
  fi
fi

# Phase 2: the load is gone; wait for the HPA to remove the extra Pods.
if [[ "${WAIT_SCALE_DOWN:-1}" != 0 && -n "$min_replicas" ]]; then
  deadline=$(( SECONDS + ${SCALE_DOWN_TIMEOUT:-900} ))
  while snapshot; (( replicas > min_replicas )); do
    if (( SECONDS >= deadline )); then
      echo "FAIL: still $replicas Pods ${SCALE_DOWN_TIMEOUT:-900}s after the load ended (minimum $min_replicas)" >&2
      status=1
      break
    fi
    line "cool-down"
    sleep "$interval"
  done
  if (( replicas <= min_replicas )); then
    line "done"
    echo "ok: back to $replicas Pods (minReplicas $min_replicas)"
  fi
fi

kube delete pod "$pod" --ignore-not-found --wait=false >/dev/null
kube delete configmap load-test-script load-test-settings --ignore-not-found >/dev/null
exit "$status"

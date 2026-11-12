// Week 9: k6 load test for urdnot-api. It does two jobs at once:
//
//   1. Load. Every virtual user (VU) calls /api/charge, which burns CPU, so
//      the HorizontalPodAutoscaler has something to react to.
//   2. Statelessness. Every VU logs in once and then reads its session on
//      every iteration. Requests are spread over all replicas, including ones
//      the autoscaler adds in the middle of the test. A session that lived in
//      one Pod's memory would be answered with 401 by the others.
//
// Run it with scripts/load-test.sh, which passes these settings:
//
//   BASE_URL   default http://urdnot-api (the Service, from inside the cluster)
//   VUS        virtual users at the peak, default 10
//   RAMP_UP    time to go from 0 to VUS, default 30s
//   HOLD       time at the peak, default 3m
//   RAMP_DOWN  time to go back to 0, default 15s
//   CHARGE_MS  CPU milliseconds each /api/charge request burns, default 100
//   THINK_MS   pause between a VU's iterations, default 300
import http from 'k6/http';
import { check, fail, sleep } from 'k6';

const BASE_URL = __ENV.BASE_URL || 'http://urdnot-api';
const CHARGE_MS = __ENV.CHARGE_MS || '100';
const THINK_MS = Number(__ENV.THINK_MS || '300');

export const options = {
  stages: [
    { duration: __ENV.RAMP_UP || '30s', target: Number(__ENV.VUS || '10') },
    { duration: __ENV.HOLD || '3m', target: Number(__ENV.VUS || '10') },
    { duration: __ENV.RAMP_DOWN || '15s', target: 0 },
  ],
  // A Service balances connections, not requests. With keep-alive each VU
  // would stay on the Pod it first connected to and new Pods would get no
  // traffic. A new connection per request lets every request be routed again.
  noConnectionReuse: true,
  // By default k6 empties a VU's cookie jar after every iteration, as if a
  // new visitor arrived each time. Keep it: one VU is one logged-in user.
  noCookiesReset: true,
  // The test fails (k6 exits 99) if either is missed.
  thresholds: {
    http_req_failed: ['rate<0.01'],
    checks: ['rate>0.99'],
  },
};

export default function () {
  // Each VU has its own cookie jar, like one browser. Log in on the first
  // iteration only; k6 sends the session cookie back on later requests.
  const name = `vu-${__VU}`;
  if (__ITER === 0) {
    const login = http.post(`${BASE_URL}/api/session`, JSON.stringify({ name }), {
      headers: { 'Content-Type': 'application/json' },
    });
    if (!check(login, { 'login 201': (r) => r.status === 201 })) {
      fail(`login failed with ${login.status}`);
    }
  }

  const session = http.get(`${BASE_URL}/api/session`);
  check(session, {
    'session 200 on whichever replica answered': (r) => r.status === 200,
    'session is mine': (r) => r.status === 200 && r.json('name') === name,
  });

  const charge = http.get(`${BASE_URL}/api/charge?ms=${CHARGE_MS}`);
  check(charge, { 'charge 200': (r) => r.status === 200 });

  sleep(THINK_MS / 1000);
}

// Printed after k6's own summary: how many different Pods answered.
export function handleSummary(data) {
  return { stdout: textSummary(data) };
}

function textSummary(data) {
  const m = data.metrics;
  const ms = (v) => `${v.toFixed(0)} ms`;
  const lines = [
    `requests        ${m.http_reqs.values.count} (${m.http_reqs.values.rate.toFixed(1)}/s)`,
    `failed          ${(m.http_req_failed.values.rate * 100).toFixed(2)} %`,
    `checks passed   ${(m.checks.values.rate * 100).toFixed(2)} %`,
    `duration        median ${ms(m.http_req_duration.values.med)}, p95 ${ms(m.http_req_duration.values['p(95)'])}, max ${ms(m.http_req_duration.values.max)}`,
  ];
  for (const [name, t] of Object.entries(m)) {
    for (const [rule, result] of Object.entries(t.thresholds || {})) {
      lines.push(`threshold       ${name} ${rule}: ${result.ok ? 'ok' : 'FAILED'}`);
    }
  }
  return lines.join('\n') + '\n';
}

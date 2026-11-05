// Load test for the idempotent write path.
//
// Each iteration: create an order with a fresh Idempotency-Key, retry it with
// the same key (must replay the same order_id), then read it back.
//
//   k6 run loadtest/orders.js                          # 10 VUs, 30s, 0.1s think time
//   k6 run -e VUS=50 -e SLEEP=0 loadtest/orders.js     # saturation run
//   k6 run -e BASE_URL=http://<alb-dns> loadtest/orders.js
//
// The API rate-limits per client IP; run it with RATE_LIMIT_ENABLED=false
// (or a high RATE_LIMIT) or every request after the 10th will be a 429.
import http from 'k6/http';
import { check, sleep } from 'k6';
import { Trend } from 'k6/metrics';

const BASE = __ENV.BASE_URL || 'http://localhost:8080';
const createLatency = new Trend('order_create_duration', true);
const replayLatency = new Trend('order_replay_duration', true);

export const options = {
  vus: Number(__ENV.VUS || 10),
  duration: __ENV.DURATION || '30s',
  thresholds: {
    http_req_failed: ['rate<0.01'],
    checks: ['rate>0.99'],
  },
  summaryTrendStats: ['avg', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
};

const params = (key) => ({
  headers: { 'Content-Type': 'application/json', 'Idempotency-Key': key },
});

export default function () {
  const key = `k6-${__VU}-${__ITER}-${Date.now()}`;
  const body = JSON.stringify({
    customer_id: `cust-${__VU}`,
    item_id: `item-${__ITER % 100}`,
    quantity: 1 + (__ITER % 5),
  });

  let res = http.post(`${BASE}/orders`, body, params(key));
  createLatency.add(res.timings.duration);
  const ok = check(res, { 'create 201': (r) => r.status === 201 });
  if (!ok) return;
  const orderId = res.json('order_id');

  res = http.post(`${BASE}/orders`, body, params(key));
  replayLatency.add(res.timings.duration);
  check(res, {
    'replay 201': (r) => r.status === 201,
    'replay same order': (r) => r.json('order_id') === orderId,
  });

  res = http.get(`${BASE}/orders/${orderId}`);
  check(res, { 'get 200': (r) => r.status === 200 });

  sleep(Number(__ENV.SLEEP || 0.1));
}

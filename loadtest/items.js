// Original load test: /health + /items. Kept for comparison with orders.js.
import http from 'k6/http';
import { check, sleep } from 'k6';

const BASE = __ENV.BASE_URL || 'http://localhost:8080';

export const options = {
  vus: Number(__ENV.VUS || 10),
  duration: __ENV.DURATION || '30s',
};

export default function () {
  let res = http.get(`${BASE}/health`);
  check(res, { 'health 200': (r) => r.status === 200 });

  const payload = JSON.stringify({
    name: `item-${__VU}-${__ITER}`,
    value: Math.floor(Math.random() * 1000),
  });
  res = http.post(`${BASE}/items`, payload, {
    headers: { 'Content-Type': 'application/json' },
  });
  check(res, { 'create 201': (r) => r.status === 201 });

  if (res.status === 201) {
    const id = JSON.parse(res.body).id;
    res = http.get(`${BASE}/items/${id}`);
    check(res, { 'get 200': (r) => r.status === 200 });
  }

  sleep(Number(__ENV.SLEEP || 0.1));
}

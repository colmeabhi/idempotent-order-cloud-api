import http from 'k6/http';
import { check, sleep } from 'k6';

export const options = {
  vus: 10,
  duration: '30s',
};

export default function () {
  let res = http.get('http://localhost:8080/health');
  check(res, { 'health 200': (r) => r.status === 200 });

  const payload = JSON.stringify({
    name: `item-${__VU}-${__ITER}`,
    value: Math.floor(Math.random() * 1000),
  });
  res = http.post('http://localhost:8080/items', payload, {
    headers: { 'Content-Type': 'application/json' },
  });
  check(res, { 'create 201': (r) => r.status === 201 });

  if (res.status === 201) {
    const id = JSON.parse(res.body).id;
    res = http.get(`http://localhost:8080/items/${id}`);
    check(res, { 'get 200': (r) => r.status === 200 });
  }

  sleep(0.1);
}
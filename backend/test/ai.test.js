import test from 'node:test';
import assert from 'node:assert/strict';
import handler, { buildRequest, completedText } from '../api/ai.js';

const body = { operation: 'text', text: '합성 안내', mode: '요약', audience: '교직원' };
const completed = { status: 'completed', output: [{ type: 'message', status: 'completed',
  content: [{ type: 'output_text', text: '합성 결과' }] }] };
function response() {
  return { headers: {}, setHeader(k, v) { this.headers[k] = v; }, end(data) { this.data = JSON.parse(data); } };
}

test('fixed model, no storage/tools, reject injected options and remote images', () => {
  const request = buildRequest(body);
  assert.equal(request.model, 'gpt-5-nano');
  assert.equal(request.store, false);
  assert.equal(request.max_output_tokens, undefined);
  assert.throws(() => buildRequest({ ...body, model: 'other' }));
  assert.throws(() => buildRequest({ ...body, mode: 'unknown' }));
  assert.throws(() => buildRequest({ operation: 'ocr', image: 'https://example.test/private' }));
});
test('refused, incomplete, empty output never becomes a result', () => {
  assert.equal(completedText(completed), '합성 결과');
  assert.throws(() => completedText({ ...completed, status: 'incomplete' }));
  assert.throws(() => completedText({ status: 'completed', output: [] }));
  assert.throws(() => completedText({ status: 'completed', output: [{ type: 'message', status: 'completed', content: [{ type: 'refusal' }] }] }));
});
test('missing key and method block before upstream', async () => {
  delete process.env.OPENAI_API_KEY;
  let res = response();
  await handler({ method: 'POST', headers: {}, body }, res);
  assert.equal(res.statusCode, 503);
  res = response();
  await handler({ method: 'GET', headers: {} }, res);
  assert.equal(res.statusCode, 405);
});
test('only server key goes upstream; response has text and timing, no key', async () => {
  delete process.env.SSOKLY_RELAY_ENABLED;
  process.env.OPENAI_API_KEY = 'synthetic-server-key';
  const original = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async (url, options) => {
    calls++;
    assert.equal(url, 'https://api.openai.com/v1/responses');
    assert.equal(options.headers.Authorization, 'Bearer synthetic-server-key');
    assert.equal(options.redirect, 'error');
    return { ok: true, json: async () => completed };
  };
  try {
    const res = response();
    await handler({ method: 'POST', headers: { 'content-type': 'application/json' }, body }, res);
    assert.equal(res.statusCode, 200);
    assert.equal(res.data.text, '합성 결과');
    assert.equal(calls, 1);
    assert.equal(res.headers['Cache-Control'], 'no-store');
    assert.ok(!JSON.stringify(res).includes('synthetic-server-key'));
    globalThis.fetch = async () => { throw Error('synthetic-secret-error'); };
    const failure = response();
    await handler({ method: 'POST', headers: { 'content-type': 'application/json' }, body }, failure);
    assert.equal(failure.statusCode, 502);
    assert.ok(!JSON.stringify(failure).includes('synthetic-secret'));
  } finally { globalThis.fetch = original; }
});
test('former size limits are removed; malformed requests still fail', async () => {
  process.env.OPENAI_API_KEY = 'synthetic-server-key';
  const largeText = '합성'.repeat(60_000);
  assert.equal(buildRequest({ ...body, text: largeText }).input[0].content[0].text, largeText);
  const png = Buffer.concat([Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]), Buffer.alloc(3_050_000)]);
  const image = 'data:image/png;base64,' + png.toString('base64');
  assert.ok(image.length > 4_000_000);
  assert.equal(buildRequest({ operation: 'ocr', image }).input[0].content[1].image_url, image);
  const bad = response();
  await handler({ method: 'POST', headers: { 'content-type': 'application/json' }, body: '{' }, bad);
  assert.equal(bad.statusCode, 400);
});
test('prototype keys are rejected; long text is not capped', () => {
  for (const bad of [{ mode: 'constructor', audience: 'name' }, { mode: '요약', audience: 'constructor' },
    { mode: '__proto__', audience: 'x' }]) {
    assert.throws(() => buildRequest({ ...body, ...bad }));
  }
  assert.doesNotThrow(() => buildRequest({ ...body, text: 'x'.repeat(500_000) }));
});

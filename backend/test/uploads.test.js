import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { temporaryImages, signReceipt, verifyReceipt, PREFIX } from '../lib/temp-images.js';
import { aiHandler } from '../api/ai.js';
import { uploadHandler } from '../api/upload.js';
import { cleanupHandler } from '../api/cleanup.js';

process.env.CRON_SECRET = 'synthetic-cleanup-secret';
process.env.OPENAI_API_KEY = 'synthetic-openai-key';
const png = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10, 1]);
function record() {
  return { pathname: PREFIX + Date.now() + '-12345678-1234-1234-1234-123456789012.png',
    bytes: png.length, sha256: createHash('sha256').update(png).digest('hex'), expires: Date.now() + 60_000 };
}
function response() {
  return { headers: {}, setHeader(k, v) { this.headers[k] = v; }, end(data) { this.data = JSON.parse(data); } };
}
function request(body) { return { method: 'POST', headers: { 'content-type': 'application/json' }, body }; }

test('upload delegation is private, exact path, put only, exact byte count, short lived', async () => {
  const config = {};
  const storage = temporaryImages({
    issueSignedToken: async options => { config.token = options; return {}; },
    presignUrl: async (_, options) => { config.url = options; return { presignedUrl: 'https://vercel.com/api/blob/?signed' }; }
  });
  const grant = await storage.issue(record());
  const claim = verifyReceipt(grant.receipt);
  assert.deepEqual(config.token.operations, ['put']);
  assert.equal(config.token.pathname, claim.pathname);
  assert.equal(config.token.maximumSizeInBytes, png.length);
  assert.ok(config.token.validUntil <= Date.now() + 600_000);
  assert.equal(config.url.access, 'private');
  assert.equal(config.url.allowOverwrite, false);
  assert.equal(config.url.addRandomSuffix, false);
  assert.ok(!JSON.stringify(grant).includes('synthetic-cleanup-secret'));
});
test('receipt forgery, expiration, external URLs and paths are rejected', () => {
  const valid = signReceipt(record());
  assert.throws(() => verifyReceipt(valid + 'x'));
  assert.throws(() => verifyReceipt(signReceipt({ ...record(), expires: 1 })));
  for (const pathname of ['https://example.test/private', '../secret', 'other/image.png']) {
    assert.throws(() => verifyReceipt(signReceipt({ ...record(), pathname })));
  }
  assert.doesNotThrow(() => verifyReceipt(signReceipt({ ...record(), expires: Date.now() - 1000 }), { cleanup: true }));
});
test('private read checks exact length, SHA and PNG before OpenAI', async () => {
  let options;
  const storage = temporaryImages({ get: async (_, value) => {
    options = value;
    return { statusCode: 200, stream: new Blob([png]).stream() };
  } });
  assert.equal(await storage.read(record()), 'data:image/png;base64,' + png.toString('base64'));
  assert.equal(options.access, 'private');
  assert.equal(options.useCache, false);
  await assert.rejects(storage.read({ ...record(), bytes: 2 }));
  await assert.rejects(storage.read({ ...record(), sha256: '0'.repeat(64) }));
});
test('OCR deletes private object on success, provider failure and read failure', async () => {
  const original = globalThis.fetch;
  try {
    for (const scenario of ['success', 'provider_failure', 'read_failure']) {
      let deleted = 0;
      let fetched = 0;
      globalThis.fetch = async (_, options) => {
        fetched++;
        assert.equal(JSON.parse(options.body).input[0].content[1].image_url, 'data:image/png;base64,' + png.toString('base64'));
        return { ok: scenario === 'success', status: 500, json: async () => ({ status: 'completed', output: [
          { type: 'message', status: 'completed', content: [{ type: 'output_text', text: 'synthetic' }] }
        ] }) };
      };
      const handler = aiHandler({
        read: async () => { if (scenario === 'read_failure') throw Error('private'); return 'data:image/png;base64,' + png.toString('base64'); },
        remove: async () => { deleted++; }
      });
      const res = response();
      await handler(request({ operation: 'ocr_blob', receipt: signReceipt(record()) }), res);
      assert.equal(res.statusCode, scenario === 'success' ? 200 : 502);
      assert.equal(deleted, 1);
      assert.equal(fetched, scenario === 'read_failure' ? 0 : 1);
    }
  } finally { globalThis.fetch = original; }
});
test('untrusted reference causes no read, delete, or AI request', async () => {
  const handler = aiHandler({ read: () => assert.fail(), remove: () => assert.fail() });
  const res = response();
  await handler(request({ operation: 'ocr_blob', receipt: 'forged' }), res);
  assert.equal(res.statusCode, 400);
});
test('client cleanup is limited to signed owned object', async () => {
  let path;
  const handler = uploadHandler({ remove: async value => { path = value.pathname; } });
  let res = response();
  const claim = record();
  await handler(request({ action: 'delete', receipt: signReceipt(claim) }), res);
  assert.equal(path, claim.pathname);
  assert.equal(res.statusCode, 200);
  path = null;
  res = response();
  await handler(request({ action: 'delete', receipt: 'forged' }), res);
  assert.equal(path, null);
  assert.equal(res.statusCode, 400);
});
test('sweep only removes expired owned uploads, paginates, retains active/unrelated objects', async () => {
  const now = Date.now();
  const stale = { pathname: record().pathname, uploadedAt: new Date(now - 7200_000) };
  const removed = [];
  let pages = 0;
  const storage = temporaryImages({
    list: async options => {
      assert.equal(options.prefix, PREFIX);
      pages++;
      return pages === 1 ? { blobs: [stale, { ...stale, pathname: 'other/private.png' },
        { ...stale, uploadedAt: new Date(now) }], hasMore: true, cursor: 'next' } : { blobs: [], hasMore: false };
    },
    del: async paths => removed.push(...paths)
  });
  assert.equal(await storage.sweep(now), 1);
  assert.deepEqual(removed, [stale.pathname]);
  assert.equal(pages, 2);
});
test('scheduled cleanup requires server secret', async () => {
  const handler = cleanupHandler({ sweep: async () => 2 });
  let res = response();
  await handler({ method: 'GET', headers: {} }, res);
  assert.equal(res.statusCode, 401);
  res = response();
  await handler({ method: 'GET', headers: { authorization: 'Bearer synthetic-cleanup-secret' } }, res);
  assert.equal(res.data.removed, 2);
});

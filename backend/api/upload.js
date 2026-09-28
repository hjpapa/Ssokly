import { temporaryImages, verifyReceipt } from '../lib/temp-images.js';
import { readJson, reply } from '../lib/http.js';

export function uploadHandler(storage = temporaryImages()) {
  return async (req, res) => {
    if (req.method !== 'POST') return reply(res, 405, { error: 'method_not_allowed' });
    if (!process.env.CRON_SECRET) return reply(res, 503, { error: 'storage_unavailable' });
    let body;
    let record;
    try {
      body = await readJson(req);
      if (body?.action === 'delete') record = verifyReceipt(body.receipt, { cleanup: true });
      else if (body?.action !== 'create' || !Number.isSafeInteger(body.bytes) || body.bytes <= 0
        || typeof body.sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(body.sha256)) throw Error('invalid');
    } catch {
      return reply(res, 400, { error: 'invalid_request' });
    }
    try {
      if (record) {
        await storage.remove(record);
        return reply(res, 200, { deleted: true });
      }
      return reply(res, 200, await storage.issue(body));
    } catch {
      return reply(res, 502, { error: 'storage_failed' });
    }
  };
}
export default uploadHandler();

import { timingSafeEqual } from 'node:crypto';
import { temporaryImages } from '../lib/temp-images.js';
import { reply } from '../lib/http.js';

export function cleanupHandler(storage = temporaryImages()) {
  return async (req, res) => {
    const secret = process.env.CRON_SECRET;
    const supplied = Buffer.from(String(req.headers.authorization ?? ''));
    const expected = Buffer.from('Bearer ' + secret);
    if (!secret || supplied.length !== expected.length || !timingSafeEqual(supplied, expected)) {
      return reply(res, 401, { error: 'unauthorized' });
    }
    if (req.method !== 'GET') return reply(res, 405, { error: 'method_not_allowed' });
    try {
      return reply(res, 200, { removed: await storage.sweep() });
    } catch {
      return reply(res, 502, { error: 'cleanup_failed' });
    }
  };
}
export default cleanupHandler();

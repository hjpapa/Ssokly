import { createHash, createHmac, randomUUID, timingSafeEqual } from 'node:crypto';
import { issueSignedToken, presignUrl, get, del, list } from '@vercel/blob';

export const PREFIX = 'ssokly-ocr/';
const OWNED = /^ssokly-ocr\/\d{13}-[0-9a-f-]{36}\.png$/;
const PNG = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]);

function mac(value) {
  if (!process.env.CRON_SECRET) throw Error('storage_not_configured');
  return createHmac('sha256', process.env.CRON_SECRET).update('ssokly-upload-v1:' + value).digest();
}

export function signReceipt(record) {
  const value = Buffer.from(JSON.stringify(record)).toString('base64url');
  return value + '.' + mac(value).toString('base64url');
}

export function verifyReceipt(receipt, { cleanup = false, now = Date.now() } = {}) {
  if (typeof receipt !== 'string' || receipt.length > 2048) throw Error('invalid_receipt');
  const [value, signature, extra] = receipt.split('.');
  if (extra || !signature) throw Error('invalid_receipt');
  const supplied = Buffer.from(signature, 'base64url');
  const expected = mac(value);
  if (supplied.length !== expected.length || !timingSafeEqual(supplied, expected)) throw Error('invalid_receipt');
  const record = JSON.parse(Buffer.from(value, 'base64url').toString('utf8'));
  if (!OWNED.test(record.pathname) || !Number.isSafeInteger(record.bytes) || record.bytes <= 0
      || !/^[a-f0-9]{64}$/.test(record.sha256) || !Number.isSafeInteger(record.expires)
      || record.expires + (cleanup ? 86400_000 : 0) < now) throw Error('expired_or_invalid_receipt');
  return record;
}

// Injectable SDK methods keep failure/cleanup tests completely offline.
export function temporaryImages(sdk = { issueSignedToken, presignUrl, get, del, list }) {
  return {
    async issue({ bytes, sha256 }) {
      if (!Number.isSafeInteger(bytes) || bytes <= 0 || !/^[a-f0-9]{64}$/.test(sha256)) throw Error('invalid_image');
      const now = Date.now();
      const pathname = PREFIX + now + '-' + randomUUID() + '.png';
      const receipt = signReceipt({ pathname, bytes, sha256, expires: now + 20 * 60_000 });
      const token = await sdk.issueSignedToken({ pathname, operations: ['put'],
        validUntil: now + 10 * 60_000, allowedContentTypes: ['image/png'], maximumSizeInBytes: bytes });
      const { presignedUrl } = await sdk.presignUrl(token, { operation: 'put', pathname, access: 'private',
        addRandomSuffix: false, allowOverwrite: false, cacheControlMaxAge: 60,
        maximumSizeInBytes: bytes, allowedContentTypes: ['image/png'] });
      return { upload_url: presignedUrl, receipt };
    },
    async read(record) {
      const result = await sdk.get(record.pathname, { access: 'private', useCache: false });
      if (!result || result.statusCode !== 200) throw Error('image_missing');
      const reader = result.stream.getReader();
      const chunks = [];
      let size = 0;
      try {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          size += value.length;
          if (size > record.bytes) throw Error('image_changed');
          chunks.push(Buffer.from(value));
        }
      } finally {
        await reader.cancel();
        reader.releaseLock();
      }
      const data = Buffer.concat(chunks);
      if (size !== record.bytes || !data.subarray(0, 8).equals(PNG)
          || createHash('sha256').update(data).digest('hex') !== record.sha256) throw Error('image_changed');
      return 'data:image/png;base64,' + data.toString('base64');
    },
    async remove(record) {
      if (!OWNED.test(record.pathname)) throw Error('invalid_path');
      await sdk.del(record.pathname);
    },
    async sweep(now = Date.now()) {
      let cursor;
      let removed = 0;
      do {
        const page = await sdk.list({ prefix: PREFIX, limit: 1000, cursor });
        const stale = page.blobs.filter(blob => OWNED.test(blob.pathname)
          && new Date(blob.uploadedAt).getTime() < now - 3600_000);
        if (stale.length) {
          await sdk.del(stale.map(blob => blob.pathname));
          removed += stale.length;
        }
        cursor = page.hasMore ? page.cursor : undefined;
      } while (cursor);
      return removed;
    }
  };
}

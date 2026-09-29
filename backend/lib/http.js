import { createHash, timingSafeEqual } from 'node:crypto';

export function reply(res, status, body) {
  res.setHeader('Content-Type', 'application/json; charset=utf-8');
  res.setHeader('Cache-Control', 'no-store');
  res.setHeader('X-Content-Type-Options', 'nosniff');
  res.statusCode = status;
  res.end(JSON.stringify(body));
}

export async function readJson(req) {
  let body = req.body;
  if (body === undefined) {
    const chunks = [];
    for await (const chunk of req) chunks.push(Buffer.from(chunk));
    body = Buffer.concat(chunks).toString('utf8');
  }
  if (Buffer.isBuffer(body)) body = body.toString('utf8');
  return typeof body === 'string' ? JSON.parse(body) : body;
}

// Optional shared app token. When SSOKLY_APP_TOKEN is set on the server, every
// billable route requires a matching X-Ssokly-Token header. Unset keeps the open behaviour.
export function authorized(req) {
  const expected = process.env.SSOKLY_APP_TOKEN;
  if (!expected) return true;
  const digest = value => createHash('sha256').update(String(value)).digest();
  return timingSafeEqual(digest(req.headers?.['x-ssokly-token'] ?? ''), digest(expected));
}

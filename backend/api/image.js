import prompts from '../prompts.json' with { type: 'json' };
import { readJson, reply } from '../lib/http.js';

export function imageRequest(body) {
  if (!body || Array.isArray(body) || !Object.hasOwn(body, 'text') ||
      Object.keys(body).some(k => k !== 'text') ||
      typeof body.text !== 'string' || !body.text.trim()) throw Error('invalid');
  return {model: 'gpt-image-2.5-flare', prompt: prompts.work_image + JSON.stringify(body.text),
    n: 1, size: '1024x1536', quality: 'high', output_format: 'png'};
}

export default async function handler(req, res) {
  if (req.method !== 'POST') {
    res.setHeader('Allow', 'POST');
    return reply(res, 405, {error: 'method_not_allowed'});
  }
  if (!process.env.OPENAI_API_KEY) return reply(res, 503, {error: 'unavailable'});
  if (!String(req.headers['content-type'] ?? '').toLowerCase().startsWith('application/json'))
    return reply(res, 415, {error: 'json_required'});
  let request;
  try { request = imageRequest(await readJson(req)); }
  catch { return reply(res, 400, {error: 'invalid_request'}); }
  try {
    const upstream = await fetch('https://api.openai.com/v1/images/generations', {
      method: 'POST', headers: {'Content-Type': 'application/json',
        Authorization: `Bearer ${process.env.OPENAI_API_KEY}`},
      body: JSON.stringify(request), signal: AbortSignal.timeout(285_000), redirect: 'error'
    });
    if (!upstream.ok) return reply(res, upstream.status === 429 ? 429 : 502, {error: 'upstream_failed'});
    const result = await upstream.json();
    const encoded = result.data?.[0]?.b64_json;
    if (typeof encoded !== 'string' || !/^[A-Za-z0-9+/]+={0,2}$/.test(encoded)) throw Error('invalid_image');
    const data = Buffer.from(encoded, 'base64');
    if (!data.subarray(0, 8).equals(Buffer.from([137,80,78,71,13,10,26,10]))) throw Error('invalid_image');
    res.setHeader('Content-Type', 'image/png');
    res.setHeader('Cache-Control', 'no-store');
    res.setHeader('X-Content-Type-Options', 'nosniff');
    res.statusCode = 200;
    return res.end(data);
  } catch {
    return reply(res, 502, {error: 'request_failed'});
  }
}

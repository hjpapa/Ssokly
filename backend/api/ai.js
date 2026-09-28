import { readFileSync } from 'node:fs';
import { readJson, reply } from '../lib/http.js';
import { temporaryImages, verifyReceipt } from '../lib/temp-images.js';

const prompts = JSON.parse(readFileSync(new URL('../prompts.json', import.meta.url), 'utf8'));

export function buildRequest(body) {
  if (!body || typeof body !== 'object' || Array.isArray(body)) throw Error('invalid');
  const common = { model: 'gpt-5-nano', store: false,
    text: { verbosity: 'low' } };
  if (body.operation === 'ocr') {
    if (Object.keys(body).some(k => !['operation', 'image', 'detail'].includes(k))) throw Error('invalid');
    if (typeof body.image !== 'string'
        || !/^data:image\/png;base64,[A-Za-z0-9+/]+={0,2}$/.test(body.image)) throw Error('invalid');
    if (!Buffer.from(body.image.split(',')[1], 'base64').subarray(0, 8)
      .equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]))) throw Error('invalid');
    const detail = body.detail ?? 'high';
    if (!['low', 'auto', 'high'].includes(detail)) throw Error('invalid');
    return { ...common, instructions: prompts.ocr, reasoning: { effort: 'minimal' },
      input: [{ role: 'user', content: [
        { type: 'input_text', text: '이 이미지를 고정밀 OCR로 전사해 주세요.' },
        { type: 'input_image', image_url: body.image, detail }
      ] }] };
  }
  if (body.operation === 'text') {
    if (Object.keys(body).some(k => !['operation', 'text', 'mode', 'audience'].includes(k))) throw Error('invalid');
    if (typeof body.text !== 'string' || !body.text.trim()) throw Error('invalid');
    const instruction = prompts.actions[body.mode]?.[body.audience];
    if (typeof instruction !== 'string') throw Error('invalid');
    return { ...common, instructions: instruction, reasoning: { effort: 'low' },
      input: [{ role: 'user', content: [{ type: 'input_text', text: body.text }] }] };
  }
  throw Error('invalid');
}

export function completedText(response) {
  if (response.status !== 'completed' || response.error || response.incomplete_details) throw Error('incomplete');
  let text = '';
  for (const item of response.output ?? []) {
    if (item.type !== 'message') continue;
    if (item.status !== 'completed') throw Error('incomplete');
    for (const part of item.content ?? []) {
      if (part.type === 'refusal') throw Error('refused');
      if (part.type === 'output_text' && typeof part.text === 'string') text += part.text;
    }
  }
  if (!text.trim()) throw Error('empty');
  return text;
}

export function aiHandler(storage = temporaryImages()) {
return async function handler(req, res) {
  if (req.method !== 'POST') {
    res.setHeader('Allow', 'POST');
    return reply(res, 405, { error: 'method_not_allowed' });
  }
  if (!process.env.OPENAI_API_KEY) {
    return reply(res, 503, { error: 'unavailable' });
  }
  if (!String(req.headers['content-type'] ?? '').toLowerCase().startsWith('application/json')) {
    return reply(res, 415, { error: 'json_required' });
  }
  let request;
  let record;
  let body;
  try {
    body = await readJson(req);
    if (body?.operation === 'ocr_blob') {
      if (Object.keys(body).some(k => !['operation', 'receipt', 'detail'].includes(k))
          || !['low', 'auto', 'high'].includes(body.detail ?? 'high')) throw Error('invalid');
      record = verifyReceipt(body.receipt);
    } else request = buildRequest(body);
  } catch {
    return reply(res, 400, { error: 'invalid_request' });
  }
  let status = 502;
  let result = { error: 'request_failed' };
  try {
    if (record) request = buildRequest({ operation: 'ocr', image: await storage.read(record), detail: body.detail });
    const started = performance.now();
    const upstream = await fetch('https://api.openai.com/v1/responses', {
      method: 'POST', headers: { 'Content-Type': 'application/json',
        Authorization: `Bearer ${process.env.OPENAI_API_KEY}` },
      body: JSON.stringify(request), signal: AbortSignal.timeout(285_000), redirect: 'error'
    });
    if (!upstream.ok) {
      status = upstream.status === 429 ? 429 : 502;
      result = { error: 'upstream_failed' };
    } else {
      const text = completedText(await upstream.json());
      const upstreamMs = Math.round(performance.now() - started);
      res.setHeader('Server-Timing', `processing;dur=${upstreamMs}`);
      status = 200;
      result = { status: 'completed', text, upstream_ms: upstreamMs };
    }
  } catch {
    // Never return/log upstream bodies, document content, headers, or secrets.
  } finally {
    if (record) {
      try { await storage.remove(record); }
      catch { result.cleanup_pending = true; }
    }
  }
  return reply(res, status, result);
};
}
export default aiHandler();

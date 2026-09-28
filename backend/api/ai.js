import { readFileSync } from 'node:fs';

const prompts = JSON.parse(readFileSync(new URL('../prompts.json', import.meta.url), 'utf8'));
const MAX_BYTES = 4_000_000;

function reply(res, status, body) {
  res.setHeader('Content-Type', 'application/json; charset=utf-8');
  res.setHeader('Cache-Control', 'no-store');
  res.setHeader('X-Content-Type-Options', 'nosniff');
  res.statusCode = status;
  res.end(JSON.stringify(body));
}

export function buildRequest(body) {
  if (!body || typeof body !== 'object' || Array.isArray(body)) throw Error('invalid');
  const common = { model: 'gpt-5-nano', store: false, max_output_tokens: 4000,
    text: { verbosity: 'low' } };
  if (body.operation === 'ocr') {
    if (Object.keys(body).some(k => !['operation', 'image', 'detail'].includes(k))) throw Error('invalid');
    if (typeof body.image !== 'string' || body.image.length > 3_950_000
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
    if (typeof body.text !== 'string' || !body.text.trim() || body.text.length > 100_000) throw Error('invalid');
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

export default async function handler(req, res) {
  if (req.method !== 'POST') {
    res.setHeader('Allow', 'POST');
    return reply(res, 405, { error: 'method_not_allowed' });
  }
  if (process.env.SSOKLY_RELAY_ENABLED !== 'true' || !process.env.OPENAI_API_KEY) {
    return reply(res, 503, { error: 'unavailable' });
  }
  if (!String(req.headers['content-type'] ?? '').toLowerCase().startsWith('application/json')) {
    return reply(res, 415, { error: 'json_required' });
  }
  let request;
  try {
    if (Number(req.headers['content-length']) > MAX_BYTES) return reply(res, 413, { error: 'too_large' });
    let body = req.body;
    if (body === undefined) {
      let size = 0;
      const chunks = [];
      for await (const chunk of req) {
        size += Buffer.byteLength(chunk);
        if (size > MAX_BYTES) return reply(res, 413, { error: 'too_large' });
        chunks.push(Buffer.from(chunk));
      }
      body = Buffer.concat(chunks).toString('utf8');
    }
    if (Buffer.isBuffer(body)) body = body.toString('utf8');
    if (Buffer.byteLength(typeof body === 'string' ? body : JSON.stringify(body)) > MAX_BYTES) {
      return reply(res, 413, { error: 'too_large' });
    }
    request = buildRequest(typeof body === 'string' ? JSON.parse(body) : body);
  } catch {
    return reply(res, 400, { error: 'invalid_request' });
  }
  const started = performance.now();
  try {
    const upstream = await fetch('https://api.openai.com/v1/responses', {
      method: 'POST', headers: { 'Content-Type': 'application/json',
        Authorization: `Bearer ${process.env.OPENAI_API_KEY}` },
      body: JSON.stringify(request), signal: AbortSignal.timeout(105_000), redirect: 'error'
    });
    if (!upstream.ok) return reply(res, upstream.status === 429 ? 429 : 502, { error: 'upstream_failed' });
    const text = completedText(await upstream.json());
    const upstreamMs = Math.round(performance.now() - started);
    res.setHeader('Server-Timing', `openai;dur=${upstreamMs}`);
    return reply(res, 200, { status: 'completed', text, upstream_ms: upstreamMs });
  } catch {
    // Never return/log upstream bodies, document content, headers, or secrets.
    return reply(res, 502, { error: 'request_failed' });
  }
}

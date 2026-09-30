import test from 'node:test';
import assert from 'node:assert/strict';
import handler, {imageRequest} from '../api/image.js';

test('image fixed model and exact text; no options injection or content caps', () => {
  const text = '희망자만 10월 2일 오후 3시까지';
  const request = imageRequest({text});
  assert.equal(request.model, 'gpt-image-2.5-flare');
  assert.ok(request.prompt.endsWith(JSON.stringify(text)));
  for (const body of [{text, model:'other'}, {text, n:2}, {text:''}, [], null, Object.create({text})])
    assert.throws(() => imageRequest(body));
  assert.ok(imageRequest({text: '가'.repeat(100000)}));
});

test('binary response and sanitized failure without retries', async () => {
  const originalFetch = global.fetch;
  const originalKey = process.env.OPENAI_API_KEY;
  process.env.OPENAI_API_KEY = 'synthetic';
  let calls = 0;
  const png = Buffer.from([137,80,78,71,13,10,26,10,1]);
  const res = {headers:{}, setHeader(k,v){this.headers[k]=v;}, end(data){this.data=data;}};
  const req = {method:'POST', headers:{'content-type':'application/json'}, body:{text:'합성'}};
  try {
    global.fetch = async (url, options) => {
      calls++;
      assert.equal(url, 'https://api.openai.com/v1/images/generations');
      assert.equal(options.redirect, 'error');
      return {ok:true, json:async()=>({data:[{b64_json:png.toString('base64')}]})};
    };
    await handler(req,res);
    assert.equal(res.statusCode,200);
    assert.deepEqual(res.data,png);
    assert.equal(res.headers['Cache-Control'],'no-store');
    global.fetch = async()=>{calls++;throw Error('private upstream text');};
    await handler(req,res);
    assert.equal(res.statusCode,502);
    assert.equal(calls,2);
    assert.ok(!res.data.includes('private'));
  } finally {
    global.fetch = originalFetch;
    if(originalKey === undefined) delete process.env.OPENAI_API_KEY;
    else process.env.OPENAI_API_KEY = originalKey;
  }
});

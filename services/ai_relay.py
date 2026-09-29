"""Keyless desktop transport. Never forwards local credentials or retries a billable request."""
import json
import base64
import hashlib
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

import httpx

CONFIG_PATH = (Path(sys.executable).parent if getattr(sys, 'frozen', False)
               else Path(__file__).resolve().parents[1]) / 'ai-server.json'
INLINE_REQUEST_BYTES = 4_000_000  # Route threshold, not a rejection limit.


class RelayError(RuntimeError):
    pass


def server_url():
    value = os.getenv('SSOKLY_API_URL')
    if value is None:
        try:
            value = json.loads(CONFIG_PATH.read_text(encoding='utf-8')).get('url', '')
        except FileNotFoundError:
            value = ''
        except (ValueError, AttributeError, OSError):
            raise RelayError('AI 서버 설정 파일을 확인해 주세요.') from None
    if not isinstance(value, str):
        raise RelayError('AI 서버 주소는 문자열이어야 합니다.')
    value = value.strip()
    if not value:
        return ''
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise RelayError('AI 서버 주소는 인증 정보 없는 HTTPS 주소여야 합니다.')
    return value.rstrip('/')


def server_token():
    """Optional shared app token from SSOKLY_API_TOKEN or ai-server.json ("token")."""
    value = os.getenv('SSOKLY_API_TOKEN')
    if value is None:
        try:
            value = json.loads(CONFIG_PATH.read_text(encoding='utf-8')).get('token', '')
        except (OSError, ValueError, AttributeError):
            value = ''
    return value.strip() if isinstance(value, str) else ''


def _headers(json_body=False):
    headers = {'Content-Type': 'application/json'} if json_body else {}
    token = server_token()
    if token:
        headers['X-Ssokly-Token'] = token
    return headers


def request_relay(payload):
    url = server_url()
    if not url:
        raise RelayError('AI 서버 주소를 설정해 주세요.')
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    try:
        with httpx.Client(timeout=300, follow_redirects=False) as client:
            if payload.get('operation') == 'ocr' and len(body) > INLINE_REQUEST_BYTES:
                response = _large_image_request(client, url, payload)
            else:
                response = client.post(url + '/api/ai', content=body,
                                       headers=_headers(True))
        if response.status_code == 413:
            raise RelayError('Vercel의 요청·응답 크기 한도(4.5MB)를 초과했습니다. 필요한 영역을 나누어 캡처해 주세요.')
        if response.status_code == 401:
            raise RelayError('AI 서버 접근 토큰이 없거나 올바르지 않습니다. ai-server.json의 token을 확인해 주세요.')
        if response.status_code == 503:
            raise RelayError('AI 서버가 아직 설정되지 않았거나 사용할 수 없습니다.')
        if response.status_code == 429:
            raise RelayError('AI 서버가 혼잡하거나 사용 한도에 도달했습니다. 잠시 후 다시 시도해 주세요.')
        if response.status_code != 200:
            raise RelayError('AI 서버 요청을 완료하지 못했습니다. 서버 연결·설정을 확인해 주세요.')
        result = response.json()
        if (result.get('status') != 'completed' or not isinstance(result.get('text'), str)
                or not result['text'].strip()):
            raise RelayError('완성된 AI 응답이 없어 기존 결과를 유지했습니다.')
        return result['text'].strip()
    except RelayError:
        raise
    except Exception:
        raise RelayError('AI 서버에 연결하지 못했습니다. 네트워크와 서버 주소를 확인해 주세요.') from None


def _large_image_request(client, url, payload):
    image = payload.get('image', '')
    if not isinstance(image, str) or not image.startswith('data:image/png;base64,'):
        raise RelayError('전송할 이미지 형식을 확인해 주세요.')
    data = base64.b64decode(image.split(',', 1)[1], validate=True)
    receipt = None
    try:
        issued = client.post(url + '/api/upload', json={'action': 'create', 'bytes': len(data),
                            'sha256': hashlib.sha256(data).hexdigest()},
                            headers=_headers())
        if issued.status_code == 401:
            raise RelayError('AI 서버 접근 토큰이 없거나 올바르지 않습니다. ai-server.json의 token을 확인해 주세요.')
        if issued.status_code != 200:
            raise RelayError('큰 이미지 업로드를 준비하지 못했습니다. 서버 저장소 연결을 확인해 주세요.')
        grant = issued.json()
        receipt = grant['receipt']
        upload_url = grant['upload_url']
        target = urlsplit(upload_url)
        if (target.scheme != 'https' or target.netloc != 'vercel.com' or target.path != '/api/blob/'
                or target.username or target.password or target.fragment
                or not isinstance(receipt, str)):
            raise RelayError('이미지 업로드 주소를 확인하지 못했습니다.')
        uploaded = client.put(upload_url, content=data, headers={'Content-Type': 'image/png'})
        if uploaded.status_code not in (200, 201):
            raise RelayError('이미지를 업로드하지 못했습니다. 연결 상태를 확인하고 다시 시도해 주세요.')
        return client.post(url + '/api/ai', json={'operation': 'ocr_blob', 'receipt': receipt,
                           'detail': payload.get('detail', 'high')}, headers=_headers())
    finally:
        if receipt:
            try:
                # Server also deletes before replying; repeat deletion is safe.
                client.post(url + '/api/upload', json={'action': 'delete', 'receipt': receipt},
                            headers=_headers(), timeout=15)
            except Exception:
                pass  # Daily server cleanup covers disconnects and abandoned uploads.

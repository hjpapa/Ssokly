"""Keyless desktop transport. Never forwards local credentials or retries a billable request."""
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

import httpx

CONFIG_PATH = (Path(sys.executable).parent if getattr(sys, 'frozen', False)
               else Path(__file__).resolve().parents[1]) / 'ai-server.json'
MAX_REQUEST_BYTES = 4_000_000


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


def request_relay(payload):
    url = server_url()
    if not url:
        raise RelayError('AI 서버 주소를 설정해 주세요.')
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    if len(body) > MAX_REQUEST_BYTES:
        raise RelayError('전송 사본이 너무 큽니다. 필요한 영역을 나누어 캡처해 주세요. (4MB 제한)')
    try:
        with httpx.Client(timeout=120, follow_redirects=False) as client:
            response = client.post(url + '/api/ai', content=body,
                                   headers={'Content-Type': 'application/json'})
        if response.status_code == 413:
            raise RelayError('전송 사본이 너무 큽니다. 필요한 영역을 나누어 캡처해 주세요.')
        if response.status_code == 503:
            raise RelayError('테스트용 AI 서버가 중지되었거나 아직 설정되지 않았습니다.')
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

# Ssokly AI 중계

로그인 없이 POST `/api/ai`를 사용한다. 서버가 OpenAI를 호출하고 완성된 텍스트만 반환한다. 데스크톱에는 서버 주소만 배포하며 API 키를 내려주는 경로는 없다.

## 배포

Node.js 24와 Vercel CLI 사용. 이 `backend` 폴더를 프로젝트 루트로 선택한다. 저장소 전체를 배포하지 않는다.

1. `npx vercel login`
2. `npx vercel link`로 전용 프로젝트 연결
3. Vercel 프로젝트 환경변수 Production에 `OPENAI_API_KEY`를 Secret으로 등록
4. `npx vercel blob create-store ssokly-ocr-temp --access private --region icn1 --environment production --yes`로 전용 비공개 저장소 생성·연결
4-1. (권장) Production의 `SSOKLY_APP_TOKEN`에 무작위 비밀값을 Secret으로 등록하면 `/api/ai`·`/api/upload`가 `X-Ssokly-Token` 헤더 일치를 요구한다. 미설정이면 기존처럼 공개 동작한다. 같은 값을 앱의 `ai-server.json`에 `"token"`으로 넣거나 환경변수 `SSOKLY_API_TOKEN`으로 지정한다. 제출·공유용 `ai-server.json`에는 토큰을 넣지 않는다. 앱에 든 토큰은 배포본에서 추출될 수 있으므로 OpenAI 프로젝트 지출 한도와 함께 사용한다.
5. Production의 `CRON_SECRET`에 충분히 긴 무작위 비밀값을 Secret으로 등록(업로드 확인 서명·정리 작업 인증). 생성값을 코드/로그/제출 파일에 넣지 않음
6. `npx vercel deploy --prod`
7. 앱 루트(패키징된 앱은 EXE 옆)의 `ai-server.json`에 `{"url":"https://실제-서버-주소"}` 설정

키는 명령 인수·코드·응답·로그에 넣지 않는다. `.env`를 업로드하지 않는다. 이 프로젝트는 Git 자동 배포를 연결하지 않고 백엔드 폴더만 CLI로 배포한다.

## 동작과 제한

- OCR/요약/일정·할 일/안내문만 처리. 모델 `gpt-5-nano`, 서버 고정 프롬프트, `store:false`. 앱이 정한 출력 토큰 상한은 없음. 임의 모델·URL·도구 요청 불가.
- 이미지 JSON이 4MB를 초과하면 `/api/upload`에서 단일 경로의 PUT 전용 서명 URL을 발급한다. 앱은 PNG를 Blob으로 직접 업로드하고 `/api/ai`에는 `ocr_blob`과 서명된 확인 정보만 보낸다. Vercel 함수의 4.5MB 요청 본문 한도를 통과하는 대용량 이미지 경로이다. 모델 한도·큰 텍스트/일반 응답 한도는 별개다.
- `@vercel/blob` 2.8 이상, private 접근, 파일명은 서버 생성 UUID, 덮어쓰기 금지, PNG MIME만 허용. 업로드 권한은 10분, 이미지 처리 확인 정보는 20분 유효하다. SHA-256과 바이트 수를 확인해 앱이 승인한 이미지와 일치할 때만 OCR한다. 저장소 관리 키·OpenAI 키·서명용 비밀값은 클라이언트로 보내지 않는다.
- 성공/실패 시 서버가 삭제 후 응답하고 앱에서도 정리 요청. 삭제 장애 시 `cleanup_pending`과 일별 정리로 보완한다. `/api/cleanup`은 CRON_SECRET 인증을 요구하며 `ssokly-ocr/` 내 1시간 지난 정규 파일만 지운다. 매일 UTC 18시(한국 03시), 정상 실행 시 약 25시간 내 잔여 파일 정리. 플랫폼 장애 시 보존이 더 길어질 수 있다. 사용자 원본·로컬 캡처는 지우지 않는다.
- 응답이 미완성·거절·빈 결과면 오류로 처리. 자동 재시도나 로컬 키로의 전환 없음. 서버 모드는 완성 결과를 한 번에 표시하므로 생성 중 부분 미리보기는 없다.
- 서울 `icn1`, 함수 최대 300초, OpenAI 대기 최대 285초. 앱 대기는 300초. 앱 취소는 결과 적용을 막으며 이미 실행 중인 서버 요청의 비용 취소를 보장하지 않는다.
- 서버는 문서 본문과 키를 로그에 기록하지 않는다. 플랫폼의 기본 요청 메타데이터 로그는 별도이다.
- 텍스트 요청은 200,000자, 큰 이미지 업로드 발급은 30MB까지만 허용한다. 프롬프트 조회는 서버 고정 목록의 자체 키만 인정한다.
- 로그인·시험 활성화 조건·이용 횟수·서비스 만료 기한을 두지 않는다. 큰 이미지에는 연결된 비공개 저장소와 CRON_SECRET이 필요하며 SSOKLY_RELAY_ENABLED는 사용하지 않는다. 주소를 아는 사람은 호출할 수 있고 모델·저장소 이용료는 운영자에게 청구된다. 파일 업로드 권한의 짧은 만료는 서비스 이용 기간 제한이 아니다.
- 이전 저장 기록의 문서 파일 전체 API 전송은 서버 모드에서 지원하지 않는다. 파일 열기로 다시 가져온 뒤 필요한 페이지를 OCR한다.

## 검증

`node --test test/*.test.js`는 실제 API를 호출하지 않는다. 프롬프트 변경 후 저장소 루트에서 `python tools/export_relay_prompts.py` 실행.

`python tools/verify_large_relay.py --live`는 약 8MB 합성 PNG로 키 없는 앱 OCR 1회, 비공개 접근 거절, 정리 요청을 확인한다. 함수 요청 크기와 시간만 기록하며 업로드 서명 URL·확인 정보는 기록하지 않는다.

루트에서 `python tools/measure_relay.py --url https://실제-주소 --live`는 합성 이미지 직접 1회·중계 2회, 총 3회의 유료 OCR을 실행한다. 서버 처리 시간과 클라이언트 총시간을 분리하되 플랫폼 cold start 여부는 확정하지 않는다.

`python tools/build_submission.py --url https://실제-주소`는 추적된 파일로 소스 제출용 `dist/Ssokly-submission.zip`을 만든다. `.env`·Git·가상환경·사용자 자료·서버 코드는 제외한다. 새 파일은 먼저 Git에 추가해야 한다. EXE 생성 기능은 아니다.

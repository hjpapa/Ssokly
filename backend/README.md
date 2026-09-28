# Ssokly 테스트용 AI 중계

로그인 없이 POST `/api/ai`를 사용한다. 서버가 OpenAI를 호출하고 완성된 텍스트만 반환한다. 데스크톱에는 서버 주소만 배포하며 API 키를 내려주는 경로는 없다.

## 배포

Node.js 24와 Vercel CLI 사용. 이 `backend` 폴더를 프로젝트 루트로 선택한다. 저장소 전체를 배포하지 않는다.

1. `npx vercel login`
2. `npx vercel link`로 전용 프로젝트 연결
3. Vercel 프로젝트 환경변수 Production에 `OPENAI_API_KEY`를 Secret으로 등록
4. `SSOKLY_RELAY_ENABLED=true` 등록
5. `npx vercel deploy --prod`
6. 앱 루트(패키징된 앱은 EXE 옆)의 `ai-server.json`에 `{"url":"https://실제-서버-주소"}` 설정

키는 명령 인수·코드·응답·로그에 넣지 않는다. `.env`를 업로드하지 않는다. 이 프로젝트는 Git 자동 배포를 연결하지 않고 백엔드 폴더만 CLI로 배포한다.

## 동작과 제한

- OCR/요약/일정·할 일/안내문만 처리. 모델 `gpt-5-nano`, 서버 고정 프롬프트, `store:false`, 최대 출력 4000토큰. 임의 모델·URL·도구 요청 불가.
- 요청 4,000,000바이트 이하. PNG 데이터만 허용하고 외부 이미지 URL은 받지 않는다. 큰 캡처는 필요한 영역을 나눠 전송한다. 텍스트 작업은 10만 자까지.
- 응답이 미완성·거절·빈 결과면 오류로 처리. 자동 재시도나 로컬 키로의 전환 없음. 서버 모드는 완성 결과를 한 번에 표시하므로 생성 중 부분 미리보기는 없다.
- 서울 `icn1`, 함수 최대 120초, OpenAI 대기 최대 105초. 앱 대기는 120초. 앱 취소는 결과 적용을 막으며 이미 실행 중인 서버 요청의 비용 취소를 보장하지 않는다.
- 서버는 문서 본문과 키를 로그에 기록하지 않는다. 플랫폼의 기본 요청 메타데이터 로그는 별도이다.
- 로그인 없는 시험 서버이므로 주소를 아는 사람은 호출할 수 있다. 사용자별 과금/전역 사용량 제한은 구현하지 않았다. 시험 종료 시 환경변수 `SSOKLY_RELAY_ENABLED=false`로 변경하고 재배포하면 새 요청이 중단된다.
- 이전 저장 기록의 문서 파일 전체 API 전송은 서버 모드에서 지원하지 않는다. 파일 열기로 다시 가져온 뒤 필요한 페이지를 OCR한다.

## 검증

`node --test test/ai.test.js`는 실제 API를 호출하지 않는다. 프롬프트 변경 후 저장소 루트에서 `python tools/export_relay_prompts.py` 실행.

루트에서 `python tools/measure_relay.py --url https://실제-주소 --live`는 합성 이미지 직접 1회·중계 2회, 총 3회의 유료 OCR을 실행한다. 서버 처리 시간과 클라이언트 총시간을 분리하되 플랫폼 cold start 여부는 확정하지 않는다.

`python tools/build_submission.py --url https://실제-주소`는 추적된 파일로 소스 제출용 `dist/Ssokly-submission.zip`을 만든다. `.env`·Git·가상환경·사용자 자료·서버 코드는 제외한다. 새 파일은 먼저 Git에 추가해야 한다. EXE 생성 기능은 아니다.

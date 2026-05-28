# Ssokly

만든 사람: docsusil

**Ssokly**는 학교 공문, PDF, 한글 문서, 웹페이지처럼 화면에 보이는 문서 일부를 드래그로 캡처한 뒤, **GPT-5 nano 기반 이미지 OCR**로 글자를 읽고, 교사가 실제로 해야 할 일을 정리해주는 데스크톱 MVP 앱입니다.

부제: **Pull tasks out of documents.**
설명: **드래그한 공문을 해야 할 일로 바꿔요.**

## 핵심 기획

공문 업무에서 중요한 것은 단순히 글자를 많이 읽는 것이 아니라, **잘못 읽지 않는 것**입니다. 그래서 Ssokly는 로컬 Tesseract OCR 대신 OpenAI의 이미지 이해 모델을 OCR의 중심에 둡니다.

```text
화면 영역 드래그 -> 이미지 캡처 -> GPT-5 nano 이미지 OCR -> OCR 텍스트 검수/수정 -> 업무 분석 -> 할 일/기한/대상/인사이트 정리
```

현재 목표는 다음 한 가지 흐름을 정확하게 만드는 것입니다.

```text
공문 영역을 사각형으로 드래그하면 AI가 글자를 읽고, 인식된 텍스트가 앱의 텍스트 칸에 표시된다.
```

그 다음 단계에서 사용자가 OCR 텍스트를 확인하고 수정한 뒤 업무 분석을 실행합니다.

## 주요 기능

- 화면 영역을 사각형으로 드래그해서 캡처
- 캡처 이미지 또는 이미지 파일을 GPT-5 nano로 직접 OCR
- 표, 날짜, 대상, 기관명, 영문 약어를 최대한 구조적으로 전사
- OCR 텍스트 직접 입력 및 수정
- 검수된 텍스트를 바탕으로 공문 업무 분석
- Markdown 형식의 업무 요약, 할 일, 기한, 대상, 제출 방법, 안내문, 체크리스트 생성
- 분석 결과 클립보드 복사

## 새 구조

```text
main.py
ui/app.py
  - Tkinter 앱 화면
  - 캡처/이미지 불러오기/텍스트 검수/업무 분석 버튼

services/capture_service.py
  - 화면 전체 오버레이
  - 사각형 드래그 영역 캡처

services/ocr_service.py
  - PIL 이미지를 PNG data URL로 변환
  - OpenAI Responses API에 이미지 입력
  - GPT-5 nano가 OCR 전사 결과 반환

services/ai_service.py
  - OCR로 얻은 텍스트를 업무 중심으로 분석
```

## 설치 방법

Python 3.10 이상 사용을 권장합니다.

```bash
cd Ssokly
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

`tkinter`는 일반적인 Python Windows 설치에 기본 포함되어 있습니다. 만약 tkinter 관련 오류가 난다면 Python 설치 옵션을 확인해 주세요.

## .env 설정 방법

`.env.example` 파일을 참고해 프로젝트 폴더에 `.env` 파일을 만듭니다.

```env
OPENAI_API_KEY=your_api_key_here
OPENAI_MODEL=gpt-5-nano
OPENAI_OCR_MODEL=gpt-5-nano
OPENAI_ANALYSIS_MODEL=gpt-5-nano
```

- `OPENAI_API_KEY`: OpenAI API 키입니다.
- `OPENAI_MODEL`: OCR/분석 모델을 따로 지정하지 않았을 때 쓰는 기본 모델입니다.
- `OPENAI_OCR_MODEL`: 이미지 OCR에 사용할 모델입니다.
- `OPENAI_ANALYSIS_MODEL`: OCR 후 업무 분석에 사용할 모델입니다.

API 키나 민감정보는 코드에 직접 적지 마세요.

## 실행 방법

가상환경 Python으로 실행하는 것을 권장합니다.

```bash
cd Ssokly
.venv\Scripts\python.exe main.py
```

창만 띄우고 콘솔을 쓰지 않으려면 다음처럼 실행할 수 있습니다.

```bash
.venv\Scripts\pythonw.exe main.py
```

그냥 `python main.py`로 실행하면 시스템 Python이 잡혀 필요한 패키지를 못 찾을 수 있습니다.

## 사용 방법

1. 앱을 실행합니다.
2. `화면 글씨 인식하기` 버튼을 누릅니다.
3. 화면 위 공문 영역을 사각형으로 드래그합니다.
4. OpenAI 이미지 OCR이 글자를 읽고 `AI OCR 추출 텍스트` 칸에 표시합니다.
5. OCR 텍스트를 원문과 대조해 확인하고 필요하면 직접 수정합니다.
6. `업무 분석하기`를 눌러 할 일, 기한, 대상, 제출 방법, 안내문, 체크리스트를 생성합니다.
7. `결과 복사`로 분석 결과를 클립보드에 복사합니다.

## 정확도 원칙

- OCR 결과는 사용자가 원문과 대조해 검수하는 것을 전제로 합니다.
- 공문 분석은 OCR 텍스트가 정확할수록 좋아집니다.
- 표나 작은 글씨는 캡처 영역을 너무 좁게 잡지 말고, 글자가 선명하게 보이는 배율에서 캡처하는 것이 좋습니다.
- AI가 불확실한 글자를 확정적으로 말할 수 있으므로 최종 판단은 반드시 원문 확인이 필요합니다.

## 개인정보 및 보안 주의

Ssokly의 OCR 및 분석 기능을 사용하면 공문 이미지와 텍스트가 OpenAI API로 전송될 수 있습니다.

학생 이름, 연락처, 주민등록번호, 건강 정보, 민감한 내부 정보는 가급적 가리거나 삭제한 뒤 테스트하는 것을 권장합니다. 실제 업무에서 사용할 때는 학교와 기관의 개인정보 처리 지침을 먼저 확인해 주세요.

## 다음 개발 계획

- OCR 결과와 원본 캡처 미리보기 나란히 보기
- OCR 신뢰도 낮은 부분 표시
- 표 OCR 결과를 Markdown 표로 안정화
- 업무 분석 전 개인정보 마스킹 옵션
- 캘린더 등록용 문구 생성
- 교직원 메신저 문구 여러 버전 생성
- 학부모 안내문 변환
- 업무 카드 저장 기능
- 최근 분석 기록 보기
- HWP/PDF 파일 직접 업로드 분석
- 사용자 역할별 분석: 담임, 정보부장, 학년부장, 연구부장
- PyInstaller를 이용한 exe 패키징

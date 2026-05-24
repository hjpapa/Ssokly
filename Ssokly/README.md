# Ssokly

만든 사람: docsusil

**Ssokly**는 학교 공문, PDF, 한글 문서, 웹페이지 등 화면에 보이는 문서 일부를 드래그로 캡처한 뒤 OCR로 텍스트를 추출하고, AI가 교사가 실제로 해야 할 일을 정리해주는 데스크톱 MVP 앱입니다.

부제: **Pull tasks out of documents.**  
설명: **드래그한 공문을 해야 할 일로 바꿔요.**

## 핵심 개념

Ssokly는 단순 OCR 앱이 아닙니다.

핵심 흐름은 다음과 같습니다.

```text
문서 이미지 -> 텍스트 추출 -> 업무 의미 분석 -> 해야 할 일/기한/대상/제출 방법/안내문/체크리스트 생성
```

즉, OCR 결과를 보여주는 데서 끝나지 않고, 교사가 공문을 읽고 판단해야 하는 업무 정보를 빠르게 뽑아내는 **업무 의미 추출 앱**입니다.

## 주요 기능

- 화면 영역을 드래그해서 캡처
- 캡처 이미지 또는 이미지 파일에서 OCR 텍스트 추출
- OCR 텍스트 직접 입력 및 수정
- OpenAI API를 이용한 공문 업무 분석
- Markdown 형식의 업무 요약, 기한, 대상, 제출 방법, 안내문, 체크리스트 생성
- 분석 결과 클립보드 복사

## 설치 방법

Python 3.10 이상 사용을 권장합니다.

처음 설치한다면 프로젝트 전용 가상환경을 만드는 것을 권장합니다.

```bash
cd Ssokly
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

가상환경을 쓰지 않고 바로 설치할 수도 있습니다.

```bash
cd Ssokly
pip install -r requirements.txt
```

`tkinter`는 일반적인 Python Windows 설치에 기본 포함되어 있습니다. 만약 tkinter 관련 오류가 난다면 Python 설치 옵션을 확인해 주세요.

## Tesseract OCR 설치 안내

OCR 기능을 사용하려면 Windows에 **Tesseract OCR**이 설치되어 있어야 합니다.

1. Windows용 Tesseract OCR 설치 파일을 내려받아 설치합니다.
2. 설치 시 추가 언어 데이터에서 Korean 또는 Korean script 관련 항목이 있으면 함께 선택합니다.
3. 설치 후 `C:\Program Files\Tesseract-OCR\tesseract.exe` 같은 실행 파일 경로가 있는지 확인합니다.
4. 한국어 OCR에서 오류가 나면 `kor.traineddata` 파일이 `tessdata` 폴더에 있는지 확인합니다.
5. 설치 후에도 인식이 안 되면 Tesseract 실행 파일 경로가 Windows PATH에 등록되어 있는지 확인합니다.

한국어 공문을 분석하려면 한국어 언어팩 설치가 필요할 수 있습니다.

Windows에서 PATH 설정이 어렵다면 다음 경로가 있는지 먼저 확인해 보세요.

```text
C:\Program Files\Tesseract-OCR\tesseract.exe
C:\Program Files\Tesseract-OCR\tessdata\kor.traineddata
```

## .env 설정 방법

`.env.example` 파일을 참고해 프로젝트 폴더에 `.env` 파일을 직접 만듭니다.

```env
OPENAI_API_KEY=your_api_key_here
OPENAI_MODEL=gpt-5-nano
TESSERACT_CMD=C:\Program Files\Tesseract-OCR\tesseract.exe
TESSDATA_DIR=tessdata
```

- `OPENAI_API_KEY`: OpenAI API 키를 입력합니다.
- `OPENAI_MODEL`: 사용할 모델명입니다. 비워두면 기본값으로 `gpt-5-nano`를 사용합니다.
- `TESSERACT_CMD`: 선택 사항입니다. Tesseract가 PATH에 잡히지 않을 때 `tesseract.exe` 전체 경로를 입력합니다.
- `TESSDATA_DIR`: 선택 사항입니다. 한국어 언어팩을 앱 폴더 안의 `tessdata`에 둘 때 사용합니다.

API 키나 민감정보는 코드에 직접 적지 마세요.

`OPENAI_API_KEY`가 없으면 앱은 종료되지 않고 설정 안내 메시지를 보여줍니다. OCR 기능과 텍스트 입력 기능은 API 키 없이도 확인할 수 있습니다.

## 실행 방법

```bash
cd Ssokly
python main.py
```

상위 폴더에서 실행할 수도 있습니다.

```bash
python Ssokly/main.py
```

`ModuleNotFoundError`가 나오면 필요한 Python 패키지가 아직 설치되지 않은 상태입니다. `pip install -r requirements.txt`를 먼저 실행해 주세요.

## Windows에서 pip 또는 py가 인식되지 않을 때

PowerShell에서 `pip` 또는 `py`가 인식되지 않는다면 Python이 설치되어 있지 않거나 PATH에 등록되지 않은 상태일 수 있습니다.

1. Python 공식 사이트에서 Windows용 Python 3.10 이상을 설치합니다.
2. 설치 첫 화면에서 **Add python.exe to PATH** 옵션을 반드시 체크합니다.
3. 설치 옵션에서 `pip`가 포함되어 있는지 확인합니다.
4. 설치가 끝나면 PowerShell을 완전히 닫았다가 다시 엽니다.
5. 아래 명령으로 Python과 pip가 인식되는지 확인합니다.

```bash
python --version
python -m pip --version
```

정상적으로 버전이 보이면 다시 설치 명령을 실행합니다.

```bash
cd Ssokly
python -m pip install -r requirements.txt
python main.py
```

`python`을 입력했을 때 Microsoft Store가 열리거나 아무 일도 일어나지 않으면 Windows 설정의 **앱 실행 별칭**에서 `python.exe`, `python3.exe` 별칭을 끄고 다시 시도해 보세요.

## 사용 방법

1. 앱을 실행합니다.
2. `영역 드래그 캡처하기` 버튼을 눌러 화면 위 문서 일부를 드래그합니다.
3. 또는 `이미지 파일 불러오기`로 png, jpg, jpeg 파일을 선택합니다.
4. OCR 추출 텍스트 영역에서 내용을 확인하고 필요하면 직접 수정합니다.
5. `업무 분석하기`를 누릅니다.
6. 분석 결과를 확인한 뒤 `결과 복사`로 클립보드에 복사합니다.

## 현재 한계

- OCR 정확도는 원본 이미지 품질, 해상도, 글꼴, 배경 상태에 영향을 받습니다.
- AI 분석 결과만 믿지 말고 실제 공문 원문을 반드시 확인해야 합니다.
- 개인정보와 민감정보가 포함된 공문은 주의해서 사용해야 합니다.
- OpenAI API 사용량에 따라 비용이 발생할 수 있습니다.
- 현재는 결과를 파일로 저장하지 않고 화면 출력과 클립보드 복사만 지원합니다.

## 개인정보 및 보안 주의

Ssokly의 AI 분석 기능을 사용하면 공문 내용이 외부 API로 전송될 수 있습니다.

학생 이름, 연락처, 주민등록번호, 건강 정보, 민감한 내부 정보는 가급적 가리거나 삭제한 뒤 테스트하는 것을 권장합니다. 실제 업무에서 사용할 때는 학교와 기관의 개인정보 처리 지침을 먼저 확인해 주세요.

## 다음 개발 계획

- 캘린더 등록용 문구 생성
- 교직원 메신저 문구 여러 버전 생성
- 학부모 안내문 변환
- 업무 카드 저장 기능
- 최근 분석 기록 보기
- HWP/PDF 파일 직접 업로드 분석
- 사용자 역할별 분석: 담임, 정보부장, 학년부장, 연구부장
- PyInstaller를 이용한 exe 패키징

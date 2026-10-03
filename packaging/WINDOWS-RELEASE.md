# Windows 서명 배포 안내

2026-10-03 기준. 인증서가 아직 없어 실제 서명과 외부 배포는 수행하지 않았다.

## 경고와 인증서 준비

기존 배포본은 미서명이다. 실제 경고 화면을 확인하지 않았으므로 차단 원인을 단정하지 않는다. “Windows의 PC 보호”는 SmartScreen, “게시자 알 수 없음”은 서명, Windows 보안 보호 기록의 탐지명은 Defender를 먼저 확인한다. 학교 관리 PC의 정책 차단은 기관 담당자가 확인해야 한다.

서명 후에도 새 파일의 SmartScreen 평판이 쌓이기 전에는 경고가 남을 수 있다. EV 인증서도 즉시 우회를 보장하지 않으며 자체 서명은 일반 배포 신뢰를 해결하지 못한다. [Microsoft 공식 안내](https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/smartscreen-reputation).

1. 개인/조직 중 배포 주체와 인증서에 표시할 법적 게시자 이름을 정한다. 앱 제작자 표시 `docsusil`이 자동으로 인증서 이름이 되지는 않는다.
2. 한국 개인 개발자는 공인 인증기관에 **개인 명의 Windows Authenticode 코드 서명 인증서**의 한국 지원, 신원 확인 서류, USB 토큰/클라우드 키 방식과 비용을 확인한다. 웹사이트 SSL 인증서는 사용할 수 없다. 구매 전 실제 상품의 개인 발급 가능 여부를 확인한다.
3. 조직 명의라면 Microsoft Artifact Signing도 검토한다. 현재 한국 조직의 Public Trust는 지원하지만 개인 개발자는 미국·캐나다로 제한된다. Azure 서명 계정 → 신원 확인 → 인증서 프로필 순서다. [공식 준비 안내](https://learn.microsoft.com/en-us/azure/artifact-signing/quickstart).
4. 현재 자동 도구는 **CurrentUser/My 인증서 저장소 + SignTool** 방식이다. USB 토큰은 공급자 드라이버와 개인 키 접근이 필요하다. 클라우드 서명 서비스는 별도 인증·플러그인 연결이 필요하며 현재 자동 도구의 지원 범위에 포함하지 않는다.
5. Windows SDK의 x64 `signtool.exe`, Inno Setup의 `ISCC.exe`, `requirements-build.txt` 의존성을 준비한다. 인증서 비밀번호나 개인 키를 코드·대화에 넣지 않는다.

```powershell
Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert |
  Select-Object Subject, Thumbprint, NotAfter, HasPrivateKey
```

## 생성과 검증

프로젝트 루트에서 실제 도구 경로·인증서 지문으로 실행한다. 출력 폴더는 존재하지 않는 새 경로여야 한다.

```powershell
.\.venv\Scripts\python.exe tools/release_windows.py `
  --version 0.1.0 --output .local-results/signed-release-0.1.0 `
  --signtool 'C:\SDK\signtool.exe' `
  --iscc 'C:\Inno Setup 6\ISCC.exe' `
  --thumbprint '발급된 인증서의 40자리 지문'
```

`--plan --output <새 폴더>`는 단계만 표시한다. 실제 순서:

1. 인증서·개인 키·유효 기간·코드 서명 용도 확인.
2. PyInstaller 폴더형 빌드와 비밀 파일 포함 검사.
3. EXE SHA256 서명 + RFC3161 SHA256 타임스탬프.
4. `signtool verify /pa /all /v` 및 임시 자료 EXE 자가검사.
5. Inno Setup으로 서명된 EXE 포함. 제거 프로그램과 Setup을 각각 서명.
6. Setup 서명 검증 → 폴더형 ZIP → SHA256 `release-manifest.json` 생성.

실패 시 즉시 중단하고 성공 manifest를 만들지 않는다. 실패 폴더는 배포하지 않는다. ZIP은 내부 EXE가 서명되며 ZIP 자체는 Authenticode 대상이 아니다. 의존 라이브러리의 공급자 서명은 변경하지 않는다. 서명 후 파일을 수정하면 다시 서명해야 한다.

[Inno Setup SignTool](https://jrsoftware.org/ishelp/topic_setup_signtool.htm), [SignedUninstaller](https://jrsoftware.org/ishelp/topic_setup_signeduninstaller.htm), [Microsoft SignTool](https://learn.microsoft.com/en-us/windows/win32/seccrypto/signtool)을 따른다.

## 배포 전 확인

자동 도구는 외부 게시를 하지 않는다. 별도 Windows 10/11 PC에서 실제 다운로드 → 게시자/디지털 서명 확인 → 설치 → 실행 → 제거 → 사용자 데이터 보존을 확인한다. 제거 프로그램의 서명도 확인한다. 학교 관리 PC의 정책은 별도 확인한다. Setup 또는 portable ZIP과 SHA256을 신뢰할 수 있는 HTTPS 배포 위치에 게시한다.

SmartScreen 경고, Defender 탐지, 기관 정책 차단은 구분한다. 실제 Defender 오탐은 탐지명과 파일을 확인해 Microsoft 분석 제출 절차를 진행한다. 보안 기능을 끄는 방식으로 배포하지 않는다.

"""Offline, selectable Korean help for the capture desktop."""
import tkinter as tk
from tkinter import font as tkfont, ttk


# Kept with the application so both installed and portable builds have the same
# help without relying on a working directory, browser, network, or README file.
MANUAL_SECTIONS = (
    ('처음 시작하기', (
        'Ssokly는 캡처·파일의 원본과 텍스트를 보관하고, 필요한 내용을 복사하거나 AI로 정리하는 프로그램입니다.',
        '① 캡처 후 처리 방식을 고릅니다. 처음에는 「보관만」입니다.\n'
        '② 「새 캡처」를 누르고 화면에서 필요한 영역을 드래그합니다. 같은 문서의 다음 장은 「+ 페이지」로 추가합니다.\n'
        '③ 「다시 읽기」로 글자를 인식한 뒤 원본과 텍스트를 대조해 수정합니다.\n'
        '④ 「선택 복사 / 페이지 복사 / 전체 복사」로 사용하거나 「업무 실행」에서 정리합니다.\n'
        '⑤ 제목과 라벨·메모로 분류하고 「보관함」에서 다시 찾습니다.',
        '제목·라벨·메모·텍스트·AI 결과는 편집 후 자동 저장됩니다. 하단 저장 상태를 확인하고, 바로 저장하려면 Ctrl+S를 누르세요. 이 사용 설명서는 보관함 아래의 (?) 버튼 또는 F1로 열 수 있습니다.',
    )),
    ('캡처와 글자 읽기', (
        '「보관만」은 캡처 이미지를 PC에 저장합니다. 자동으로 AI에 전송하지 않습니다. 나중에 「다시 읽기」로 OCR(이미지 속 글자 읽기)을 실행할 수 있습니다.',
        '「자동 인식」은 처음 켤 때 새 캡처의 외부 전송과 비용 발생에 동의해야 합니다. 켜진 뒤에는 새 캡처를 보관하고 OCR을 요청합니다. 「가리고 읽기」는 가린 사본을 준비한 뒤 인식합니다.',
        '캡처 영역은 마우스로 드래그해 고르고 Esc로 취소합니다. 이미지는 OCR보다 먼저 보관되므로 인식 실패나 처리 취소에도 보관된 원본은 유지됩니다.',
        '페이지 선택칸으로 장을 바꾸고 ↑/↓ 버튼으로 순서를 조절합니다. 원본은 확대·축소·100%·맞춤으로 확인하세요. 좁은 창에서는 이미지 우클릭 메뉴에 확대 도구가 있습니다.',
        '「다시 읽기」는 최근 인식본을 갱신하지만 직접 수정한 텍스트는 덮어쓰지 않습니다. 「더보기 → 원래 인식 내용 보기」에서 처음·최근 인식본을 확인하고 필요한 부분을 편집본에 복사하세요.',
    )),
    ('파일 열기', (
        '상단 「파일 열기」에서 이미지, 텍스트, PDF, HWPX 및 지원하는 Office 문서를 가져옵니다. 최대 파일 크기는 50MB, 문서당 최대 100개 쪽/항목입니다.',
        '이미지: PNG, JPG/JPEG, BMP, WEBP.\n'
        '텍스트: TXT, MD, CSV, TSV, JSON, XML, HTML. 마크업은 코드 그대로 표시합니다.\n'
        '문서: PDF, HWPX, DOCX, RTF, ODT.\n'
        '발표: PPTX, ODP.\n'
        '시트: XLSX, ODS.',
        'PDF·HWPX·Office 파일 열기 자체는 로컬 처리입니다. PDF는 쪽 이미지와 내장 텍스트를, HWPX는 본문과 참조된 삽입 이미지를 가져옵니다. 스캔 PDF나 삽입 이미지는 해당 항목을 고르고 「다시 읽기」로 OCR하세요.',
        '구형 HWP/DOC/PPT/XLS는 HWPX/DOCX/PPTX/XLSX 또는 PDF로 변환해 여세요. 원본 배치·도형·차트·그림이 중요하면 PDF로 변환하는 편이 좋습니다. XLSX 수식은 직접 계산하지 않아 저장된 계산값이 없으면 그 상태를 표시합니다.',
        '「더보기 → 원본 문서 파일 열기」는 외부 원본을 기본 프로그램에서 엽니다. 외부 원본 파일 전체를 앱에 복제하지는 않습니다. 원본을 옮겨도 저장된 텍스트와 PDF 쪽 이미지 등은 남지만 외부 원본 열기·텍스트 문서 다시 읽기에는 원래 파일이 필요합니다.',
    )),
    ('텍스트·표 대조와 복사', (
        '「텍스트」 탭의 편집창은 현재 선택한 페이지 하나입니다. 직접 수정하면 자동 저장되며 「전체 복사」와 기본 AI 입력에는 현재 페이지 순서의 수정본을 사용합니다.',
        '「표」 탭은 탭 구분, 공백을 둔 | 구분, Markdown 표를 읽어 행·열로 보여 줍니다. 표 셀은 「텍스트」 탭에서 수정합니다. 긴 셀은 아래 상세 영역에서 확인하세요. 「행 복사」 또는 Ctrl+C는 선택한 행을, 전체 표 복사는 모든 셀을 탭 구분으로 복사합니다.',
        '표는 원본 지면을 그대로 재현하는 화면이 아닙니다. 빈 셀·병합 표시·행 순서를 원본과 대조하세요. HWPX의 ↳ 표시는 병합 셀의 이어지는 칸입니다.',
        '날짜 검토의 청록색 표시는 해석한 날짜, 빨간 밑줄은 불확실한 글자·잘못된 날짜·날짜/요일 충돌 등의 확인 지점입니다. 색상만으로 정확성을 판단하지 말고 원본을 확인하세요.',
        '원본과 작업 영역 사이의 분할선을 드래그해 비율을 바꿀 수 있습니다. 「화면 비율 초기화」로 되돌리고 「보관함」으로 목록을 접거나 펼치세요. 좁은 창에서는 원본과 편집 영역을 위아래로 보여 줍니다.',
    )),
    ('가림과 AI 전송', (
        'OCR·AI 정리·이미지 생성은 선택한 입력을 서버를 통해 OpenAI로 전송하며 비용이 발생합니다. 로컬 보관·편집·검색·복사에는 AI를 호출하지 않습니다. 기관의 외부 AI 이용 기준을 확인한 뒤 필요한 범위만 전송하세요.',
        '이미지를 읽을 때 전송 확인창에서 가릴 영역을 지정하면 불투명하게 가린 이미지 사본을 전송합니다. 원본은 유지됩니다. 개인정보를 자동으로 모두 찾아 가리는 기능은 아닙니다.',
        '「가리고 정리」는 ① 글자를 드래그 → ② 「선택한 텍스트 가리기」 → ③ 「가린 내용으로 정리」 순서입니다. 미리 선택한 글자가 있으면 그 부분을 먼저 가립니다. 같은 이름·번호가 여러 번 나오면 남은 부분도 확인하세요. 원문은 유지하고 전송 사본만 바꿉니다.',
        '이전 가림 설정은 재열기·재인식·AI 정리에도 이어집니다. 전송 범위를 넓힐 때는 확인이 필요합니다. 준비를 취소하거나 실패한 경우 원본으로 자동 대체해 보내지 않습니다.',
        '처리 중에는 경과 시간과 「처리 취소」가 표시됩니다. 취소하면 늦게 도착한 결과를 적용하지 않지만 이미 전송된 요청의 처리·비용까지 취소되지는 않을 수 있습니다. 같은 작업은 기존 전송이 끝난 뒤 다시 실행합니다.',
    )),
    ('업무 실행과 결과 이미지', (
        '「업무 실행」에서 「요약」, 「일정·할 일 정리」, 「안내문」을 고르고 「정리하기」를 누릅니다. 안내문은 교직원 메신저·학부모 메신저·가정통신문 초안을 만듭니다. 일정·할 일은 복사 가능한 결과이며 별도 할 일 목록에 자동 등록하지 않습니다.',
        '기본 입력은 문서 전체의 현재 수정본입니다. 일부만 정리하려면 텍스트 탭에서 글자를 선택하고 「선택한 텍스트만 정리」를 켭니다. 전송 사본을 확인한 뒤 실행하세요.',
        '결과는 원문과 별도로 편집·복사·자동 저장됩니다. 「이전 결과」에서 과거 결과를 볼 수 있습니다. 원문을 바꾼 뒤에는 이전 원문 기반 표시를 확인하고 날짜·대상·조건을 대조하세요. AI가 원문에 없는 내용을 만들거나 일부를 빠뜨릴 수 있습니다.',
        '결과를 수정한 뒤 「이미지로 만들기」를 누르면 현재 결과의 사본으로 새 창을 엽니다. 「이미지 생성」에서 전송 사본을 확인하거나 가린 뒤 생성합니다. 생성과 다시 생성은 각각 별도 AI 요청입니다.',
        '생성 이미지의 한글·날짜·시간·조건을 전송 본문과 대조한 뒤 「PNG 저장」 또는 「이미지 복사」로 사용하세요. 이미지는 문서 DB에 자동 저장되지 않으므로 창을 닫기 전에 저장하세요. 새 결과 텍스트를 사용하려면 이미지 창을 닫고 다시 엽니다.',
    )),
    ('보관함 검색과 분류', (
        '「보관함」의 자료 검색칸에서 제목·본문·인식 원문·라벨·메모의 단어를 찾습니다. 입력하면 목록을 갱신하고, 「검색」 버튼이나 Enter로 즉시 조회합니다. 검색어를 지워도 라벨 필터가 남아 있을 수 있으므로 두 조건을 함께 확인하세요.',
        '최근 자료부터 표시하며 「더 보기」로 다음 자료를 불러옵니다. 목록에서 문서를 선택하면 원본과 텍스트를 다시 엽니다. 좁은 창에서 목록이 넓게 펼쳐져 있으면 문서 선택 후 작업 화면으로 돌아갑니다.',
        '문서 제목 옆 「라벨·메모」를 눌러 분류 입력칸을 펼칩니다. 라벨은 쉼표로 구분하거나 「라벨 선택」에서 추가·해제합니다. 문서당 최대 8개, 라벨 하나당 24자, 메모는 2000자까지 사용할 수 있습니다.',
        '「자료 정리 · 라벨 / 쪽 → 라벨 관리 · 이름 변경」은 휴지통을 포함해 같은 라벨을 사용하는 문서 전체에 반영됩니다. 이미 있는 라벨 이름으로 바꾸면 중복을 합칩니다.',
        '기존 읽기 전용 기록은 「새 문서로 가져오기」로 사본을 만든 뒤 편집합니다. 과거 기록은 「더보기 → 기존 기록 보기」에서 열람·복사할 수 있습니다.',
    )),
    ('쪽 합치기와 분리', (
        '「자료 정리 · 라벨 / 쪽」 또는 「더보기」에서 쪽 합치기·분리를 엽니다.',
        '합치기는 현재 문서와 공통 라벨이 하나 이상 있거나 비어 있지 않은 메모가 같은 문서를 후보로 보여 줍니다. 선택한 문서의 보이는 쪽을 현재 문서 뒤로 옮깁니다. 현재 문서의 제목·라벨·메모는 유지합니다. 같은 캡처가 중복되면 중단합니다.',
        '합친 원래 문서는 휴지통으로 옮겨 메모·과거 AI 결과·삭제한 쪽을 보존합니다. 문서를 복원해도 쪽 이동 자체를 되돌리지는 않습니다. 필요한 쪽은 분리 기능으로 다시 나누세요.',
        '분리는 Ctrl/Shift로 쪽을 고르고 새 문서 제목을 입력합니다. 기존 문서에 한 쪽 이상 남겨야 합니다. 새 문서는 라벨·메모와 선택한 쪽의 원본·인식본·수정본·가림 설정을 이어받습니다. 기존 AI 결과는 원래 문서에 남습니다.',
    )),
    ('휴지통·복원·영구 삭제', (
        '한 페이지만 지우려면 원본 영역의 「이 페이지 삭제」 또는 이미지·페이지 선택칸의 우클릭 메뉴를 사용합니다. 문서 전체는 보관함의 「선택 문서 삭제」로 휴지통에 옮깁니다. 휴지통 자료는 복원한 뒤 편집할 수 있습니다.',
        '페이지 복원: 「휴지통 관리 → 페이지 휴지통 열기…」에서 문서와 페이지를 고르고 「선택 페이지 복원」을 누릅니다. 이 메뉴는 삭제된 페이지 목록을 여는 기능입니다.',
        '문서 복원: 「문서 휴지통 보기」에서 문서를 고르고 「선택 문서 복원」을 누릅니다. 문서를 직접 삭제한 경우에는 문서를 먼저 복원하세요.',
        '마지막 페이지를 삭제하면 문서도 휴지통으로 이동합니다. 이렇게 자동 이동한 문서를 복원하면 마지막으로 삭제한 페이지도 돌아옵니다. 그 전에 따로 삭제한 페이지는 휴지통에 남습니다.',
        '영구 삭제: 페이지 휴지통의 「선택 페이지 영구 삭제」 또는 문서 휴지통의 「선택 휴지통 문서 영구 삭제」를 사용합니다. 확인창에 표시된 대상을 꼭 확인하세요. 영구 삭제한 자료는 앱에서 복원할 수 없습니다.',
        '「휴지통 비우기」는 검색·라벨 필터와 관계없이 휴지통 문서와 개별 삭제 페이지 전체를 대상으로 합니다. 외부 원본 파일·다른 문서가 공유하는 이미지·이전 백업은 지우지 않습니다.',
    )),
    ('캡처 관리', (
        '「더보기 → 캡처 관리 · 전체 이미지」에서 전체 캡처를 검색하고 선택해 열기·편집·이동·삭제할 수 있습니다. 썸네일을 선택하면 큰 미리보기가 나오고, 더블클릭 또는 「열기」로 해당 페이지로 이동합니다.',
        '이름·문서 제목·본문/OCR·라벨·메모를 한 검색칸으로 찾습니다. 「필터·정렬」을 접어도 적용 조건은 유지됩니다. 「전체 보기」로 초기화하고, 더 많은 자료는 「더 보기」로 불러오세요.',
        '「여러 장 선택」을 켜면 Ctrl 없이 클릭으로 선택·해제할 수 있습니다. 「표시된 항목 전체 선택」은 현재 불러온 항목을 선택합니다. 선택한 캡처들을 기존 문서로 이동하거나 휴지통으로 삭제할 수 있습니다.',
        '이동한 쪽은 대상 문서의 라벨·메모를 따르고 원본·인식본·수정본·가림 설정을 유지합니다. 기존 AI 결과는 원래 문서에 남습니다. 문서 복원만으로 이동을 되돌리지는 않습니다.',
        '「편집 → 이름 변경」은 선택한 캡처의 표시 이름만 바꾸고 원본 파일명은 유지합니다. 「편집 → 라벨·메모」는 그 캡처가 속한 문서 전체에 적용됩니다. 기존 읽기 전용 캡처는 새 문서로 가져온 뒤 편집하세요.',
    )),
    ('저장 위치와 백업', (
        '캡처 폴더와 앱 데이터 폴더는 서로 다릅니다. 신규 사용자의 캡처는 Windows 바탕화면의 ssokly 폴더에 저장합니다. 기존 사용자는 이전 위치를 계속 사용합니다.',
        '「더보기 → 캡처 저장 폴더 설정」에서 현재 위치를 확인하거나 열고 변경합니다. 다른 Ssokly 창을 닫고 빈 폴더를 선택하세요. 캡처 이미지와 관련 DB를 복사·검증한 뒤 앱이 종료되며, 다시 실행하면 새 위치를 사용합니다. 이전 원본은 자동 삭제하지 않습니다.',
        '문서·라벨·메모·AI 결과·설정은 %LOCALAPPDATA%\\Ssokly에 보존합니다. 해당 환경 변수가 없으면 사용자 홈의 .ssokly를 사용합니다. 프로그램을 제거해도 앱 데이터와 캡처 폴더는 남습니다.',
        '다른 PC로 옮기거나 백업하려면 앱을 닫고 캡처 폴더와 앱 데이터 폴더를 모두 보관하세요. 캡처 폴더만 복사하면 문서·라벨·메모까지 이전되지 않습니다. 외부 원본 문서와 따로 저장한 생성 이미지도 필요하면 함께 백업하세요.',
        '업그레이드 전 자동 백업 파일은 최신 자료의 정기 백업을 대신하지 않습니다. 클라우드 자동 동기화는 제공하지 않습니다. 저장 폴더를 옮긴 PC에서는 새 경로 설정도 확인하세요.',
    )),
    ('문제 해결과 단축키', (
        '자료가 안 보일 때: 검색어·라벨 필터·문서 휴지통 보기를 확인합니다. 캡처 관리에서는 「전체 보기」로 필터를 초기화합니다. 저장 폴더 설정의 현재 위치도 확인하세요.',
        '글자를 읽지 못할 때: 원본을 확대해 글자 선명도를 확인하고 해당 페이지에서 「다시 읽기」를 실행합니다. 인터넷·AI 서버 연결 오류라면 원본과 기존 수정본을 보관한 상태로 나중에 다시 시도하세요.',
        '「복구 필요」가 표시될 때: 원본 파일 또는 저장 장치에 문제가 있는 페이지입니다. 저장된 텍스트는 읽기·복사할 수 있습니다. 저장 장치와 캡처 폴더를 확인하고 원본 상태가 해결된 뒤 다시 여세요. 같은 문서의 정상 페이지는 계속 작업할 수 있습니다.',
        '저장 충돌·실패가 표시될 때: 필요한 입력을 「페이지 복사 / 결과 복사」로 먼저 보관합니다. 다른 Ssokly 창과 저장 공간을 확인하세요. 「더보기 → 저장된 최신값 다시 열기…」는 확인 후 미저장 입력을 버리고 저장값을 불러옵니다.',
        '검색 버튼이나 작업 영역이 좁을 때: 창을 넓히거나 보관함을 접고 「화면 비율 초기화」를 사용합니다. 새 버전을 적용하려면 앱을 종료한 뒤 설치파일을 실행하거나 무설치 폴더 전체를 갱신하세요.',
        'Ctrl+S: 현재 편집 내용 즉시 저장.\n'
        'Enter: 보관함 검색칸에서 즉시 검색.\n'
        'Ctrl+C: 선택한 텍스트 복사, 표에서는 선택한 행 복사.\n'
        'Esc: 캡처 영역 선택 취소 / 이 사용 설명서 닫기.\n'
        'F1: 사용 설명서 열기.\n'
        '사용 설명서 안에서 Alt+←/→: 이전/다음 항목, Page Up/Down: 스크롤, Ctrl+Home/End: 처음/끝, Tab: 다음 조작 요소.',
    )),
)


def show_user_manual(parent):
    """Open one modeless help window per desktop and return its controller."""
    owner = parent.winfo_toplevel()
    existing = getattr(owner, '_user_manual_window', None)
    if existing is not None and existing.window.winfo_exists():
        existing.window.deiconify()
        existing.window.lift()
        existing.text.focus_set()
        return existing
    manual = UserManualWindow(owner)
    owner._user_manual_window = manual
    return manual


class UserManualWindow:
    def __init__(self, parent):
        self.parent = parent
        self.window = tk.Toplevel(parent)
        self.window.withdraw()
        self.window.title('Ssokly 사용 설명서')
        self.window.transient(parent)
        self.window.protocol('WM_DELETE_WINDOW', self.close)
        self.window.bind('<Destroy>', self._destroyed, add='+')
        self.window.bind('<Escape>', self.close)
        self.window.bind('<Alt-Left>', lambda _event: self.move_section(-1))
        self.window.bind('<Alt-Right>', lambda _event: self.move_section(1))
        self.font = tkfont.Font(root=self.window, family='Malgun Gothic', size=11)
        self.heading_font = tkfont.Font(root=self.window, family='Malgun Gothic', size=14, weight='bold')

        body = ttk.Frame(self.window, padding=12)
        body.pack(fill='both', expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(2, weight=1)
        ttk.Label(body, text='Ssokly 사용 설명서', font=self.heading_font).grid(row=0, column=0, sticky='w', pady=(0, 8))
        self.section_picker = ttk.Combobox(body, state='readonly', width=1, font=self.font,
                                          values=tuple(f'{i + 1}. {title}' for i, (title, _) in enumerate(MANUAL_SECTIONS)))
        self.section_picker.grid(row=1, column=0, sticky='ew', pady=(0, 10))
        self.section_picker.bind('<<ComboboxSelected>>', self._section_selected)
        content = ttk.Frame(body)
        content.grid(row=2, column=0, sticky='nsew')
        content.columnconfigure(0, weight=1)
        content.rowconfigure(0, weight=1)
        self.text = tk.Text(content, wrap='word', width=1, height=1, font=self.font,
                            background='#ffffff', foreground='#193640', relief='flat',
                            padx=14, pady=12, spacing1=3, spacing3=8, cursor='arrow',
                            selectbackground='#d6efeb', selectforeground='#064e50',
                            takefocus=True, exportselection=False)
        self.text.grid(row=0, column=0, sticky='nsew')
        self.scrollbar = ttk.Scrollbar(content, orient='vertical', command=self.text.yview)
        self.scrollbar.grid(row=0, column=1, sticky='ns')
        self.text.configure(yscrollcommand=self.scrollbar.set)
        self.text.tag_configure('heading', font=self.heading_font, foreground='#087f80', spacing1=14, spacing3=12)
        for index, (title, paragraphs) in enumerate(MANUAL_SECTIONS):
            mark = f'section_{index}'
            self.text.mark_set(mark, 'end-1c')
            self.text.mark_gravity(mark, 'left')
            self.text.insert('end', f'{index + 1}. {title}\n', 'heading')
            for paragraph in paragraphs:
                self.text.insert('end', paragraph + '\n\n')
        self.text.configure(state='disabled')
        self.text.bind('<Prior>', lambda _event: self.scroll(-1))
        self.text.bind('<Next>', lambda _event: self.scroll(1))
        self.text.bind('<Control-Home>', lambda _event: self.scroll_to(0))
        self.text.bind('<Control-End>', lambda _event: self.scroll_to(1))
        # Text's default Tab binding inserts a tab, even in a read-only widget.
        self.text.bind('<Tab>', lambda _event: self._focus_next())
        self.text.bind('<Shift-Tab>', lambda _event: self._focus_previous())

        footer = ttk.Frame(body)
        footer.grid(row=3, column=0, sticky='ew', pady=(10, 0))
        footer.columnconfigure(0, weight=1)
        self.hint = ttk.Label(footer, text='Alt+←/→ 항목 이동 · Esc 닫기', wraplength=300)
        self.hint.grid(row=0, column=0, sticky='w', padx=(0, 8))
        self.close_button = ttk.Button(footer, text='닫기', command=self.close, width=-4)
        self.close_button.grid(row=0, column=1, sticky='e')
        self.window.bind('<Configure>', self._resize_hint, add='+')
        self.window.update_idletasks()

        # Keep text and controls legible at high DPI, but cap the window to the
        # display. The long manual itself always scrolls instead of growing it.
        available_width = max(240, self.window.winfo_screenwidth() - 40)
        available_height = max(240, self.window.winfo_screenheight() - 100)
        width = min(available_width, max(700, self.font.measure('가') * 38 + 70))
        controls_height = body.winfo_reqheight() - content.winfo_reqheight()
        height = min(available_height, max(640, controls_height + self.font.metrics('linespace') * 17))
        minimum_width = min(width, max(420, self.font.measure('가') * 18 + 70))
        minimum_height = min(height, max(320, controls_height + self.font.metrics('linespace') * 5))
        self.window.minsize(minimum_width, minimum_height)
        self.window.geometry(f'{width}x{height}')
        self.section_picker.current(0)
        self.window.deiconify()
        self.text.focus_set()

    def _resize_hint(self, event):
        if event.widget is self.window:
            width = max(80, event.width - self.close_button.winfo_reqwidth() - 44)
            if int(self.hint.cget('wraplength')) != width:
                self.hint.configure(wraplength=width)

    def _section_selected(self, _event=None):
        self.show_section(self.section_picker.current())

    def show_section(self, index):
        index = max(0, min(len(MANUAL_SECTIONS) - 1, index))
        self.section_picker.current(index)
        self.text.yview(f'section_{index}')
        self.text.focus_set()
        return 'break'

    def move_section(self, offset):
        return self.show_section(self.section_picker.current() + offset)

    def scroll(self, direction):
        self.text.yview_scroll(direction, 'pages')
        return 'break'

    def scroll_to(self, fraction):
        self.text.yview_moveto(fraction)
        return 'break'

    def _focus_next(self):
        self.text.tk_focusNext().focus_set()
        return 'break'

    def _focus_previous(self):
        self.text.tk_focusPrev().focus_set()
        return 'break'

    def _destroyed(self, event):
        if event.widget is self.window and getattr(self.parent, '_user_manual_window', None) is self:
            self.parent._user_manual_window = None

    def close(self, _event=None):
        if self.window.winfo_exists():
            self.window.destroy()
        return 'break'

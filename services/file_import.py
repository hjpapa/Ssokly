"""Local PDF/HWPX import. No network calls; prepare all pages before saving."""
from contextlib import closing
from io import BytesIO
from pathlib import Path, PurePosixPath
import tempfile
import threading
import zipfile
from xml.etree import ElementTree

from PIL import Image
from services.document_service import read_hwpx_file, LOCAL_DOCUMENT_EXTENSIONS
from services.office_reader import read_office

MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_PAGES = 100
MAX_IMAGE_SIDE = 2400
_PDF_LOCK = threading.Lock()  # PDFium must not run concurrently across threads.


def prepare_pdf(path, destination):
    import pypdfium2 as pdfium
    entries = []
    try:
        with _PDF_LOCK, pdfium.PdfDocument(path) as pdf:
            if not 0 < len(pdf) <= MAX_PAGES:
                raise ValueError('PDF는 한 번에 1~100쪽을 열 수 있습니다. 파일을 나눠 주세요.')
            pdf.init_forms()
            for index in range(len(pdf)):
                with closing(pdf[index]) as page:
                    with closing(page.get_textpage()) as textpage:
                        text = textpage.get_text_bounded().replace('\r\n', '\n').strip()
                    width, height = page.get_size()
                    if min(width, height) <= 0:
                        raise ValueError('PDF 페이지 크기를 읽을 수 없습니다.')
                    scale = min(2.5, MAX_IMAGE_SIDE / max(width, height))
                    with closing(page.render(scale=scale)) as bitmap:
                        with bitmap.to_pil() as image:
                            image_path = destination / f'pdf-{index + 1}.png'
                            image.save(image_path)
                    entries.append({'text': text, 'image': image_path,
                                    'name': f'{path.name} · {index + 1}쪽' + ('' if text else ' · OCR 필요')})
    except pdfium.PdfiumError as error:
        raise ValueError('PDF를 열 수 없습니다. 암호를 해제하거나 손상 여부를 확인하세요.') from error
    return entries


def prepare_hwpx(path, destination):
    text = read_hwpx_file(path)
    entries = [{'text': text, 'image': None, 'name': path.name}]
    with zipfile.ZipFile(path) as archive:
        # Import raster images referenced by section picture elements, rather
        # than every binary asset stored in the package.
        references = []
        section_names = [name for name in archive.namelist() if name.startswith('Contents/section') and name.endswith('.xml')]
        def section_order(name):
            number = name[len('Contents/section'):-4].lstrip('0') or '0'
            return len(number), number
        for name in sorted(section_names, key=section_order):
            root = ElementTree.fromstring(archive.read(name))
            for node in root.iter():
                identity = node.get('binaryItemIDRef')
                if node.tag.rsplit('}', 1)[-1] == 'img' and identity and identity not in references:
                    references.append(identity)
        items = {}
        if 'Contents/content.hpf' in archive.namelist():
            for node in ElementTree.fromstring(archive.read('Contents/content.hpf')).iter():
                if node.tag.rsplit('}', 1)[-1] == 'item':
                    href = node.get('href', '')
                    if '..' not in PurePosixPath(href).parts and not href.startswith('/'):
                        items[node.get('id')] = href if href in archive.namelist() else 'Contents/' + href
        for identity in references:
            name = items.get(identity)
            if not name or name not in archive.namelist():
                raise ValueError('HWPX 삽입 이미지 연결을 찾지 못했습니다.')
            if Path(name).suffix.lower() not in {'.png', '.jpg', '.jpeg', '.bmp', '.gif', '.webp'}:
                raise ValueError('HWPX에 지원하지 않는 그림 형식이 있습니다. PDF로 저장해 열어 주세요.')
            if len(entries) >= MAX_PAGES:
                raise ValueError('HWPX 삽입 이미지가 너무 많습니다. 파일을 나눠 주세요.')
            with Image.open(BytesIO(archive.read(name))) as image:
                if image.width * image.height > 40_000_000:
                    raise ValueError('HWPX 삽입 이미지가 너무 큽니다.')
                converted = destination / f'hwpx-{len(entries)}.png'
                image.save(converted)
            entries.append({'text': '', 'image': converted,
                            'name': f'{path.name} · 삽입 이미지 {len(entries)} · OCR 필요'})
    if not text.strip() and len(entries) == 1:
        raise ValueError('HWPX에서 읽을 본문이나 삽입 이미지를 찾지 못했습니다.')
    return entries


def import_local_document(library, path):
    path = Path(path).resolve()
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError('파일이 50MB를 초과합니다. 파일을 나눠 주세요.')
    if path.suffix.lower() not in LOCAL_DOCUMENT_EXTENSIONS:
        raise ValueError('지원하는 문서 파일을 선택하세요.')
    with tempfile.TemporaryDirectory(prefix='ssokly-document-') as temp:
        if path.suffix.lower() in {'.pdf', '.hwpx'}:
            entries = (prepare_pdf if path.suffix.lower() == '.pdf' else prepare_hwpx)(path, Path(temp))
        else:
            entries = read_office(path)
            if not entries or not any(entry['text'].strip() for entry in entries):
                raise ValueError('읽을 텍스트가 없습니다. 그림·차트 문서는 PDF로 저장해 열어 주세요.')
        if len(entries) > MAX_PAGES:
            raise ValueError('한 번에 100개 항목까지 가져올 수 있습니다. 파일을 나눠 주세요.')
        for entry in entries:
            entry['capture_id'] = None
            if entry['image']:
                with Image.open(entry['image']) as image:
                    capture = library.capture_store.save(image, source='document')
                entry['capture_id'] = capture.id
                if entry['text']:
                    library.capture_store.update_ocr(capture.id, entry['text'], profile='local-pdf')
        document = library.create_imported_document(path.stem, entries, str(path))
    return document, sum(not entry['text'] and bool(entry['capture_id']) for entry in entries)

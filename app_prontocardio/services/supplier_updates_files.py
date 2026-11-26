import io
from pathlib import PurePath
import zipfile

MAX_FILE = 10 * 1024 * 1024
MAX_TOTAL = 25 * 1024 * 1024


def validate_attachment(name, content):
    name = PurePath(name.replace('\\', '/')).name[:200]
    extension = PurePath(name).suffix.lower()
    if not content or len(content) > MAX_FILE:
        raise ValueError('Cada arquivo deve ter conteúdo e no máximo 10 MB.')
    types = {'.pdf': 'application/pdf', '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
             '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', '.csv': 'text/csv'}
    if extension not in types:
        raise ValueError('Envie somente PDF, PNG, JPG, XLSX ou CSV.')
    valid = False
    if extension == '.pdf':
        valid = content.startswith(b'%PDF-')
    elif extension == '.png':
        valid = content.startswith(b'\x89PNG\r\n\x1a\n')
    elif extension in ('.jpg', '.jpeg'):
        valid = content.startswith(b'\xff\xd8\xff')
    elif extension == '.csv':
        try:
            text = content.decode('utf-8-sig')
            valid = '\x00' not in text and not text.lstrip().startswith(('<', '%PDF', 'MZ'))
        except UnicodeError:
            pass
    elif extension == '.xlsx':
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                names = archive.namelist()
                valid = '[Content_Types].xml' in names and 'xl/workbook.xml' in names and not any('vbaproject' in n.casefold() for n in names)
                valid = valid and len(names) < 3000 and sum(i.file_size for i in archive.infolist()) <= 100 * 1024 * 1024
        except (zipfile.BadZipFile, OSError):
            pass
    if not valid:
        raise ValueError('O conteúdo do arquivo não corresponde ao formato permitido.')
    return {'name': name, 'content_type': types[extension], 'content': content}

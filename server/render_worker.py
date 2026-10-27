"""Bounded, isolated document/image renderer. Invoked only by app.py."""
import json
from pathlib import Path
import sys
import warnings

if sys.platform.startswith('linux'):
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (1536 * 1024 * 1024, 1536 * 1024 * 1024))

import fitz
from PIL import Image, ImageOps
Image.MAX_IMAGE_PIXELS = 50_000_000
warnings.simplefilter('error', Image.DecompressionBombWarning)


def render(kind, source, folder):
    paths = []
    if kind == 'image':
        target = folder / 'image.jpg'
        with Image.open(source) as image:
            image.seek(0)
            image = ImageOps.exif_transpose(image)
            image.thumbnail((3840, 2160))
            if image.mode in ('RGBA', 'LA') or 'transparency' in image.info:
                rgba = image.convert('RGBA')
                canvas = Image.new('RGB', rgba.size, 'white')
                canvas.paste(rgba, mask=rgba.getchannel('A'))
                image = canvas
            image.convert('RGB').save(target, quality=92)
        paths.append(target.name)
    else:
        with source.open('rb') as stream:
            if b'%PDF-' not in stream.read(1024):
                raise ValueError('文件不是有效的 PDF。')
        with fitz.open(source) as document:
            if document.needs_pass:
                raise ValueError('PDF 有打开密码，请先导出无密码版本。')
            if not 1 <= len(document) <= 200:
                raise ValueError('每份文档支持 1～200 页，请拆分后重新上传。')
            for i, page in enumerate(document):
                scale = min(1920 / max(page.rect.width, 1), 1080 / max(page.rect.height, 1), 2.5)
                target = folder / f'page-{i + 1:03}.jpg'
                page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False).save(target, jpg_quality=90)
                paths.append(target.name)
    return paths


if __name__ == '__main__':
    try:
        files = render(sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]))
        print(json.dumps({'files': files}))
    except Exception as error:
        print(json.dumps({'error': str(error) if isinstance(error, ValueError) else '素材无法渲染，可能已损坏、过于复杂或超过内存上限。'}, ensure_ascii=False))
        sys.exit(1)

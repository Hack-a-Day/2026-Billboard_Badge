"""Receive and validate a device-sized, indexed sleep image."""
import os

PATH = '/data/graphics/pocketreader-sleep.png'
MAX_BYTES = 256 * 1024
SIGNATURE = b'\x89PNG\r\n\x1a\n'


def validate(path):
    with open(path, 'rb') as source:
        data = source.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES or data[:8] != SIGNATURE:
        raise ValueError('Use an optimized PNG no larger than 256 KB')
    pos = 8
    header = palette = None
    has_image = has_end = False
    while pos + 12 <= len(data):
        length = int.from_bytes(data[pos:pos + 4], 'big')
        kind = data[pos + 4:pos + 8]
        end = pos + 12 + length
        if end > len(data):
            raise ValueError('PNG file is incomplete')
        chunk = data[pos + 8:pos + 8 + length]
        if kind == b'IHDR':
            if header is not None or length != 13:
                raise ValueError('Invalid PNG header')
            header = chunk
        elif kind == b'PLTE':
            palette = chunk
        elif kind == b'tRNS':
            raise ValueError('Transparent images are not supported')
        elif kind == b'IDAT':
            has_image = True
        elif kind == b'IEND':
            has_end = True
            break
        pos = end
    if header is None or not has_image or not has_end:
        raise ValueError('PNG file is incomplete')
    width = int.from_bytes(header[:4], 'big')
    height = int.from_bytes(header[4:8], 'big')
    depth, color, compression, filtering, interlace = header[8:13]
    if (width, height) != (960, 672):
        raise ValueError('Image must be 960 by 672 pixels in badge orientation')
    if compression or filtering or interlace:
        raise ValueError('Use a non-interlaced PNG')
    if color == 0 and depth == 1:
        pass
    elif color == 3 and depth in (1, 2) and palette and len(palette) % 3 == 0 and len(palette) <= (1 << depth) * 3:
        allowed = (b'\x00\x00\x00', b'\xff\xff\xff', b'\xff\x00\x00')
        if any(palette[i:i + 3] not in allowed for i in range(0, len(palette), 3)):
            raise ValueError('Image colors must be black, white, or red')
    else:
        raise ValueError('Use a 1-bit black and white or indexed black, white, and red PNG')
    # Use the badge's own decoder to reject truncated or unsupported images.
    from lib.large_png import _read_palette_png
    _, _, _, rows, _ = _read_palette_png(path)
    if sum(1 for _ in rows) != 672:
        raise ValueError('PNG image is incomplete')


def receive(length, initial, connection):
    if length < 1 or length > MAX_BYTES:
        raise ValueError('Image must be no larger than 256 KB')
    temporary, previous = PATH + '.upload', PATH + '.previous'
    try:
        with open(temporary, 'wb') as output:
            data = initial[:length]
            output.write(data)
            remaining = length - len(data)
            while remaining:
                chunk = connection.recv(min(2048, remaining))
                if not chunk:
                    raise ValueError('Image upload was interrupted')
                output.write(chunk)
                remaining -= len(chunk)
        validate(temporary)
        try:
            os.stat(previous)
            raise ValueError('Previous image recovery is needed')
        except OSError:
            pass
        try:
            os.stat(PATH)
            had_previous = True
        except OSError:
            had_previous = False
        if had_previous:
            os.rename(PATH, previous)
        try:
            os.rename(temporary, PATH)
        except OSError:
            if had_previous:
                os.rename(previous, PATH)
            raise
        if had_previous:
            os.remove(previous)
    finally:
        try:
            os.remove(temporary)
        except OSError:
            pass

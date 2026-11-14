"""Shared streaming text layout for the converter and badge."""

# Increment for changes to page geometry or font rendering that don't alter
# advance widths or the number of lines. Existing indexes then rebuild.
PAGINATION_VERSION = 1
MARKUP_LAYOUT_VERSION = 2
CENTER = "\x1fC"
HEADING = "\x1fH"
LARGE = "\x1fL"


def needs_heading_page_break(text, count, limit, has_larger_font):
    return bool(has_larger_font and count == limit - 1 and text and (text.startswith(HEADING) or text.startswith(LARGE)))

def layout_lines(handle, data_offset, advances, position=None, body_end=None, heading_advances=None, markup=False):
    """Yield wrapped lines with file coordinates and paragraph boundaries."""
    handle.seek(data_offset if position is None else position)
    word_number = 0
    while True:
        offset = handle.tell()
        if body_end is not None and offset >= body_end:
            return
        record = handle.readline()
        if not record:
            return
        record = record.decode("utf-8", "replace").rstrip("\r\n")
        if record == "\x1e":
            yield (offset, 0, word_number, None)
            continue
        marker = ''
        if markup and (record.startswith(CENTER) or record.startswith(HEADING) or record.startswith(LARGE)):
            marker, record = record[:2], record[2:]
        selected_advances = heading_advances if marker in (HEADING, LARGE) and heading_advances is not None else advances
        has_larger_font = marker in (HEADING, LARGE) and heading_advances is not None and heading_advances != advances
        words = record.split()
        current = ""
        current_width = 0
        first_word = word_number
        line_index = 0
        space_width = selected_advances[0]
        for word in words:
            chunks = []
            chunk = ""
            width = 0
            for character in word:
                char_width = selected_advances[ord(character)-32] if 32 <= ord(character) <= 126 else selected_advances[31]
                if chunk and width + char_width > 608:
                    chunks.append((chunk, width))
                    chunk = ""
                    width = 0
                chunk += character
                width += char_width
            if chunk:
                chunks.append((chunk, width))
            for chunk, width in chunks:
                if current and current_width + space_width + width > 608:
                    yield (offset, line_index, first_word, marker + current)
                    line_index += 1
                    if has_larger_font:
                        yield (offset, line_index, word_number, "")
                        line_index += 1
                    current = ""
                    current_width = 0
                    first_word = word_number
                if current:
                    current += " "
                    current_width += space_width
                current += chunk
                current_width += width
            word_number += 1
        if current:
            yield (offset, line_index, first_word, marker + current)
            line_index += 1
            if has_larger_font:
                yield (offset, line_index, word_number, "")
                line_index += 1
            yield (offset, line_index, word_number, "")


def pack_page_positions(pages):
    """Encode each page as unsigned varints: byte-offset delta, line, word delta.

    Values use seven low bits per byte and a continuation bit; the bytes are
    base64-encoded for the JSON sidecar. Legacy lists remain readable.
    """
    import binascii
    data = bytearray()
    previous_offset = previous_word = 0
    for offset, line, word in pages:
        values = (offset - previous_offset, line, word - previous_word)
        if any(not isinstance(value, int) or value < 0 for value in values):
            raise ValueError('Invalid page position')
        for value in values:
            while value >= 128:
                data.append((value & 127) | 128)
                value >>= 7
            data.append(value)
        previous_offset, previous_word = offset, word
    return binascii.b2a_base64(data).decode().strip()


def unpack_page_positions(encoded):
    """Decode compact positions; reject truncated or oversized indexes."""
    import binascii
    try:
        data = binascii.a2b_base64(encoded)
    except (ValueError, TypeError):
        raise ValueError('Invalid page index encoding')
    if len(data) > 1200000:
        raise ValueError('Page index too large')
    values, pages = [], []
    value = shift = 0
    offset = word = 0
    for byte in data:
        value |= (byte & 127) << shift
        if byte & 128:
            shift += 7
            if shift > 35:
                raise ValueError('Page index value too large')
            continue
        values.append(value)
        value = shift = 0
        if len(values) == 3:
            offset += values[0]
            word += values[2]
            pages.append([offset, values[1], word])
            if len(pages) > 100000:
                raise ValueError('Page index too large')
            values = []
    if shift or values or not pages:
        raise ValueError('Incomplete page index')
    return pages

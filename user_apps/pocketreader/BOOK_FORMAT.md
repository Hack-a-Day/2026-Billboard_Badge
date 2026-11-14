# PocketReader book format

A PocketReader book is a UTF-8 `.prb` file. Its first line is a JSON object,
followed by a newline and the book body. The `id` is stable for the book;
the displayed title and author come from the header rather than the filename.
IDs use one to 96 ASCII letters, digits, hyphens, or underscores.
Reading position and bookmarks are stored separately on the badge.

The reader supports existing schema 1, 2, and 3 books. Schema 4 adds the
following basic block formatting to the reflowable body:

- Each body line is a paragraph record. A line containing only U+001E is an
  explicit page break.
- U+001F followed by `C` at the start of a record centers that paragraph.
- U+001F followed by `H` centers the paragraph and uses the next larger reader
  font size, up to the largest available size.
- U+001F followed by `L` uses the next larger reader font size, left aligned.
- A record without a prefix is ordinary left-aligned text. Prefixes are control
  data, not text or separate words.

For example, the following uses `\u001f` and `\u001e` to show control characters;
they represent single UTF-8 control bytes in the actual file:

```text
{"schema":4,"id":"example","title":"Example","author":"Writer","word_count":6}
\u001fHExample
\u001fCWriter
\u001e
Here is the story.
```

The reader wraps records to the selected font and size. Larger lines reserve
extra vertical space when needed; a heading is moved to the next page if it
would otherwise be split at the bottom. The largest text size stays at the
largest font. Explicit breaks start a new page. The converter retains basic
centering, larger block text, and page breaks; it does not yet preserve every
EPUB style, including inline bold and italic.

A matching `.pri` sidecar is JSON with `version: 1`, `book_id`, the SHA-256
of the complete `.prb`, `chapters`, and `sizes`. A chapter is a pair of
zero-based word position and title. Size indexes use `layout_version: 2` for
schema 4 and store delta-varint, base64-encoded triples of body-byte offset,
wrapped-line index, and word position. The font metrics hashes and line count
identify the layout. The reader can rebuild missing or incompatible size
indexes; chapter information remains independent of font size. Schema 3 may
also contain binary page indexes after the body, with their offsets described
in the header.

Unknown metadata fields may be ignored. Converters should avoid emitting
unsupported markup or attributing words to the formatting control bytes.

To check a book and its sidecar before distributing them, run:

```sh
python3 validate_book.py example.prb example.pri
```

The index argument is optional. The validator checks the book, matching hash,
chapter positions, and encoded page-index boundaries.

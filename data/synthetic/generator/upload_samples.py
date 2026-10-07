"""A few tiny files for the upload demo, made in code with the standard library.

The claim upload route (S070) takes a PDF, a JPEG or a PNG of up to 1 MiB, known
by its first bytes alone. The demo needs files to post, and none may be a real
person's document, so the generator writes its own: a one-page PDF, a JPEG and a
PNG that each say SYNTHETIC in their visible content and in their metadata, and
one plain-text file named like a PDF that the route must refuse.

They live in their own folder with their own manifest, outside the golden set:
the evaluation fingerprints every file its manifest lists and refuses an unlisted
one beside them, so a new file there would move the gate (S071's held-out set
taught the same lesson). Nothing here reads a clock or a hash-ordered set; the
only draw is the claim the PDF names, from a stream seeded with the seed and a
fixed label.

How each is made, so no binary blob is copied from anywhere:

- the PDF is written object by object, with the byte offsets of its cross-reference
  table counted, and no date and no identifier in it;
- the JPEG is a baseline grayscale image whose every 8x8 block is flat, so each
  block has a DC coefficient and no other: the generator codes those with the
  standard luminance DC Huffman table and a one-code AC table (end of block);
- the PNG is a grayscale image whose compressed stream is made of stored deflate
  blocks, written here, because the bytes ``zlib`` compresses to differ between
  builds of zlib and a committed file must not.
"""

import hashlib
import random
import struct
import zlib

from . import GENERATOR_VERSION, WORKLOAD
from .scenarios import Dataset

FOLDER = "upload-samples"
MANIFEST = "manifest.json"
# The bound the upload route's own limit sits far above (1 MiB): a sample stays
# small so the repository does, and the demo posts it at once.
MAX_SAMPLE_BYTES = 20 * 1024
STREAM_LABEL = "upload-samples"

PDF_NAME = "synthetic-document.pdf"
JPEG_NAME = "synthetic-photo.jpg"
PNG_NAME = "synthetic-photo.png"
REFUSED_NAME = "not-a-pdf.pdf"

# The route sniffs the first bytes and ignores the name and the declared type.
ACCEPTS, REFUSES = "accepts", "refuses"
STORED_STATUS, REFUSED_STATUS = 201, 415

BANNER = "SYNTHETIC"
METADATA_TEXT = (
    "SYNTHETIC sample of the Meridian Insurance upload demo. "
    "Fictional insurer, not a real document."
)

# A 5x7 bitmap font for the letters of BANNER; "X" is ink.
GLYPHS: dict[str, tuple[str, ...]] = {
    "S": (".XXX.", "X...X", "X....", ".XXX.", "....X", "X...X", ".XXX."),
    "Y": ("X...X", "X...X", ".X.X.", "..X..", "..X..", "..X..", "..X.."),
    "N": ("X...X", "XX..X", "X.X.X", "X..XX", "X...X", "X...X", "X...X"),
    "T": ("XXXXX", "..X..", "..X..", "..X..", "..X..", "..X..", "..X.."),
    "H": ("X...X", "X...X", "X...X", "XXXXX", "X...X", "X...X", "X...X"),
    "E": ("XXXXX", "X....", "X....", "XXXX.", "X....", "X....", "XXXXX"),
    "I": (".XXX.", "..X..", "..X..", "..X..", "..X..", "..X..", ".XXX."),
    "C": (".XXXX", "X....", "X....", "X....", "X....", "X....", ".XXXX"),
}
GLYPH_GAP = 1
MARGIN = 1
PAPER, INK = 235, 25  # gray levels of the page and of the letters


def banner_pixels() -> list[list[int]]:
    """The banner as rows of gray levels, with a margin of paper around it."""
    rows = []
    for line in range(7):
        row = []
        for index, letter in enumerate(BANNER):
            if index:
                row += [PAPER] * GLYPH_GAP
            row += [INK if cell == "X" else PAPER for cell in GLYPHS[letter][line]]
        rows.append([PAPER] * MARGIN + row + [PAPER] * MARGIN)
    width = len(rows[0])
    blank = [[PAPER] * width for _ in range(MARGIN)]
    return blank + rows + blank


# -- PDF -----------------------------------------------------------------------------
def pdf_text(claim_id: str) -> tuple[str, ...]:
    """The lines on the page: ASCII, no parenthesis or backslash (the string
    syntax of a PDF would need an escape for them)."""
    return (
        "SYNTHETIC SAMPLE - NOT A REAL DOCUMENT",
        "Meridian Insurance upload demo. Meridian is a fictional insurer.",
        f"Supporting document for the synthetic claim {claim_id}.",
        "Made by the seeded generator: no real person, policy or address.",
    )


def render_pdf(claim_id: str) -> bytes:
    """A one-page PDF 1.4 of four lines of Helvetica, with a document
    information dictionary and no date, no identifier and no producer version."""
    lines = pdf_text(claim_id)
    drawing = ["BT", "/F1 12 Tf", "16 TL", "20 160 Td"]
    drawing += [f"({line}) '" for line in lines]
    drawing.append("ET")
    stream = ("\n".join(drawing) + "\n").encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 420 200] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"endstream",
        b"<< /Title (Synthetic sample - Meridian Insurance upload demo) "
        b"/Subject (SYNTHETIC - not a real document) "
        b"/Author (Meridian synthetic data generator) >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R /Info 6 0 R >>\n" % (len(objects) + 1)
    out += b"startxref\n%d\n%%%%EOF\n" % xref_at
    return bytes(out)


# -- JPEG ----------------------------------------------------------------------------
JPEG_BLOCK = 8
JPEG_QUANT = 8  # one DC step is one gray level: coefficient = level - 128
# The standard luminance DC table (ITU T.81, table K.3): code lengths 1 to 16.
DC_COUNTS = (0, 1, 5, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0)
DC_SYMBOLS = tuple(range(12))
# The AC table holds one code, the end of the block (symbol 0), of one bit.
AC_COUNTS = (1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
AC_SYMBOLS = (0,)


def huffman_codes(
    counts: tuple[int, ...], symbols: tuple[int, ...]
) -> dict[int, tuple[int, int]]:
    """The canonical codes of a JPEG table: symbol to (code, length)."""
    codes, code, index = {}, 0, 0
    for length, count in enumerate(counts, start=1):
        for _ in range(count):
            codes[symbols[index]] = (code, length)
            code, index = code + 1, index + 1
        code <<= 1
    return codes


def jpeg_segment(marker: int, body: bytes) -> bytes:
    return struct.pack(">BBH", 0xFF, marker, len(body) + 2) + body


def jpeg_scan(levels: list[list[int]]) -> bytes:
    """The entropy-coded data: for each block in reading order, the difference of
    its DC coefficient from the one before (a size code, then that many bits) and
    the end of block. A 0xFF byte is followed by 0x00; the last byte is padded
    with one bits."""
    dc_codes = huffman_codes(DC_COUNTS, DC_SYMBOLS)
    ac_code, ac_length = huffman_codes(AC_COUNTS, AC_SYMBOLS)[0]
    bits, count, out = 0, 0, bytearray()

    def put(code: int, length: int) -> None:
        nonlocal bits, count
        bits, count = (bits << length) | code, count + length
        while count >= 8:
            byte = (bits >> (count - 8)) & 0xFF
            out.append(byte)
            if byte == 0xFF:
                out.append(0x00)
            count -= 8
            bits &= (1 << count) - 1

    previous = 0
    for row in levels:
        for level in row:
            coefficient = level - 128
            diff, previous = coefficient - previous, coefficient
            size = abs(diff).bit_length()
            put(*dc_codes[size])
            if size:
                put(diff if diff > 0 else diff + (1 << size) - 1, size)
            put(ac_code, ac_length)
    if count:
        put((1 << (8 - count)) - 1, 8 - count)
    return bytes(out)


def render_jpeg() -> bytes:
    """A baseline grayscale JPEG: the banner, one 8x8 block to a bitmap cell."""
    levels = banner_pixels()
    height, width = len(levels) * JPEG_BLOCK, len(levels[0]) * JPEG_BLOCK
    jfif = b"JFIF\x00" + struct.pack(">BBBHHBB", 1, 1, 0, 1, 1, 0, 0)
    return b"".join(
        [
            b"\xff\xd8",
            jpeg_segment(0xE0, jfif),
            jpeg_segment(0xFE, METADATA_TEXT.encode("ascii")),
            jpeg_segment(0xDB, b"\x00" + bytes([JPEG_QUANT]) * 64),
            jpeg_segment(
                0xC0, struct.pack(">BHHBBBB", 8, height, width, 1, 1, 0x11, 0)
            ),
            jpeg_segment(0xC4, b"\x00" + bytes(DC_COUNTS) + bytes(DC_SYMBOLS)),
            jpeg_segment(0xC4, b"\x10" + bytes(AC_COUNTS) + bytes(AC_SYMBOLS)),
            jpeg_segment(0xDA, bytes([1, 1, 0x00, 0, 63, 0])),
            jpeg_scan(levels),
            b"\xff\xd9",
        ]
    )


# -- PNG -----------------------------------------------------------------------------
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_SCALE = 4
STORED_BLOCK_MAX = 65535


def png_chunk(kind: bytes, body: bytes) -> bytes:
    crc = zlib.crc32(kind + body)
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc)


def stored_deflate(raw: bytes) -> bytes:
    """A zlib stream of stored (uncompressed) blocks: the same bytes on every
    build of zlib, which compressed output is not."""
    out = bytearray(b"\x78\x01")
    blocks = [
        raw[start : start + STORED_BLOCK_MAX]
        for start in range(0, len(raw), STORED_BLOCK_MAX)
    ] or [b""]
    for index, block in enumerate(blocks):
        final = 1 if index == len(blocks) - 1 else 0
        out += struct.pack("<BHH", final, len(block), len(block) ^ 0xFFFF) + block
    out += struct.pack(">I", zlib.adler32(raw))
    return bytes(out)


def render_png() -> bytes:
    """An 8-bit grayscale PNG of the banner, each bitmap cell PNG_SCALE pixels
    square, with the synthetic notice in two text chunks."""
    cells = banner_pixels()
    rows = []
    for cell_row in cells:
        pixels = bytes(level for level in cell_row for _ in range(PNG_SCALE))
        rows += [b"\x00" + pixels] * PNG_SCALE  # filter type 0 on every row
    width, height = len(cells[0]) * PNG_SCALE, len(cells) * PNG_SCALE
    return b"".join(
        [
            PNG_SIGNATURE,
            png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)),
            png_chunk(b"tEXt", b"Title\x00" + b"Synthetic sample, Meridian demo"),
            png_chunk(b"tEXt", b"Comment\x00" + METADATA_TEXT.encode("ascii")),
            png_chunk(b"IDAT", stored_deflate(b"".join(rows))),
            png_chunk(b"IEND", b""),
        ]
    )


# -- the refused file ----------------------------------------------------------------
def render_refused() -> bytes:
    """Plain text with a PDF's name: its first bytes are not those of a PDF."""
    return (
        "SYNTHETIC sample of the Meridian Insurance upload demo.\n"
        "This is plain text that carries the name of a PDF. The upload route\n"
        "knows a file by its first bytes alone, so it refuses this one.\n"
        "Fictional insurer, not a real document.\n"
    ).encode("ascii")


# -- the folder ----------------------------------------------------------------------
def sample_claim_id(dataset: Dataset, seed: int) -> str:
    """A claim the golden set holds that waits for documents, drawn from a stream
    of its own: a supporting document is what such a claim lacks."""
    waiting = sorted(
        outcome["claim_id"]
        for outcome in dataset.outcomes
        if outcome["reason"] == "missing_documents"
    )
    return random.Random(f"{seed}:{STREAM_LABEL}").choice(waiting)


def build_manifest(seed: int, files: dict[str, bytes]) -> dict:
    """What each file is and what the upload route does with it, by file name."""
    kinds = {
        PDF_NAME: "application/pdf",
        JPEG_NAME: "image/jpeg",
        PNG_NAME: "image/png",
        REFUSED_NAME: "text/plain",
    }
    return {
        "synthetic": True,
        "workload": WORKLOAD,
        "generator_version": GENERATOR_VERSION,
        "seed": seed,
        "files": [
            {
                "name": name,
                "media_type": kinds[name],
                "size_bytes": len(files[name]),
                "sha256": hashlib.sha256(files[name]).hexdigest(),
                "upload_route": REFUSES if name == REFUSED_NAME else ACCEPTS,
                "upload_status": REFUSED_STATUS
                if name == REFUSED_NAME
                else STORED_STATUS,
            }
            for name in sorted(files)
        ],
    }


def render_files(dataset: Dataset, seed: int) -> dict[str, bytes]:
    """The four files by name; the manifest is built from them."""
    files = {
        PDF_NAME: render_pdf(sample_claim_id(dataset, seed)),
        JPEG_NAME: render_jpeg(),
        PNG_NAME: render_png(),
        REFUSED_NAME: render_refused(),
    }
    too_large = sorted(
        name for name, data in files.items() if len(data) > MAX_SAMPLE_BYTES
    )
    if too_large:
        raise ValueError(f"samples over {MAX_SAMPLE_BYTES} bytes: {too_large}")
    return files

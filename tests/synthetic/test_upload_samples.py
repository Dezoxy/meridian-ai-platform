"""The upload demo's sample files: valid, small, synthetic, generated, and outside
the golden set.

The three good files start with the first bytes the upload route sniffs (the test
uses the route's own function), each is far under its size limit, and the file
named like a PDF is the one the route refuses. The manifest in the folder matches
the files, a second generation is byte-identical, and the golden manifest and the
evaluation gate's check of the golden set do not see the folder.
"""

import hashlib
import json
import re
import shutil
import struct
import subprocess
import zlib
from pathlib import Path

import pytest
from generator import upload_samples as samples
from generator.scenarios import build_dataset
from generator.upload_samples import huffman_codes

from meridian.platform.evaluation.fingerprints import golden_set_of
from meridian.workloads.claims_triage.uploads import (
    MAX_FILE_BYTES,
    SIGNATURES,
    sniff_media_type,
)

FOLDER = "upload-samples"
GOOD = {
    samples.PDF_NAME: "application/pdf",
    samples.JPEG_NAME: "image/jpeg",
    samples.PNG_NAME: "image/png",
}
SIZE_BOUND = 20 * 1024
SEED = 20260929


@pytest.fixture(scope="module")
def folder(synthetic_dir: Path) -> Path:
    return synthetic_dir / FOLDER


@pytest.fixture(scope="module")
def committed(folder: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted(folder.iterdir())}


@pytest.fixture(scope="module")
def manifest(committed: dict[str, bytes]) -> dict:
    return json.loads(committed["manifest.json"])


# -- what the route sees -----------------------------------------------------------
@pytest.mark.parametrize("name", sorted(GOOD))
def test_a_good_file_starts_with_the_bytes_of_its_type(committed, name: str):
    # Arrange
    signature = dict((media, prefix) for prefix, media in SIGNATURES)[GOOD[name]]

    # Act
    content = committed[name]

    # Assert
    assert content.startswith(signature)
    assert sniff_media_type(content) == GOOD[name]


@pytest.mark.parametrize("name", [*sorted(GOOD), samples.REFUSED_NAME])
def test_every_sample_is_small_and_far_under_the_routes_limit(committed, name: str):
    assert 0 < len(committed[name]) < SIZE_BOUND < MAX_FILE_BYTES


def test_the_file_named_like_a_pdf_is_not_one(committed, manifest):
    # Arrange
    content = committed[samples.REFUSED_NAME]
    entry = next(f for f in manifest["files"] if f["name"] == samples.REFUSED_NAME)

    # Act
    sniffed = sniff_media_type(content)

    # Assert
    assert samples.REFUSED_NAME.endswith(".pdf")
    assert not content.startswith(b"%PDF")
    assert sniffed is None
    assert entry["upload_route"] == "refuses"
    assert entry["upload_status"] == 415


def test_the_manifest_says_the_route_accepts_each_good_file(manifest):
    # Act
    accepted = {f["name"] for f in manifest["files"] if f["upload_route"] == "accepts"}

    # Assert
    assert accepted == set(GOOD)


# -- the manifest ------------------------------------------------------------------
def test_the_manifest_matches_the_files(committed, manifest):
    # Arrange
    listed = [entry["name"] for entry in manifest["files"]]

    # Act
    actual = {
        name: (len(data), hashlib.sha256(data).hexdigest())
        for name, data in committed.items()
        if name != "manifest.json"
    }

    # Assert: no file is missing from the manifest and none is extra
    assert listed == sorted(listed)
    assert set(listed) == set(actual)
    for entry in manifest["files"]:
        assert (entry["size_bytes"], entry["sha256"]) == actual[entry["name"]]
        assert entry["media_type"] in {*GOOD.values(), "text/plain"}
    assert manifest["synthetic"] is True
    assert manifest["seed"] == SEED


def test_the_declared_type_of_each_good_file_is_the_sniffed_one(committed, manifest):
    for entry in manifest["files"]:
        if entry["upload_route"] == "accepts":
            assert sniff_media_type(committed[entry["name"]]) == entry["media_type"]


# -- generated, and the same every time --------------------------------------------
def test_the_committed_samples_are_what_the_seed_generates(generated, committed):
    # Arrange
    fresh = {
        path.removeprefix(f"{FOLDER}/"): data
        for path, data in generated.items()
        if path.startswith(f"{FOLDER}/")
    }

    # Assert
    assert fresh == committed, "run `make synthetic` and commit the upload samples"


def test_a_second_generation_is_byte_identical():
    # Act: two builds from scratch in one process
    first = samples.render_files(build_dataset(SEED), SEED)
    second = samples.render_files(build_dataset(SEED), SEED)

    # Assert
    assert first == second
    assert len(first) == 4


# -- synthetic, in the content and in the metadata ---------------------------------
def test_every_sample_says_it_is_synthetic(committed):
    # Assert
    for name in (*GOOD, samples.REFUSED_NAME):
        assert b"SYNTHETIC" in committed[name], name
    assert b"Meridian" in committed[samples.PDF_NAME]
    assert b"Meridian" in committed[samples.PNG_NAME]
    assert b"Meridian" in committed[samples.JPEG_NAME]


def test_the_pdf_names_a_claim_of_the_golden_set(committed, claims):
    # Arrange
    ids = {claim["claim_id"] for claim in claims}

    # Act
    named = set(re.findall(rb"CLM-[0-9]{4}", committed[samples.PDF_NAME]))

    # Assert
    assert len(named) == 1
    assert {name.decode() for name in named} <= ids


def test_the_pdf_has_no_date_and_no_identifier(committed):
    # Assert
    pdf = committed[samples.PDF_NAME]
    for key in (b"/CreationDate", b"/ModDate", b"/ID", b"/Producer"):
        assert key not in pdf


def test_the_text_and_the_metadata_are_plain_ascii(committed):
    for name in (samples.PDF_NAME, samples.REFUSED_NAME):
        assert committed[name].isascii(), name
    assert samples.METADATA_TEXT.isascii()


# -- each file is well formed ------------------------------------------------------
def test_the_pdf_cross_reference_table_points_at_its_objects(committed):
    # Arrange
    pdf = committed[samples.PDF_NAME]
    start = int(re.search(rb"startxref\n([0-9]+)\n%%EOF\n$", pdf).group(1))

    # Act
    table = pdf[start:].split(b"\n")
    count = int(table[1].split()[1])
    entries = table[2 : 2 + count]

    # Assert
    assert table[0] == b"xref"
    assert entries[0] == b"0000000000 65535 f "
    for number, entry in enumerate(entries[1:], start=1):
        assert len(entry) == 19  # 20 with its line feed
        offset = int(entry[:10])
        assert pdf[offset:].startswith(b"%d 0 obj\n" % number)
    assert pdf.startswith(b"%PDF-1.4\n")
    data = re.search(rb"stream\n(.*?)endstream", pdf, re.DOTALL).group(1)
    assert b"/Length %d" % len(data) in pdf


def png_chunks(content: bytes) -> list[tuple[bytes, bytes]]:
    chunks, at = [], len(samples.PNG_SIGNATURE)
    while at < len(content):
        length, kind = struct.unpack(">I4s", content[at : at + 8])
        body = content[at + 8 : at + 8 + length]
        (crc,) = struct.unpack(">I", content[at + 8 + length : at + 12 + length])
        assert crc == zlib.crc32(kind + body), kind
        chunks.append((kind, body))
        at += 12 + length
    assert at == len(content)
    return chunks


def test_the_png_chunks_checksum_and_the_image_decompresses(committed):
    # Act
    chunks = png_chunks(committed[samples.PNG_NAME])
    header = dict(chunks)[b"IHDR"]
    width, height, depth, colour = struct.unpack(">IIBB", header[:10])
    raw = zlib.decompress(dict(chunks)[b"IDAT"])

    # Assert
    assert [kind for kind, _ in chunks] == [b"IHDR", b"tEXt", b"tEXt", b"IDAT", b"IEND"]
    assert (depth, colour) == (8, 0)
    assert len(raw) == height * (1 + width)
    rows = [raw[at : at + 1 + width] for at in range(0, len(raw), 1 + width)]
    assert {row[0] for row in rows} == {0}
    assert {*raw} - {0} == {samples.PAPER, samples.INK}


def jpeg_segments(content: bytes) -> tuple[dict[int, list[bytes]], bytes]:
    """The marker segments before the scan, and the scan's data."""
    assert content[:2] == b"\xff\xd8" and content[-2:] == b"\xff\xd9"
    found: dict[int, list[bytes]] = {}
    at = 2
    while True:
        assert content[at] == 0xFF
        marker = content[at + 1]
        (length,) = struct.unpack(">H", content[at + 2 : at + 4])
        found.setdefault(marker, []).append(content[at + 4 : at + 2 + length])
        at += 2 + length
        if marker == 0xDA:
            return found, content[at:-2]


def decode_levels(content: bytes) -> list[list[int]]:
    """Decode the scan of a flat-block baseline JPEG, as a decoder would: the
    tables come from the file's own DHT segments, and each block must be a DC
    value and an end of block."""
    found, scan = jpeg_segments(content)
    (frame,) = found[0xC0]
    height, width = struct.unpack(">HH", frame[1:5])
    tables = {}
    for body in found[0xC4]:
        counts, symbols = body[1:17], body[17:]
        codes = huffman_codes(tuple(counts), tuple(symbols))
        tables[body[0]] = {
            (length, code): symbol for symbol, (code, length) in codes.items()
        }
    bits = "".join(f"{byte:08b}" for byte in scan.replace(b"\xff\x00", b"\xff"))

    def read_symbol(table: dict, at: int) -> tuple[int, int]:
        for length in range(1, 17):
            key = (length, int(bits[at : at + length], 2))
            if key in table:
                return table[key], at + length
        raise AssertionError("no code")

    blocks_across, blocks_down = width // 8, height // 8
    at, previous, rows = 0, 0, []
    for _ in range(blocks_down):
        row = []
        for _ in range(blocks_across):
            size, at = read_symbol(tables[0x00], at)
            diff = 0
            if size:
                value = int(bits[at : at + size], 2)
                diff = value if value >> (size - 1) else value - (1 << size) + 1
                at += size
            previous += diff
            row.append(previous + 128)
            end, at = read_symbol(tables[0x10], at)
            assert end == 0, "an AC coefficient where only an end of block is made"
        rows.append(row)
    assert set(bits[at:]) <= {"1"} and len(bits) - at < 8, "padding is one bits"
    return rows


def test_the_jpeg_decodes_to_the_banner(committed):
    # Arrange
    content = committed[samples.JPEG_NAME]

    # Act
    found, _ = jpeg_segments(content)
    levels = decode_levels(content)

    # Assert
    assert samples.METADATA_TEXT.encode() in found[0xFE]
    assert levels == samples.banner_pixels()
    assert b"JFIF\x00" in found[0xE0][0]


# -- outside the golden set --------------------------------------------------------
def test_the_golden_manifest_does_not_list_the_folder(synthetic_dir: Path):
    # Arrange
    golden = json.loads((synthetic_dir / "manifest.json").read_text(encoding="utf-8"))

    # Assert
    assert not [path for path in golden["files"] if path.startswith(FOLDER)]


def test_the_evaluation_gate_still_accepts_the_golden_set(synthetic_dir: Path):
    # Act: the gate's own reading of the golden set refuses an unlisted file
    golden_set = golden_set_of(synthetic_dir / "manifest.json")

    # Assert
    assert golden_set.workload == "claims-triage"
    assert not [path for path in golden_set.files if FOLDER in path]


# -- git must keep the bytes -------------------------------------------------------
@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
@pytest.mark.parametrize("name", [*sorted(GOOD), samples.REFUSED_NAME, "manifest.json"])
def test_git_stores_each_file_byte_for_byte(folder: Path, name: str):
    # Arrange: the data folder's attribute asks for LF line endings, and a PNG's
    # signature holds a CR LF; the hash git would store must be the file's own
    path = folder / name

    # Act
    def hash_object(*extra: str) -> str:
        return subprocess.run(
            ["git", "hash-object", *extra, str(path)],
            check=True,
            capture_output=True,
            text=True,
            cwd=path.parent,
        ).stdout.strip()

    # Assert
    assert hash_object("--path", str(path)) == hash_object("--no-filters")

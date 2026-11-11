"""Repack a seekable Zstandard .blend without changing its Blender payload.

Offline maintenance only; requires the optional development zstandard package.
The header and final metadata frames remain separate for native library seeks.
"""

import argparse
import hashlib
import io
import json
import struct
from pathlib import Path

import zstandard

FRAME_MAGIC = 0x184D2A5E
SEEK_MAGIC = 0x8F92EAB1
MAX_PAYLOAD = 512 * 1024 * 1024


def frames(data: bytes) -> list[bytes]:
    if len(data) < 17 or data[:4] != b"\x28\xb5\x2f\xfd":
        raise ValueError("Expected a native Zstandard .blend container")
    count, flags, magic = struct.unpack_from("<IBI", data, len(data) - 9)
    stride = 12 if flags & 128 else 8
    start = len(data) - 9 - count * stride
    if magic != SEEK_MAGIC or not count or flags & 0x60 or start < 8:
        raise ValueError("Invalid native seek table")
    frame_magic, size = struct.unpack_from("<II", data, start - 8)
    if frame_magic != FRAME_MAGIC or size != count * stride + 9:
        raise ValueError("Invalid seek-table frame")
    entries = [struct.unpack_from("<II", data, start + index * stride) for index in range(count)]
    if sum(c for c, _ in entries) != start - 8 or sum(u for _, u in entries) > MAX_PAYLOAD:
        raise ValueError("Seek table does not describe the complete bounded payload")
    decoded, offset = [], 0
    decoder = zstandard.ZstdDecompressor()
    for compressed, uncompressed in entries:
        raw = decoder.decompress(data[offset:offset + compressed], max_output_size=uncompressed)
        if len(raw) != uncompressed:
            raise ValueError("Frame size does not match its seek entry")
        decoded.append(raw)
        offset += compressed
    if not decoded[0].startswith(b"BLENDER"):
        raise ValueError("The decompressed payload is not a .blend file")
    return decoded


def repack(data: bytes, *, level: int = 19, block_bytes: int = 8 * 1024 * 1024) -> tuple[bytes, dict]:
    if not 1 <= level <= 19 or not 1024 * 1024 <= block_bytes <= 8 * 1024 * 1024:
        raise ValueError("Use levels 1..19 and blocks between 1 and 8 MiB")
    original = frames(data)
    chunks = original
    if len(original) > 2:
        middle = b"".join(original[1:-1])
        chunks = [original[0], *(middle[i:i + block_bytes] for i in range(0, len(middle), block_bytes)), original[-1]]
    encoder = zstandard.ZstdCompressor(level=level, threads=0, write_checksum=True)
    packed = [encoder.compress(chunk) for chunk in chunks]
    entries = b"".join(struct.pack("<II", len(compressed), len(raw)) for compressed, raw in zip(packed, chunks, strict=True))
    result = (b"".join(packed) + struct.pack("<II", FRAME_MAGIC, len(entries) + 9)
              + entries + struct.pack("<IBI", len(chunks), 0, SEEK_MAGIC))
    payload = b"".join(original)
    if b"".join(frames(result)) != payload:
        raise ValueError("Payload changed during compression")
    with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(result), read_across_frames=True) as reader:
        if reader.read() != payload:
            raise ValueError("Independent streaming decode differs from the source")
    proof = {"source_sha256": hashlib.sha256(data).hexdigest(),
             "container_sha256": hashlib.sha256(result).hexdigest(),
             "payload_sha256": hashlib.sha256(payload).hexdigest(), "payload_bytes": len(payload),
             "source_bytes": len(data), "container_bytes": len(result),
             "source_frames": len(original), "frames": len(chunks), "level": level,
             "block_bytes": block_bytes, "first_and_last_frames_preserved": True}
    return result, proof


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("Keep the source; choose a new output file")
    result, proof = repack(args.source.read_bytes())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(result)
    print(json.dumps(proof))


if __name__ == "__main__":
    main()

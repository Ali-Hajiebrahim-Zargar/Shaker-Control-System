"""Launch the verified Shaker CSV Viewer 1.7 payload stored beside this file.

This small launcher keeps the repository transfer reliable while preserving the
complete, SHA-256-verified viewer implementation in ``_viewer_payload``.
Run it exactly like the original script:

    python shaker_csv_viewer.py
"""
from __future__ import annotations

from pathlib import Path
import base64
import hashlib
import zlib

EXPECTED_SHA256 = "f44bc61b6c5c8ff2f12664e8b71e2b3a17fc4b2d9717571b5710209cc03e3fc0"
EXPECTED_SIZE = 77_403


def _load_source() -> bytes:
    payload_dir = Path(__file__).resolve().parent / "_viewer_payload"
    parts: list[str] = []

    for index in range(3):
        path = payload_dir / f"part-{index:02d}.b64"
        if not path.is_file():
            raise RuntimeError(f"Missing viewer payload file: {path}")

        text = path.read_text(encoding="utf-8").strip()

        # The connector staging transport appended two characters to part 1.
        # Remove them before decoding; the final source is then verified below.
        if index == 1 and len(text) == 12_002 and text.endswith("Y5"):
            text = text[:-2]

        parts.append(text)

    compressed = base64.b64decode("".join(parts), validate=True)
    source = zlib.decompress(compressed)
    digest = hashlib.sha256(source).hexdigest()

    if len(source) != EXPECTED_SIZE or digest != EXPECTED_SHA256:
        raise RuntimeError(
            "Shaker CSV Viewer payload verification failed. "
            f"Expected {EXPECTED_SIZE} bytes / {EXPECTED_SHA256}; "
            f"got {len(source)} bytes / {digest}."
        )

    return source


def main() -> None:
    source = _load_source()
    filename = str(Path(__file__).resolve())
    namespace = {
        "__name__": "__main__",
        "__file__": filename,
        "__package__": None,
        "__cached__": None,
    }
    exec(compile(source, filename, "exec"), namespace, namespace)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Refresh a verified copy of the pinned CPU VAD model (the package ships one
in hermes_livekit/models). Never downloads during capture."""

import argparse
import hashlib
import os
from pathlib import Path
import tempfile
from urllib.request import urlopen

from hermes_livekit.speech_detector import (
    SILERO_MODEL_BYTES, SILERO_MODEL_URL, SILERO_SHA256, SILERO_LICENSE_SHA256, SILERO_LICENSE_URL,
)


def install_verified(destination: Path, url: str, size: int, digest: str) -> None:
    if destination.exists():
        if (destination.stat().st_size == size
                and hashlib.sha256(destination.read_bytes()).hexdigest() == digest):
            return
        raise ValueError("Destination exists with different content; choose a new path")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with urlopen(url, timeout=30) as response:
            data = response.read(size + 1)
        if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("Download failed size/checksum verification")
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".vad-", delete=False) as output:
            temporary = Path(output.name)
            output.write(data)
        # Atomic publish without overwriting a file created by another installer.
        os.link(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def prepare(destination: Path) -> None:
    install_verified(destination.with_suffix(".LICENSE"), SILERO_LICENSE_URL, 1075, SILERO_LICENSE_SHA256)
    install_verified(destination, SILERO_MODEL_URL, SILERO_MODEL_BYTES, SILERO_SHA256)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    prepare(args.destination.expanduser())
    print(f"Verified Silero v6.2 model: {args.destination}")

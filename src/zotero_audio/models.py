from __future__ import annotations

import hashlib
import os
import tempfile
import urllib.request
from pathlib import Path


KOKORO_FILES = {
    "kokoro-v1.0.int8.onnx": {
        "url": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.int8.onnx",
        "sha256": "6e742170d309016e5891a994e1ce1559c702a2ccd0075e67ef7157974f6406cb",
    },
    "voices-v1.0.bin": {
        "url": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin",
        "sha256": "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d",
    },
}


def default_model_dir() -> Path:
    return Path.home() / "Library" / "Caches" / "zotero-audio" / "models"


def _download_verified(url: str, sha256: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(url) as response, os.fdopen(descriptor, "wb") as stream:
            while chunk := response.read(1024 * 1024):
                stream.write(chunk)
                digest.update(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        if digest.hexdigest() != sha256:
            raise RuntimeError(
                f"Checksum mismatch for {destination.name}: expected {sha256}, got {digest.hexdigest()}"
            )
        os.replace(temporary, destination)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def install_kokoro_models(model_dir: Path, *, force: bool = False) -> list[Path]:
    installed: list[Path] = []
    for filename, record in KOKORO_FILES.items():
        destination = model_dir.expanduser().resolve() / filename
        if destination.exists() and not force:
            digest = hashlib.sha256(destination.read_bytes()).hexdigest()
            if digest == record["sha256"]:
                installed.append(destination)
                continue
        _download_verified(record["url"], record["sha256"], destination)
        installed.append(destination)
    return installed

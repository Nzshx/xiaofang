from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any, Iterable

import ezdxf


# A pipeline run used to parse the same large DXF independently in sheet
# discovery, obstacle extraction, recognition, registration and migration.
# All consumers of this cache are read-only.  Stages which intentionally edit a
# document (review/fused/final DXF writers) continue to call ezdxf.readfile()
# directly and therefore never mutate the shared document.
_DOCUMENTS: dict[tuple[str, int, int], ezdxf.document.Drawing] = {}
_DOCUMENT_LOCK = threading.RLock()
_SHA256: dict[tuple[str, int, int], str] = {}


def _file_identity(path: Path | str) -> tuple[str, int, int]:
    resolved = Path(path).expanduser().resolve()
    stat = resolved.stat()
    return str(resolved), int(stat.st_size), int(stat.st_mtime_ns)


def readfile_once(path: Path | str) -> ezdxf.document.Drawing:
    """Return one shared, read-only ezdxf document per physical file/version."""
    key = _file_identity(path)
    with _DOCUMENT_LOCK:
        document = _DOCUMENTS.get(key)
        if document is None:
            document = ezdxf.readfile(key[0])
            _DOCUMENTS[key] = document
        return document


def clear_document_cache() -> None:
    """Release parsed CAD documents after a pipeline run or in tests."""
    with _DOCUMENT_LOCK:
        _DOCUMENTS.clear()


def file_sha256(path: Path | str) -> str:
    key = _file_identity(path)
    with _DOCUMENT_LOCK:
        cached = _SHA256.get(key)
    if cached:
        return cached
    digest = hashlib.sha256()
    with Path(key[0]).open("rb") as stream:
        while True:
            chunk = stream.read(4 * 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    value = digest.hexdigest()
    with _DOCUMENT_LOCK:
        _SHA256[key] = value
    return value


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_cache_key(
    namespace: str,
    *,
    input_files: Iterable[Path | str] = (),
    rule_files: Iterable[Path | str] = (),
    options: Any = None,
) -> str:
    """Build a content-addressed key that invalidates on data or rule changes."""
    digest = hashlib.sha256()
    digest.update(namespace.encode("utf-8"))
    for label, paths in (("input", input_files), ("rule", rule_files)):
        for raw_path in sorted((Path(value).expanduser().resolve() for value in paths), key=str):
            digest.update(label.encode("ascii"))
            digest.update(str(raw_path).encode("utf-8"))
            if raw_path.is_file():
                digest.update(file_sha256(raw_path).encode("ascii"))
            else:
                digest.update(b"MISSING")
    digest.update(stable_json(options).encode("utf-8"))
    return digest.hexdigest()[:24]


def json_cache_key(namespace: str, payload: Any, *, rule_files: Iterable[Path | str] = ()) -> str:
    digest = hashlib.sha256()
    digest.update(namespace.encode("utf-8"))
    digest.update(stable_json(payload).encode("utf-8"))
    for path in sorted((Path(value).expanduser().resolve() for value in rule_files), key=str):
        digest.update(str(path).encode("utf-8"))
        digest.update(file_sha256(path).encode("ascii") if path.is_file() else b"MISSING")
    return digest.hexdigest()[:24]


__all__ = [
    "clear_document_cache",
    "content_cache_key",
    "file_sha256",
    "json_cache_key",
    "readfile_once",
    "stable_json",
]

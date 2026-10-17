"""Streaming COCO reader with required-structure validation.

The LIVECell annotation files are large (up to ~551 MB). This module parses
them incrementally from disk in fixed-size chunks and yields one record at a
time, so memory use stays flat regardless of file size. Only the Python
standard library is used.

Required structure (anything else raises :class:`AuditError` naming the file
and location):

- top level is a JSON object containing ``images`` and ``annotations`` arrays;
- every image record is an object with integer ``id``, string ``file_name``,
  and integer ``width`` / ``height``;
- every annotation record is an object with integer ``id`` and integer
  ``image_id``.
"""

from __future__ import annotations

import json
from typing import Any, BinaryIO, Dict, Iterator, Tuple

from .errors import AuditError

_CHUNK_SIZE = 1 << 20  # 1 MiB text chunks
_COMPACT_AFTER = 1 << 22  # drop consumed buffer prefix past 4 MiB


class _ChunkReader:
    """Incremental text reader that parses one JSON value at a time."""

    def __init__(self, handle: BinaryIO, path: str, chunk_size: int = _CHUNK_SIZE):
        self._handle = handle
        self._path = path
        self._chunk_size = chunk_size
        self._buf = ""
        self._pos = 0
        self._eof = False
        self._decoder = json.JSONDecoder()

    def _fill(self) -> bool:
        chunk = self._handle.read(self._chunk_size)
        if not chunk:
            self._eof = True
            return False
        self._buf += chunk.decode("utf-8")
        return True

    def _compact(self) -> None:
        if self._pos >= _COMPACT_AFTER:
            self._buf = self._buf[self._pos :]
            self._pos = 0

    def skip_whitespace(self) -> bool:
        """Skip whitespace. Returns False only at end of input."""
        while True:
            while self._pos < len(self._buf) and self._buf[self._pos] in " \t\n\r":
                self._pos += 1
            if self._pos < len(self._buf):
                return True
            if self._eof:
                return False
            self._fill()

    def peek(self, where: str) -> str:
        if not self.skip_whitespace():
            raise AuditError(f"{self._path}: unexpected end of file ({where})")
        return self._buf[self._pos]

    def expect(self, char: str, where: str) -> None:
        actual = self.peek(where)
        if actual != char:
            raise AuditError(
                f"{self._path}: expected {char!r} {where}, found {actual!r}"
            )
        self._pos += 1

    def read_value(self, where: str) -> Any:
        """Parse one JSON value starting at the current position."""
        self._compact()
        start = self._pos
        last_error: json.JSONDecodeError | None = None
        while True:
            self.skip_whitespace()
            try:
                value, end = self._decoder.raw_decode(self._buf, self._pos)
                self._pos = end
                return value
            except json.JSONDecodeError as exc:
                last_error = exc
                if self._eof:
                    raise AuditError(
                        f"{self._path}: malformed JSON {where}: {exc.msg}"
                    ) from exc
                self._pos = start
                self._fill()


def _check_image(path: str, split: str, index: int, record: Any) -> Dict[str, Any]:
    where = f"{split} images[{index}]"
    if not isinstance(record, dict):
        raise AuditError(f"{path}: {where} must be an object")
    for field in ("id", "file_name", "width", "height"):
        if field not in record:
            raise AuditError(f"{path}: {where} is missing required field {field!r}")
    if isinstance(record["id"], bool) or not isinstance(record["id"], int):
        raise AuditError(f"{path}: {where} field 'id' must be an integer")
    if not isinstance(record["file_name"], str):
        raise AuditError(f"{path}: {where} field 'file_name' must be a string")
    for field in ("width", "height"):
        value = record[field]
        if isinstance(value, bool) or not isinstance(value, int):
            raise AuditError(f"{path}: {where} field {field!r} must be an integer")
    return record


def _check_annotation(path: str, split: str, index: int, record: Any) -> Dict[str, Any]:
    where = f"{split} annotations[{index}]"
    if not isinstance(record, dict):
        raise AuditError(f"{path}: {where} must be an object")
    for field in ("id", "image_id"):
        if field not in record:
            raise AuditError(f"{path}: {where} is missing required field {field!r}")
        value = record[field]
        if isinstance(value, bool) or not isinstance(value, int):
            raise AuditError(f"{path}: {where} field {field!r} must be an integer")
    return record


def iter_split(
    path: str, split: str, chunk_size: int = _CHUNK_SIZE
) -> Iterator[Tuple[str, int, Dict[str, Any]]]:
    """Yield ``(array, index, record)`` for ``images`` then ``annotations``.

    ``array`` is ``"images"`` or ``"annotations"``. Records are validated
    against the required structure as they stream past; malformed input
    raises :class:`AuditError`.
    """
    with open(path, "rb") as handle:
        reader = _ChunkReader(handle, path, chunk_size)
        reader.expect("{", "at the top level")
        seen = set()
        while True:
            if reader.peek(f"in {split} header") == "}":
                reader.expect("}", f"in {split} header")
                break
            key = reader.read_value(f"in {split} header")
            if not isinstance(key, str):
                raise AuditError(f"{path}: {split} header keys must be strings")
            reader.expect(":", f"after {split} key {key!r}")
            if key in ("images", "annotations"):
                if key in seen:
                    raise AuditError(f"{path}: {split} key {key!r} appears twice")
                seen.add(key)
                reader.expect("[", f"to open {split} {key!r}")
                if reader.peek(f"in {split} {key!r}") == "]":
                    reader.expect("]", f"to close empty {split} {key!r}")
                else:
                    index = 0
                    while True:
                        record = reader.read_value(f"in {split} {key} array")
                        if key == "images":
                            record = _check_image(path, split, index, record)
                        else:
                            record = _check_annotation(path, split, index, record)
                        yield key, index, record
                        index += 1
                        char = reader.peek(f"in {split} {key} array")
                        if char == ",":
                            reader.expect(",", f"in {split} {key} array")
                            continue
                        if char == "]":
                            reader.expect("]", f"to close {split} {key!r}")
                            break
                        raise AuditError(
                            f"{path}: expected ',' or ']' in {split} {key!r}, "
                            f"found {char!r}"
                        )
            else:
                reader.read_value(f"for {split} key {key!r}")
            char = reader.peek(f"in {split} header")
            if char == ",":
                reader.expect(",", f"in {split} header")
                continue
            if char == "}":
                reader.expect("}", f"in {split} header")
                break
            raise AuditError(
                f"{path}: expected ',' or '}}' in {split} header, found {char!r}"
            )
        for required in ("images", "annotations"):
            if required not in seen:
                raise AuditError(
                    f"{path}: {split} is missing required top-level key "
                    f"{required!r}"
                )
        if reader.skip_whitespace():
            raise AuditError(f"{path}: {split} has trailing content after the object")

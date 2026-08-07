"""Small, dependency-light helpers for Starlette multipart forms."""

from collections.abc import Iterable

from starlette.datastructures import UploadFile


def select_uploads(values: Iterable[object]) -> list[UploadFile]:
    """Select only files parsed by Starlette from multipart form values."""
    return [value for value in values if isinstance(value, UploadFile)]

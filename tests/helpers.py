"""Request payload helpers shared by the DB and integration tests."""

import hashlib

PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def pdf_bytes(tag: str = "a", size: int | None = None) -> bytes:
    """A small, distinct PDF-looking payload (P1 never parses it). `size` pads it to an exact length."""
    data = f"%PDF-1.4\n% pagecite test {tag}\n%%EOF\n".encode()
    if size is not None:
        data = data.ljust(size, b"0")
    return data


def file_spec(name: str, data: bytes, mime: str = PDF_MIME) -> dict:
    return {"name": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "mime": mime}


def auth(key: str) -> dict[str, str]:
    return {"X-Tenant-Key": key}

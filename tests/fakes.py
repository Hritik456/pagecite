"""In-memory stand-in for S3Storage that enforces the same signed-header rules as S3/MinIO."""

import base64
import hashlib
from dataclasses import dataclass

from apps.api.storage import ObjectInfo

FAKE_HOST = "https://fake-s3.test/"


@dataclass
class _Presigned:
    content_type: str
    checksum_sha256_b64: str


class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.presigned: dict[str, _Presigned] = {}
        self.deleted: list[str] = []

    # --- Storage protocol ---
    def presign_put(self, key: str, content_type: str, checksum_sha256_b64: str) -> str:
        self.presigned[key] = _Presigned(content_type, checksum_sha256_b64)
        return FAKE_HOST + key

    def head(self, key: str) -> ObjectInfo | None:
        data = self.objects.get(key)
        if data is None:
            return None
        return ObjectInfo(size=len(data), checksum_sha256_b64=b64_sha256(data))

    def get(self, key: str) -> bytes:
        return self.objects[key]

    def presign_get(self, key: str, filename: str, content_type: str) -> str:
        return f"{FAKE_HOST}{key}?response-content-type={content_type}&sig=get"

    def delete(self, key: str) -> None:
        self.objects.pop(key, None)
        self.deleted.append(key)

    # --- test helpers ---
    def put_via_url(self, url: str, data: bytes, headers: dict[str, str]) -> int:
        """Mimic a browser PUT to the presigned URL. Returns the HTTP status S3 would give."""
        key = url.removeprefix(FAKE_HOST)
        signed = self.presigned.get(key)
        if signed is None:
            return 403
        if headers.get("Content-Type") != signed.content_type:
            return 403  # SignatureDoesNotMatch
        if headers.get("x-amz-checksum-sha256") != signed.checksum_sha256_b64:
            return 403
        if b64_sha256(data) != signed.checksum_sha256_b64:
            return 400  # XAmzContentChecksumMismatch
        self.objects[key] = data
        return 200

    def put_raw(self, key: str, data: bytes) -> None:
        """Write an object bypassing the checksum (to simulate a storage that did not verify it)."""
        self.objects[key] = data


class FakeLLM:
    """Stands in for OpenAILLM. Records calls; never calls the network.

    json_responses: schema_name -> dict | Exception | callable(user_prompt) -> dict
    answer: the streamed answer (split into word-sized deltas)
    on_first_token: callback run just before the first delta is yielded (e.g. to inspect the DB pool)
    fail_after: raise this exception after that many deltas
    """

    def __init__(self, answer: str = "Answer [1].", json_responses: dict | None = None,
                 on_first_token=None, fail_after: tuple[int, Exception] | None = None):
        self.answer = answer
        self.json_responses = json_responses or {}
        self.on_first_token = on_first_token
        self.fail_after = fail_after
        self.json_calls: list[str] = []
        self.stream_calls = 0
        self.last_system = self.last_user = ""

    def json(self, model, system, user, schema_name, schema, max_tokens, reasoning_effort=None):
        from apps.api.retrieval.llm import Usage

        self.json_calls.append(schema_name)
        response = self.json_responses.get(schema_name)
        if isinstance(response, Exception):
            raise response
        if callable(response):
            response = response(user)
        if response is None:
            response = {"sub_queries": [user]} if schema_name == "decomposition" else {"ids": []}
        return response, Usage(prompt_tokens=len(user.split()), completion_tokens=7)

    def stream(self, model, system, user, max_tokens, reasoning_effort=None):
        from apps.api.retrieval.llm import Usage

        self.stream_calls += 1
        self.last_system, self.last_user = system, user
        pieces = [w + " " for w in self.answer.split(" ")]
        pieces[-1] = pieces[-1].rstrip()
        for i, piece in enumerate(pieces):
            if self.fail_after and i == self.fail_after[0]:
                raise self.fail_after[1]
            if i == 0 and self.on_first_token:
                self.on_first_token()
            yield piece
        yield Usage(prompt_tokens=len(user.split()), completion_tokens=len(pieces))


def b64_sha256(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")

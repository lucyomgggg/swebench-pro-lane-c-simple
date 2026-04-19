from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib import error, request


@dataclass
class TelosBridge:
    mode: str
    base_url: str
    monad_id: str
    timeout_sec: float = 30.0

    def _disabled(self, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"ok": True, "mode": "disabled", "action": action, "payload": payload or {}}

    def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.mode != "live":
            return self._disabled(path, payload)
        raw = json.dumps(payload).encode("utf-8")
        req = request.Request(
            self.base_url.rstrip("/") + path,
            data=raw,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self.timeout_sec) as response:
                text = response.read().decode("utf-8", errors="replace")
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            return {"ok": False, "error": f"HTTP {exc.code}", "detail": detail[:1000]}
        except error.URLError as exc:
            return {"ok": False, "error": str(exc.reason)}
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return {"ok": False, "error": "invalid_json", "body_prefix": text[:1000]}
        if not isinstance(data, dict):
            return {"ok": False, "error": "unexpected_response_shape", "data": data}
        return {"ok": True, "data": data}

    def search(
        self,
        *,
        query: str,
        limit: int,
        kind: str | None = None,
        scope_kind: str | None = None,
        scope_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"monad_id": self.monad_id, "query": query, "limit": limit}
        if kind is not None:
            payload["kind"] = kind
        if scope_kind is not None:
            payload["scope_kind"] = scope_kind
        if scope_id is not None:
            payload["scope_id"] = scope_id
        return self._post_json("/api/v1/search", payload)

    def write(
        self,
        *,
        content: str,
        parent_ids: list[str] | None = None,
        kind: str | None = None,
        scope_kind: str | None = None,
        scope_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "monad_id": self.monad_id,
            "content": content,
            "parent_ids": parent_ids or [],
        }
        if kind is not None:
            payload["kind"] = kind
        if scope_kind is not None:
            payload["scope_kind"] = scope_kind
        if scope_id is not None:
            payload["scope_id"] = scope_id
        if metadata is not None:
            payload["metadata"] = metadata
        return self._post_json("/api/v1/write", payload)


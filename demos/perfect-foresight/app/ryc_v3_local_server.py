"""Local-only server for the RYC V3 Decision Workspace.

It serves files from this directory and exposes one calculation endpoint:
``POST /api/v3/analyse``.  No request leaves 127.0.0.1.  Browser JavaScript
only renders the versioned result contract; all financial calculations run in
``ryc_v3_engine.py``.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import RLock
from typing import Any

ROOT = Path(__file__).resolve().parent
ENGINE_PATH = ROOT / "ryc_v3_engine.py"
_engine_lock = RLock()
_loaded_engine = None
_loaded_digest = None


def current_engine():
    """Load exactly the source bytes identified by the reported digest.

    An edit invalidates the engine cache. A broken edit returns an error;
    it must never silently keep answering with the previous implementation.
    Each request holds its own module reference for a consistent calculation.
    """
    global _loaded_engine, _loaded_digest
    with _engine_lock:
        source = ENGINE_PATH.read_bytes()
        digest = hashlib.sha256(source).hexdigest()
        if _loaded_engine is not None and digest == _loaded_digest:
            return _loaded_engine, digest
        name = "_ryc_served_engine"
        spec = importlib.util.spec_from_file_location(name, ENGINE_PATH)
        module = importlib.util.module_from_spec(spec)
        previous = sys.modules.get(name)
        sys.modules[name] = module
        try:
            exec(compile(source, str(ENGINE_PATH), "exec"), module.__dict__)
        except Exception:
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
            raise
        _loaded_engine, _loaded_digest = module, digest
        return module, digest


class Handler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, format: str, *args: Any) -> None:
        # Keep the hidden local helper quiet; errors still return JSON.
        return

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") == "/api/health":
            try:
                engine, digest = current_engine()
            except Exception:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"status": "engine_unavailable", "error": "Mã tính hiện tại chưa nạp được; không dùng lại mã cũ."})
                return
            self._json(HTTPStatus.OK, {
                "status": "ok",
                "service": "RYC-V3-local-decision-engine",
                "revision": engine.ENGINE_REVISION,
                "engine_sha256": digest,
                "scope": "MODEL_ONLY",
                "network": "localhost only",
            })
            return
        if self.path.rstrip("/") == "/api/v3/self-test":
            engine, digest = current_engine()
            self._json(HTTPStatus.OK, {"status": "ok", "engine_sha256": digest, "self_test": engine.self_test()})
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") != "/api/v3/analyse":
            self._json(HTTPStatus.NOT_FOUND, {"error": "Unknown local API path."})
            return
        try:
            engine, digest = current_engine()
        except Exception:
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Mã tính hiện tại chưa nạp được; không dùng lại mã cũ."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 1_000_000:
                raise engine.ModelInputError("Request body must be non-empty and smaller than 1 MB.")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            result = engine.analyse_payload(payload)
            result["engine_sha256"] = digest
            self._json(HTTPStatus.OK, result)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": f"Invalid JSON: {error}"})
        except engine.ModelInputError as error:
            self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(error), "scope": "MODEL_ONLY"})
        except Exception as error:  # Defensive: no stack trace sent to the browser.
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"Local engine error: {error}"})


def main() -> None:
    parser = argparse.ArgumentParser(description="Local RYC V3 Decision Workspace server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8772, type=int)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"RYC V3 is serving on http://{args.host}:{args.port}/ryc_v3_decision_workspace.html")
    print(f"Perfect Foresight UI: http://{args.host}:{args.port}/ryc_perfect_foresight_workspace.html")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

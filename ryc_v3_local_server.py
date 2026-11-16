"""Local-only server for the RYC V3 Decision Workspace.

It serves files from this directory and exposes versioned calculation adapters.
No request leaves 127.0.0.1. Browser JavaScript renders returned contracts;
financial calculations stay in the Python engine/event ledger.
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
LEDGER_PATH = ROOT / "ryc_investor_ledger.py"
POLICY_PATH = ROOT / "ryc_policy_vertical_slice.py"
BUILDER_PATH = ROOT / "ryc_holder_baseline_builder.py"
PRESENTATION_PATH = ROOT / "ryc_holder_presentation.py"
_engine_lock = RLock()
_ledger_lock = RLock()
_policy_lock = RLock()
_builder_lock = RLock()
_presentation_lock = RLock()
_loaded_engine = None
_loaded_digest = None
_loaded_ledger = None
_loaded_ledger_digest = None
_loaded_policy = None
_loaded_policy_digest = None
_loaded_builder = None
_loaded_builder_digest = None
_loaded_presentation = None
_loaded_presentation_digest = None
ALLOWED_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def validate_bind_host(host: str) -> str:
    """Keep the research server local, matching its public security claim."""
    if host not in ALLOWED_LOOPBACK_HOSTS:
        raise ValueError("RYC V3 chỉ cho phép bind vào localhost trong prototype này.")
    return host


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


def current_ledger():
    """Load ledger plus the exact current engine dependency as one bundle."""
    global _loaded_ledger, _loaded_ledger_digest
    with _ledger_lock:
        engine, engine_digest = current_engine()
        source = LEDGER_PATH.read_bytes()
        digest = hashlib.sha256(source + engine_digest.encode("ascii")).hexdigest()
        if _loaded_ledger is not None and digest == _loaded_ledger_digest:
            return _loaded_ledger, digest
        name = "_ryc_served_ledger"
        spec = importlib.util.spec_from_file_location(name, LEDGER_PATH)
        module = importlib.util.module_from_spec(spec)
        previous_module = sys.modules.get(name)
        previous_engine_alias = sys.modules.get("ryc_v3_engine")
        sys.modules[name] = module
        sys.modules["ryc_v3_engine"] = engine
        try:
            exec(compile(source, str(LEDGER_PATH), "exec"), module.__dict__)
        except Exception:
            if previous_module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous_module
            raise
        finally:
            if previous_engine_alias is None:
                sys.modules.pop("ryc_v3_engine", None)
            else:
                sys.modules["ryc_v3_engine"] = previous_engine_alias
        _loaded_ledger, _loaded_ledger_digest = module, digest
        return module, digest


def current_policy():
    """Load policy core with the exact current ledger and engine dependencies."""
    global _loaded_policy, _loaded_policy_digest
    with _policy_lock:
        engine, _ = current_engine()
        ledger, ledger_digest = current_ledger()
        source = POLICY_PATH.read_bytes()
        digest = hashlib.sha256(source + ledger_digest.encode("ascii")).hexdigest()
        if _loaded_policy is not None and digest == _loaded_policy_digest:
            return _loaded_policy, digest
        name = "_ryc_served_policy"
        spec = importlib.util.spec_from_file_location(name, POLICY_PATH)
        module = importlib.util.module_from_spec(spec)
        previous_module = sys.modules.get(name)
        previous_ledger_alias = sys.modules.get("ryc_investor_ledger")
        previous_engine_alias = sys.modules.get("ryc_v3_engine")
        sys.modules[name] = module
        sys.modules["ryc_investor_ledger"] = ledger
        sys.modules["ryc_v3_engine"] = engine
        try:
            exec(compile(source, str(POLICY_PATH), "exec"), module.__dict__)
        except Exception:
            if previous_module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous_module
            raise
        finally:
            if previous_ledger_alias is None:
                sys.modules.pop("ryc_investor_ledger", None)
            else:
                sys.modules["ryc_investor_ledger"] = previous_ledger_alias
            if previous_engine_alias is None:
                sys.modules.pop("ryc_v3_engine", None)
            else:
                sys.modules["ryc_v3_engine"] = previous_engine_alias
        _loaded_policy, _loaded_policy_digest = module, digest
        return module, digest


def current_holder_builder():
    """Load the holder builder against the exact current policy bundle."""
    global _loaded_builder, _loaded_builder_digest
    with _builder_lock:
        policy, policy_digest = current_policy()
        source = BUILDER_PATH.read_bytes()
        digest = hashlib.sha256(source + policy_digest.encode("ascii")).hexdigest()
        if _loaded_builder is not None and digest == _loaded_builder_digest:
            return _loaded_builder, digest
        name = "_ryc_served_holder_builder"
        spec = importlib.util.spec_from_file_location(name, BUILDER_PATH)
        module = importlib.util.module_from_spec(spec)
        previous_module = sys.modules.get(name)
        previous_policy_alias = sys.modules.get("ryc_policy_vertical_slice")
        sys.modules[name] = module
        sys.modules["ryc_policy_vertical_slice"] = policy
        try:
            exec(compile(source, str(BUILDER_PATH), "exec"), module.__dict__)
        except Exception:
            if previous_module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous_module
            raise
        finally:
            if previous_policy_alias is None:
                sys.modules.pop("ryc_policy_vertical_slice", None)
            else:
                sys.modules["ryc_policy_vertical_slice"] = previous_policy_alias
        _loaded_builder, _loaded_builder_digest = module, digest
        return module, digest


def current_holder_presentation():
    """Load the read-only presenter against the exact current holder builder."""
    global _loaded_presentation, _loaded_presentation_digest
    with _presentation_lock:
        builder, builder_digest = current_holder_builder()
        source = PRESENTATION_PATH.read_bytes()
        digest = hashlib.sha256(source + builder_digest.encode("ascii")).hexdigest()
        if (_loaded_presentation is not None
                and digest == _loaded_presentation_digest):
            return _loaded_presentation, digest
        name = "_ryc_served_holder_presentation"
        spec = importlib.util.spec_from_file_location(name, PRESENTATION_PATH)
        module = importlib.util.module_from_spec(spec)
        previous_module = sys.modules.get(name)
        previous_builder_alias = sys.modules.get("ryc_holder_baseline_builder")
        sys.modules[name] = module
        sys.modules["ryc_holder_baseline_builder"] = builder
        try:
            exec(compile(source, str(PRESENTATION_PATH), "exec"), module.__dict__)
        except Exception:
            if previous_module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous_module
            raise
        finally:
            if previous_builder_alias is None:
                sys.modules.pop("ryc_holder_baseline_builder", None)
            else:
                sys.modules["ryc_holder_baseline_builder"] = previous_builder_alias
        _loaded_presentation, _loaded_presentation_digest = module, digest
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
        route = self.path.rstrip("/")
        if route in {"/api/v3/policy-evaluate", "/api/v3/policy-replay",
                     "/api/v3/holder-baseline", "/api/v3/holder-baseline-replay",
                     "/api/v3/holder-presentation"}:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "INVALID_CONTENT_LENGTH"})
                return
            if length <= 0:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "REQUEST_BODY_SIZE"})
                return
            if length > 1_000_000:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(65_536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                           {"error": "REQUEST_BODY_TOO_LARGE"})
                return
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                self._json(HTTPStatus.BAD_REQUEST,
                           {"error": "INVALID_JSON", "detail": str(error)})
                return
            if route.endswith("policy-evaluate"):
                expected_fields, error_code = {"manifest"}, "POLICY_EVALUATE_FIELDS"
            elif route.endswith("policy-replay"):
                expected_fields, error_code = {"export"}, "POLICY_REPLAY_FIELDS"
            elif route.endswith("holder-baseline"):
                expected_fields, error_code = {"request"}, "HOLDER_BASELINE_FIELDS"
            elif route.endswith("holder-baseline-replay"):
                expected_fields, error_code = {"export"}, "HOLDER_BASELINE_REPLAY_FIELDS"
            else:
                expected_fields, error_code = {"export"}, "HOLDER_PRESENTATION_FIELDS"
            if not isinstance(payload, dict) or set(payload) != expected_fields:
                self._json(HTTPStatus.UNPROCESSABLE_ENTITY,
                           {"error": error_code, "scope": "SYNTHETIC_ONLY"})
                return
            if route.endswith("holder-baseline") or route.endswith("holder-baseline-replay"):
                try:
                    builder_core, _ = current_holder_builder()
                except Exception:
                    self._json(HTTPStatus.SERVICE_UNAVAILABLE,
                               {"error": "Holder builder unavailable; no cached result returned."})
                    return
                try:
                    if route.endswith("holder-baseline-replay"):
                        result = builder_core.replay_holder_baseline_export(payload["export"])
                    else:
                        result = builder_core.create_holder_baseline_export(payload["request"])
                    self._json(HTTPStatus.OK, result)
                except builder_core.HolderBaselineBuilderError as error:
                    self._json(HTTPStatus.UNPROCESSABLE_ENTITY,
                               {"error": str(error), "scope": "SYNTHETIC_ONLY"})
                except Exception:
                    self._json(HTTPStatus.INTERNAL_SERVER_ERROR,
                               {"error": "Local holder builder error; no partial result returned."})
                return
            if route.endswith("holder-presentation"):
                try:
                    builder_core, _ = current_holder_builder()
                    presentation_core, _ = current_holder_presentation()
                except Exception:
                    self._json(HTTPStatus.SERVICE_UNAVAILABLE,
                               {"error": "Holder presentation unavailable; no cached result returned."})
                    return
                try:
                    result = presentation_core.present_holder_baseline(payload["export"])
                    self._json(HTTPStatus.OK, result)
                except (presentation_core.HolderPresentationError,
                        builder_core.HolderBaselineBuilderError) as error:
                    self._json(HTTPStatus.UNPROCESSABLE_ENTITY,
                               {"error": str(error), "scope": "SYNTHETIC_ONLY"})
                except Exception:
                    self._json(HTTPStatus.INTERNAL_SERVER_ERROR,
                               {"error": "Local presentation error; no partial result returned."})
                return
            try:
                policy_core, _ = current_policy()
            except Exception:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE,
                           {"error": "Decision core unavailable; no cached result returned."})
                return
            try:
                if route.endswith("policy-evaluate"):
                    result = policy_core.create_decision_run_export(payload["manifest"])
                else:
                    result = policy_core.replay_decision_run_export(payload["export"])
                self._json(HTTPStatus.OK, result)
            except policy_core.PolicySliceError as error:
                self._json(HTTPStatus.UNPROCESSABLE_ENTITY,
                           {"error": str(error), "scope": "SYNTHETIC_ONLY"})
            except Exception:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR,
                           {"error": "Local policy error; no partial result returned."})
            return
        if route == "/api/v3/ledger-assess":
            try:
                ledger, digest = current_ledger()
            except Exception:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE,
                           {"error": "Sổ dòng tiền hiện tại chưa nạp được; không dùng lại mã cũ."})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 1_000_000:
                    raise ledger.LedgerError("REQUEST_BODY_SIZE")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(payload, dict) or set(payload) != {"case", "mandate"}:
                    raise ledger.LedgerError("ASSESSMENT_FIELDS")
                result = ledger.assess_terminal_cash(payload["case"], payload["mandate"])
                result.update(analysis_version="RYC-P2-TERMINAL-ASSESSMENT-1",
                              ledger_bundle_sha256=digest)
                self._json(HTTPStatus.OK, result)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                self._json(HTTPStatus.BAD_REQUEST, {"error": f"Invalid JSON: {error}"})
            except ledger.LedgerError as error:
                self._json(HTTPStatus.UNPROCESSABLE_ENTITY,
                           {"error": str(error), "scope": "SYNTHETIC_ONLY"})
            except Exception:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR,
                           {"error": "Local ledger error; no partial result returned."})
            return
        if route != "/api/v3/analyse":
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
    try:
        host = validate_bind_host(args.host)
    except ValueError as error:
        parser.error(str(error))
    server = ThreadingHTTPServer((host, args.port), Handler)
    print(f"RYC holder workspace is serving on http://{host}:{args.port}/ryc_holder_decision_workspace.html")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

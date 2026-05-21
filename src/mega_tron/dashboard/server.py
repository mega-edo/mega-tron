"""HTTP server for ``mega-tron dashboard``.

Single-tenant, loopback-bound by default. ``ThreadingHTTPServer`` so a
slow render in one tab doesn't block another. No auth — the only
defence is the bind address (override via CLI ``--host`` at your own
risk).
"""
from __future__ import annotations

import json
import logging
import mimetypes
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from typing import Any
from urllib.parse import parse_qs, urlsplit

from mega_tron.dashboard import api

_LOG = logging.getLogger(__name__)


# Static asset root inside the installed package (force-included via
# pyproject's hatch build settings).
_STATIC_ROOT = files("mega_tron.dashboard").joinpath("static")


_VERDICT_ID_RE = re.compile(r"^/api/verdict/(\d+)$")


class DashboardHandler(BaseHTTPRequestHandler):
    """JSON GETs + static-file GETs. PATCH/DELETE land in T4."""

    # Quieter logs — BaseHTTPRequestHandler defaults to stderr per
    # request which makes the CLI noisy.
    def log_message(self, format: str, *args: Any) -> None:  # noqa: D401, A002
        _LOG.info("%s - %s", self.address_string(), format % args)

    # ----- GET ----- #

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        split = urlsplit(self.path)
        path = split.path
        query = parse_qs(split.query)

        if path == "/" or path == "/index.html":
            self._send_static("index.html")
            return
        if path.startswith("/static/"):
            self._send_static(path[len("/static/"):])
            return
        if path.startswith("/api/"):
            self._dispatch_api_get(path, query)
            return
        self._send_status(HTTPStatus.NOT_FOUND, body=b"not found")

    def _dispatch_api_get(self, path: str, query: dict[str, list[str]]) -> None:
        try:
            days = _qint(query, "days", 30)
            host = _qstr(query, "host")
            if path == "/api/overview":
                payload = api.overview(days=days)
                self._send_json(payload)
                return
            if path == "/api/skills":
                payload = api.skills(host=host, days=days)
                self._send_json(payload)
                return
            if path == "/api/skills-by-name":
                payload = api.skills_by_name(days=days)
                self._send_json(payload)
                return
            if path == "/api/activity":
                skill = _qstr(query, "skill")
                payload = api.activity(days=days, skill=skill, host=host)
                self._send_json(payload)
                return
            if path == "/api/verdicts":
                payload = api.verdicts(
                    limit=_qint(query, "limit", 50),
                    host=host,
                    skill=_qstr(query, "skill"),
                    days=days if "days" in query else None,
                )
                self._send_json(payload)
                return
            if path == "/api/verdicts/search":
                q = _qstr(query, "q") or ""
                payload = api.verdict_search(
                    q=q,
                    host=host,
                    days=days if "days" in query else None,
                    limit=_qint(query, "limit", 50),
                )
                self._send_json(payload)
                return
            if path.startswith("/api/skill/"):
                name = path[len("/api/skill/"):]
                if not name:
                    self._send_status(HTTPStatus.BAD_REQUEST, body=b"missing skill name")
                    return
                payload = api.skill_detail(name)
                if payload is None:
                    self._send_status(HTTPStatus.NOT_FOUND, body=b"skill not found")
                    return
                self._send_json(payload)
                return
        except Exception as exc:  # noqa: BLE001
            _LOG.exception("api GET %s failed", path)
            self._send_status(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                body=json.dumps({"error": str(exc)}).encode("utf-8"),
                content_type="application/json",
            )
            return
        self._send_status(HTTPStatus.NOT_FOUND, body=b"unknown api route")

    # ----- POST (client error logging only) ----- #

    def do_POST(self) -> None:  # noqa: N802
        """POST routes:

        * ``/api/debug/log`` — accept a JSON payload from the
          browser's window.onerror / unhandledrejection handlers and
          print it to the server log. Loopback-only debug aid.
        * ``/api/verdicts/bulk-delete`` — delete every verdict
          matching a ``(skill_name, host, reason)`` tuple. Drives
          the dashboard's bulk-cleanup drawer.

        Both routes require a JSON body. Anything else is 404.
        """
        path = urlsplit(self.path).path

        if path == "/api/debug/log":
            body = self._read_json_body()
            if body is None:
                return
            try:
                line = json.dumps(body, default=str)
            except Exception as exc:  # noqa: BLE001
                line = f"<unserialisable: {exc}>"
            import sys as _sys
            print(
                f"[mega-tron dashboard][client] {line}",
                file=_sys.stderr,
                flush=True,
            )
            self._send_status(HTTPStatus.NO_CONTENT)
            return

        if path == "/api/verdicts/bulk-delete":
            body = self._read_json_body()
            if body is None:
                return
            try:
                payload = api.bulk_delete_verdicts(body)
            except ValueError as exc:
                self._send_status(
                    HTTPStatus.BAD_REQUEST,
                    body=json.dumps({"error": str(exc)}).encode("utf-8"),
                    content_type="application/json",
                )
                return
            except Exception as exc:  # noqa: BLE001
                _LOG.exception("POST /api/verdicts/bulk-delete failed")
                self._send_status(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    body=json.dumps({"error": str(exc)}).encode("utf-8"),
                    content_type="application/json",
                )
                return
            self._send_json(payload)
            return

        if path == "/api/verdict":
            body = self._read_json_body()
            if body is None:
                return
            try:
                payload = api.add_verdict_endpoint(body)
            except ValueError as exc:
                self._send_status(
                    HTTPStatus.BAD_REQUEST,
                    body=json.dumps({"error": str(exc)}).encode("utf-8"),
                    content_type="application/json",
                )
                return
            except Exception as exc:  # noqa: BLE001
                _LOG.exception("POST /api/verdict failed")
                self._send_status(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    body=json.dumps({"error": str(exc)}).encode("utf-8"),
                    content_type="application/json",
                )
                return
            self._send_json(payload)
            return

        if path == "/api/open-folder":
            body = self._read_json_body()
            if body is None:
                return
            try:
                payload = api.open_folder(body)
            except ValueError as exc:
                self._send_status(
                    HTTPStatus.BAD_REQUEST,
                    body=json.dumps({"error": str(exc)}).encode("utf-8"),
                    content_type="application/json",
                )
                return
            except PermissionError as exc:
                self._send_status(
                    HTTPStatus.FORBIDDEN,
                    body=json.dumps({"error": str(exc)}).encode("utf-8"),
                    content_type="application/json",
                )
                return
            except Exception as exc:  # noqa: BLE001
                _LOG.exception("POST /api/open-folder failed")
                self._send_status(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    body=json.dumps({"error": str(exc)}).encode("utf-8"),
                    content_type="application/json",
                )
                return
            self._send_json(payload)
            return

        if path.startswith("/api/skill/"):
            # Two write routes under /api/skill/<name>:
            #   POST /api/skill/<name>/archive  -> soft archive
            #   POST /api/skill/<name>/delete   -> hard rm -rf
            # The DELETE method below also wires the hard-delete path
            # so HTTP semantics fans out cleanly.
            rest = path[len("/api/skill/"):]
            if rest.endswith("/archive"):
                name = rest[:-len("/archive")]
                self._dispatch_skill_archive(name)
                return
            if rest.endswith("/delete"):
                name = rest[:-len("/delete")]
                self._dispatch_skill_hard_delete(name)
                return

        self._send_status(HTTPStatus.NOT_FOUND, body=b"unknown post route")

    def _dispatch_skill_archive(self, name: str) -> None:
        try:
            payload = api.archive_skill(name)
        except api.NotFoundError:
            self._send_status(HTTPStatus.NOT_FOUND, body=b"skill not found")
            return
        except Exception as exc:  # noqa: BLE001
            _LOG.exception("archive_skill failed")
            self._send_status(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                body=json.dumps({"error": str(exc)}).encode("utf-8"),
                content_type="application/json",
            )
            return
        self._send_json(payload)

    def _dispatch_skill_hard_delete(self, name: str) -> None:
        try:
            payload = api.hard_delete_skill(name)
        except api.NotFoundError:
            self._send_status(HTTPStatus.NOT_FOUND, body=b"skill not found")
            return
        except PermissionError as exc:
            self._send_status(
                HTTPStatus.FORBIDDEN,
                body=json.dumps({"error": str(exc)}).encode("utf-8"),
                content_type="application/json",
            )
            return
        except Exception as exc:  # noqa: BLE001
            _LOG.exception("hard_delete_skill failed")
            self._send_status(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                body=json.dumps({"error": str(exc)}).encode("utf-8"),
                content_type="application/json",
            )
            return
        self._send_json(payload)

    # ----- PATCH ----- #

    def do_PATCH(self) -> None:  # noqa: N802
        m = _VERDICT_ID_RE.match(urlsplit(self.path).path)
        if not m:
            self._send_status(HTTPStatus.NOT_FOUND, body=b"unknown patch route")
            return
        vid = int(m.group(1))
        body = self._read_json_body()
        if body is None:
            return  # already sent the error
        try:
            payload = api.patch_verdict(vid, body)
        except api.NotFoundError:
            self._send_status(HTTPStatus.NOT_FOUND, body=b"verdict not found")
            return
        except (ValueError, KeyError) as exc:
            self._send_status(
                HTTPStatus.BAD_REQUEST,
                body=json.dumps({"error": str(exc)}).encode("utf-8"),
                content_type="application/json",
            )
            return
        except Exception as exc:  # noqa: BLE001
            _LOG.exception("PATCH /api/verdict/%s failed", vid)
            self._send_status(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                body=json.dumps({"error": str(exc)}).encode("utf-8"),
                content_type="application/json",
            )
            return
        self._send_json(payload)

    # ----- DELETE ----- #

    def do_DELETE(self) -> None:  # noqa: N802
        m = _VERDICT_ID_RE.match(urlsplit(self.path).path)
        if not m:
            self._send_status(HTTPStatus.NOT_FOUND, body=b"unknown delete route")
            return
        vid = int(m.group(1))
        try:
            payload = api.delete_verdict_endpoint(vid)
        except api.NotFoundError:
            self._send_status(HTTPStatus.NOT_FOUND, body=b"verdict not found")
            return
        except Exception as exc:  # noqa: BLE001
            _LOG.exception("DELETE /api/verdict/%s failed", vid)
            self._send_status(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                body=json.dumps({"error": str(exc)}).encode("utf-8"),
                content_type="application/json",
            )
            return
        self._send_json(payload)

    # ----- Helpers ----- #

    def _read_json_body(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_status(HTTPStatus.BAD_REQUEST, body=b"bad content-length")
            return None
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._send_status(
                HTTPStatus.BAD_REQUEST,
                body=json.dumps({"error": f"invalid json: {exc}"}).encode("utf-8"),
                content_type="application/json",
            )
            return None
        if not isinstance(parsed, dict):
            self._send_status(HTTPStatus.BAD_REQUEST, body=b"body must be a JSON object")
            return None
        return parsed

    def _send_json(self, payload: Any) -> None:
        data = json.dumps(payload, default=str).encode("utf-8")
        self._send_status(
            HTTPStatus.OK, body=data, content_type="application/json"
        )

    def _send_status(
        self,
        status: HTTPStatus,
        *,
        body: bytes = b"",
        content_type: str = "text/plain; charset=utf-8",
    ) -> None:
        self.send_response(int(status))
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # Loopback-only: turn off caching so the polling dashboard
        # always sees fresh data even when proxied.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_static(self, relpath: str) -> None:
        # Strict: no .., no leading /, must resolve under _STATIC_ROOT.
        clean = relpath.lstrip("/")
        if ".." in clean.split("/"):
            self._send_status(HTTPStatus.FORBIDDEN, body=b"bad path")
            return
        try:
            target = _STATIC_ROOT.joinpath(clean)
            data = target.read_bytes()
        except FileNotFoundError:
            self._send_status(HTTPStatus.NOT_FOUND, body=b"asset not found")
            return
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("static read failed: %s", exc)
            self._send_status(HTTPStatus.NOT_FOUND, body=b"asset not found")
            return
        ctype, _enc = mimetypes.guess_type(clean)
        self._send_status(
            HTTPStatus.OK,
            body=data,
            content_type=ctype or "application/octet-stream",
        )


def _qstr(q: dict[str, list[str]], key: str) -> str | None:
    v = q.get(key)
    if not v:
        return None
    s = v[0].strip()
    return s or None


def _qint(q: dict[str, list[str]], key: str, default: int) -> int:
    v = q.get(key)
    if not v:
        return default
    try:
        return int(v[0])
    except ValueError:
        return default


def make_server(host: str, port: int) -> ThreadingHTTPServer:
    """Build (but don't start) the dashboard HTTP server."""
    return ThreadingHTTPServer((host, port), DashboardHandler)


def serve(host: str, port: int) -> None:
    """Run the server until SIGINT. Prints the URL on startup."""
    server = make_server(host, port)
    actual_host, actual_port = server.server_address[:2]  # type: ignore[index]
    url = f"http://{actual_host}:{actual_port}/"
    print(f"[mega-tron dashboard] listening at {url}")
    print("[mega-tron dashboard] press Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[mega-tron dashboard] shutting down")
    finally:
        server.shutdown()
        server.server_close()

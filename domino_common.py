"""Shared building blocks for the Domino platform configuration scripts.

* ``DominoCLI``    - argparse base class. Resolves ``--api-key/-k`` and ``--host/-h``,
                     falling back to ``DOMINO_USER_API_KEY`` / ``DOMINO_API_HOST``.
* ``DominoClient`` - ``requests`` based API client. Authenticates with
                     ``Authorization: Bearer <key>`` and, if the platform rejects that
                     (401/403, as legacy user API keys are not accepted as bearer
                     tokens), retries with the deprecated ``X-Domino-Api-Key`` header.

Only the ``requests`` library is used; no Domino SDKs.
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Optional

import requests

ENV_API_KEY = "DOMINO_USER_API_KEY"
ENV_HOST = "DOMINO_API_HOST"
AUTH_PROBE_PATH = "/api/users/v1/self"


class DominoAPIError(RuntimeError):
    """Raised for non-2xx API responses; message includes the response body."""

    def __init__(self, response: requests.Response):
        self.response = response
        body = response.text.strip()
        if len(body) > 1000:
            body = body[:1000] + "...[truncated]"
        super().__init__(
            f"{response.request.method} {response.url} -> HTTP {response.status_code}: {body}"
        )


class _Session(requests.Session):
    """Session that also strips X-Domino-Api-Key on cross-origin redirects.

    ``requests`` drops the Authorization header when a redirect changes host, but it
    forwards custom headers, which would leak the API key to the redirect target.
    """

    def rebuild_auth(self, prepared_request, response):
        super().rebuild_auth(prepared_request, response)
        if "X-Domino-Api-Key" in prepared_request.headers and self.should_strip_auth(
            response.request.url, prepared_request.url
        ):
            del prepared_request.headers["X-Domino-Api-Key"]


class DominoClient:
    """Authenticated session against the Domino platform API."""

    def __init__(self, host: str, api_key: str, timeout: float = 60.0):
        host = host.strip().rstrip("/")
        if "://" not in host:
            host = "https://" + host
        self.host = host
        self.timeout = timeout
        self.session = _Session()
        self.auth_scheme = self._authenticate(api_key)

    # -- authentication -------------------------------------------------------
    def _probe(self) -> requests.Response:
        return self.session.get(self.host + AUTH_PROBE_PATH, timeout=self.timeout)

    def _authenticate(self, token: str) -> str:
        """Pick the auth scheme the platform accepts. Returns its name."""
        self.session.headers.pop("X-Domino-Api-Key", None)
        self.session.headers["Authorization"] = f"Bearer {token}"
        resp = self._probe()
        if resp.ok:
            return "bearer"
        if resp.status_code not in (401, 403):
            raise DominoAPIError(resp)

        # Legacy user API keys are not valid bearer tokens: fall back to the header.
        self.session.headers.pop("Authorization", None)
        self.session.headers["X-Domino-Api-Key"] = token
        resp = self._probe()
        if resp.ok:
            print(
                "note: bearer authentication was rejected; using the deprecated "
                "X-Domino-Api-Key header instead",
                file=sys.stderr,
            )
            return "x-domino-api-key"
        raise SystemExit(
            f"error: authentication to {self.host} failed with both Bearer and "
            f"X-Domino-Api-Key (HTTP {resp.status_code}). Check the API key and host."
        )

    # -- requests -------------------------------------------------------------
    def request(self, method: str, path: str, *, ok_statuses: tuple = (), **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        resp = self.session.request(method, self.host + path, **kwargs)
        if not resp.ok and resp.status_code not in ok_statuses:
            raise DominoAPIError(resp)
        return resp

    def get(self, path: str, **kw: Any) -> requests.Response:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw: Any) -> requests.Response:
        return self.request("POST", path, **kw)

    def put(self, path: str, **kw: Any) -> requests.Response:
        return self.request("PUT", path, **kw)

    def patch(self, path: str, **kw: Any) -> requests.Response:
        return self.request("PATCH", path, **kw)

    def delete(self, path: str, **kw: Any) -> requests.Response:
        return self.request("DELETE", path, **kw)

    def platform_version(self) -> str:
        """Return the platform version string, e.g. '6.3.1' ('' if unavailable)."""
        resp = self.get("/version", ok_statuses=(404,))
        try:
            return str(resp.json().get("version", ""))
        except ValueError:
            return ""


class DominoCLI:
    """Base class for command-line scripts.

    Subclasses override ``description``, ``add_arguments`` and ``run``::

        class MyScript(DominoCLI):
            description = "..."
            def add_arguments(self, parser): parser.add_argument("--foo")
            def run(self, client, args): ...

        if __name__ == "__main__":
            MyScript().main()
    """

    description: str = ""

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:  # pragma: no cover
        """Hook: add script specific arguments."""

    def validate(self, parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
        """Hook: cross-argument validation; call ``parser.error`` on problems."""

    def run(self, client: DominoClient, args: argparse.Namespace) -> int:
        raise NotImplementedError

    # -- argument handling ----------------------------------------------------
    def build_parser(self) -> argparse.ArgumentParser:
        # -h is the short flag for --host, so argparse's own -h/--help is disabled
        # and --help is re-registered without a short form.
        parser = argparse.ArgumentParser(description=self.description, add_help=False)
        parser.add_argument("--help", action="help", help="show this help message and exit")
        parser.add_argument(
            "-k", "--api-key", default=None,
            help=f"Domino API key/token (used only if ${ENV_API_KEY} is not set)",
        )
        parser.add_argument(
            "-h", "--host", default=None,
            help=f"Domino API host URL (used only if ${ENV_HOST} is not set)",
        )
        self.add_arguments(parser)
        return parser

    @staticmethod
    def resolve_credentials(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
        """Environment variables win when set; otherwise the flags are required."""
        args.api_key = os.environ.get(ENV_API_KEY) or args.api_key
        args.host = os.environ.get(ENV_HOST) or args.host
        missing = []
        if not args.api_key:
            missing.append(f"--api-key/-k (or ${ENV_API_KEY})")
        if not args.host:
            missing.append(f"--host/-h (or ${ENV_HOST})")
        if missing:
            parser.error("missing required setting(s): " + ", ".join(missing))

    def main(self, argv: Optional[list] = None) -> None:
        parser = self.build_parser()
        args = parser.parse_args(argv)
        self.resolve_credentials(parser, args)
        self.validate(parser, args)
        try:
            client = DominoClient(args.host, args.api_key)
            code = self.run(client, args)
        except DominoAPIError as exc:
            print(f"error: {exc}", file=sys.stderr)
            code = 1
        except requests.RequestException as exc:
            print(f"error: request failed: {exc}", file=sys.stderr)
            code = 1
        sys.exit(code or 0)

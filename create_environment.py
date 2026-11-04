#!/usr/bin/env python3
"""Create a Domino Compute Environment, or add a revision to an existing one.

Create:  create_environment.py --name NAME --base-image IMAGE --dockerfile Dockerfile
Update:  create_environment.py -e ENVIRONMENT_ID --dockerfile Dockerfile [--base-image IMAGE]
         An update inherits the previous revision's settings; flags given override them
         (use --no-inherit to start clean).

Dump:    create_environment.py -e ENVIRONMENT_ID --dump [--output-dir DIR]
         Writes DIR/ENVIRONMENT_ID/ holding the latest revision's Dockerfile, scripts,
         environment variables, workspace tools and an environment.json of the rest, in the
         formats the flags above read back in.

Import:  create_environment.py --import NEW_NAME --directory DIR
         Creates a new environment from a directory (e.g. one written by --dump) holding a
         Dockerfile (required) and optionally env-vars.json, pluggable-workspace-tools.yaml,
         pre-setup.sh, post-setup.sh, pre-run.sh, post-run.sh and environment.json. When
         environment.json is present its base image, description, visibility, supported
         clusters, VPN setting and tags are used for the new environment (its id, name and
         revision are ignored; NEW_NAME is the name). Explicit flags override the directory.

Optional revision settings (all files are read from disk):
  --env-vars FILE                  JSON or YAML object of {NAME: value}
  --pluggable-workspace-tools/--pwt FILE
                                   YAML in the UI's Pluggable Workspace Tools format
                                   (see examples/pluggable-workspace-tools.example.yaml)
  --pre-setup-script / --post-setup-script / --pre-run-script / --post-run-script FILE
  --supported-clusters dask,ray,...   --skip-cache   --tags key=value,key2=value2

The Dockerfile holds Domino "Dockerfile Instructions" only: do not include ``FROM``
(Domino adds it from the base image). Targets Domino 6.3+: the beta routes are used on
6.3.x and the v2 routes on 6.4 and later.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

import yaml

from domino_common import DominoCLI, DominoClient

TERMINAL_STATUSES = {"succeeded", "failed", "killed"}
CLUSTER_TYPES = ("dask", "mpi", "ray", "slurm", "spark")
SCRIPT_FLAGS = {  # CLI attribute -> API field
    "pre_setup_script": "preSetupScript",
    "post_setup_script": "postSetupScript",
    "pre_run_script": "preRunScript",
    "post_run_script": "postRunScript",
}
# internalPath is required by the API; the UI YAML may omit it for most tools.
DEFAULT_INTERNAL_PATH = "/{{ownerUsername}}/{{projectName}}/{{sessionPathComponent}}/{{runId}}"


def load_structured(path: str, what: str):
    """Load a YAML (or JSON, which is valid YAML) file."""
    try:
        with open(path, encoding="utf-8") as fh:
            return yaml.safe_load(fh)
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot read {what} from {path}: {exc}")


def env_vars_to_api(data) -> list:
    if not isinstance(data, dict) or not data:
        raise ValueError("--env-vars must contain a non-empty object of {NAME: value}")
    out = []
    for key, value in data.items():
        if str(key).upper().startswith("DOMINO_"):
            raise ValueError(f"environment variable '{key}': names starting with DOMINO_ are reserved")
        if isinstance(value, (dict, list)) or value is None:
            raise ValueError(f"environment variable '{key}' must have a scalar value")
        out.append({"key": str(key), "value": value if isinstance(value, str) else
                    ("true" if value is True else "false" if value is False else str(value))})
    return out


def tools_to_api(data) -> list:
    """Convert the UI's Pluggable Workspace Tools YAML to the API's workspaceTools list.

    map key -> name; start -> startScripts; httpProxy -> proxyConfig.
    """
    if not isinstance(data, dict) or not data:
        raise ValueError("--pluggable-workspace-tools must be a non-empty YAML mapping of tool key -> definition")
    tools = []
    for name, tool in data.items():
        if not isinstance(tool, dict):
            raise ValueError(f"tool '{name}' must be a mapping")
        unknown = set(tool) - {"title", "iconUrl", "start", "supportedFileExtensions", "httpProxy"}
        if unknown:
            raise ValueError(f"tool '{name}' has unknown key(s): {', '.join(sorted(unknown))}")
        start = tool.get("start")
        proxy = tool.get("httpProxy")
        if not tool.get("title") or not start or not isinstance(proxy, dict) or "port" not in proxy:
            raise ValueError(f"tool '{name}' needs title, start and httpProxy.port")
        api_tool = {
            "name": str(name),
            "title": tool["title"],
            "startScripts": [start] if isinstance(start, str) else list(start),
            "proxyConfig": {
                "port": int(proxy["port"]),
                "internalPath": proxy.get("internalPath", DEFAULT_INTERNAL_PATH),
                "rewrite": bool(proxy.get("rewrite", False)),
                "requireSubdomain": bool(proxy.get("requireSubdomain", False)),
            },
        }
        if tool.get("iconUrl"):
            api_tool["iconUrl"] = tool["iconUrl"]
        if tool.get("supportedFileExtensions"):
            api_tool["supportedFileExtensions"] = list(tool["supportedFileExtensions"])
        tools.append(api_tool)
    return tools


def parse_tags(text: str) -> list:
    """'a=1,b=2' -> ['a=1', 'b=2'] (the API stores tags as plain strings)."""
    tags = []
    for pair in text.split(","):
        pair = pair.strip()
        if not pair:
            continue
        key, sep, value = pair.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"tag '{pair}' is not in key=value form")
        tags.append(f"{key.strip()}={value.strip()}")
    if not tags:
        raise ValueError("--tags is empty")
    return tags


class CreateEnvironment(DominoCLI):
    description = __doc__

    def build_parser(self) -> argparse.ArgumentParser:
        parser = super().build_parser()
        parser.formatter_class = argparse.RawDescriptionHelpFormatter
        return parser

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("-e", "--environment-id",
                            help="Existing environment to update (adds a new revision). "
                                 "Omit to create a new environment.")
        parser.add_argument("--dockerfile",
                            help="Path to a file of Dockerfile instructions (no FROM line); "
                                 "required unless --dump")
        parser.add_argument("--name", help="Environment name (required when creating)")
        parser.add_argument("--base-image",
                            help="Base image URI (required when creating; on update defaults to the previous revision's)")
        parser.add_argument("--description", default=None, help="Environment description (create only)")
        parser.add_argument("--visibility", choices=["private", "global"], default=None,
                            help="Environment visibility when creating (default: private)")
        parser.add_argument("--summary", default=None, help="Revision summary / build note")
        parser.add_argument("--env-vars", metavar="FILE",
                            help="JSON or YAML file of {NAME: value} environment variables")
        parser.add_argument("--pluggable-workspace-tools", "--pwt", dest="pwt", metavar="FILE",
                            help="YAML file defining Pluggable Workspace Tools (UI format)")
        parser.add_argument("--pre-setup-script", metavar="FILE", help="Pre-setup script (bash)")
        parser.add_argument("--post-setup-script", metavar="FILE", help="Post-setup script (bash)")
        parser.add_argument("--pre-run-script", metavar="FILE", help="Pre-run script (bash)")
        parser.add_argument("--post-run-script", metavar="FILE",
                            help="Post-run script (bash), run after the execution stops")
        parser.add_argument("--supported-clusters", metavar="LIST",
                            help="Comma-delimited cluster types this environment supports: "
                                 + ",".join(CLUSTER_TYPES))
        parser.add_argument("--skip-cache", action="store_true",
                            help="Build the revision without the Docker layer cache")
        parser.add_argument("--tags", metavar="K=V,K=V",
                            help="Comma-delimited key=value pairs, stored as 'key=value' tags")
        parser.add_argument("--no-inherit", action="store_true",
                            help="On update, do not carry over the previous revision's image, scripts, "
                                 "variables, workspace tools, clusters, VPN and tags; build the "
                                 "revision only from the flags given (needs --base-image)")
        parser.add_argument("--dump", action="store_true",
                            help="Write the latest revision of --environment-id to a directory named "
                                 "after the environment id (no changes are made)")
        parser.add_argument("--import", dest="import_name", metavar="NAME",
                            help="Create a new environment called NAME from the files in --directory")
        parser.add_argument("--directory", metavar="DIR",
                            help="With --import: directory holding Dockerfile, optional env-vars.json, "
                                 "pluggable-workspace-tools.yaml, *.sh scripts and environment.json")
        parser.add_argument("--output-dir", default=".", metavar="DIR",
                            help="With --dump: parent directory for the environment-id directory "
                                 "(default: current directory)")
        parser.add_argument("--wait", action="store_true",
                            help="Poll until the new revision's build finishes; exit 1 if it fails")
        parser.add_argument("--wait-timeout", type=int, default=1800,
                            help="Seconds to wait with --wait (default: 1800)")

    def validate(self, parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
        if args.dump:
            if not args.environment_id:
                parser.error("--dump requires --environment-id")
            conflicting = ["import_name", "directory", "dockerfile", "name", "base_image", "env_vars", "pwt", "pre_setup_script",
                           "post_setup_script", "pre_run_script", "post_run_script",
                           "supported_clusters", "tags", "summary"]
            given = [a for a in conflicting if getattr(args, a) is not None]
            given += [a for a in ("skip_cache", "no_inherit", "wait") if getattr(args, a)]
            if given:
                parser.error("--dump cannot be combined with: "
                             + ", ".join("--" + a.replace("_", "-") for a in given))
            return
        args.import_extra = {}
        if args.import_name is not None or args.directory is not None:
            self._resolve_import(parser, args)
        if args.description is None:
            args.description = ""
        if args.visibility is None:
            args.visibility = "private"
        if not args.dockerfile:
            parser.error("--dockerfile is required unless --dump or --import is given")
        if not args.environment_id:
            if not args.name:
                parser.error("--name is required when creating an environment (no --environment-id)")
            if not args.base_image:
                parser.error("--base-image is required when creating an environment")
        elif args.no_inherit and not args.base_image:
            parser.error("--base-image is required with --no-inherit")
        try:
            with open(args.dockerfile, encoding="utf-8") as fh:
                args.dockerfile_text = fh.read()
        except OSError as exc:
            parser.error(f"cannot read --dockerfile: {exc}")
        for line in args.dockerfile_text.splitlines():
            if re.match(r"\s*FROM\s", line, re.IGNORECASE):
                parser.error("the Dockerfile must not contain FROM; use --base-image instead")

        # Optional revision settings; only fields the user supplied are sent.
        args.extra = {}
        args.scripts = {}
        try:
            if args.env_vars is not None:
                args.extra["environmentVariables"] = env_vars_to_api(
                    load_structured(args.env_vars, "--env-vars"))
            if args.pwt is not None:
                args.extra["workspaceTools"] = tools_to_api(
                    load_structured(args.pwt, "--pluggable-workspace-tools"))
            if args.supported_clusters is not None:
                clusters = [c.strip().lower() for c in args.supported_clusters.split(",") if c.strip()]
                bad = [c for c in clusters if c not in CLUSTER_TYPES]
                if bad or not clusters:
                    raise ValueError(f"--supported-clusters: invalid value(s) {bad or '(empty)'}; "
                                     f"choose from {', '.join(CLUSTER_TYPES)}")
                args.extra["supportedClusters"] = clusters
            if args.tags is not None:
                args.extra["tags"] = parse_tags(args.tags)
            if args.skip_cache:
                args.extra["skipCache"] = True
            for attr, field in SCRIPT_FLAGS.items():
                path = getattr(args, attr)
                if path is not None:
                    try:
                        with open(path, encoding="utf-8") as fh:
                            args.scripts[field] = fh.read()
                    except OSError as exc:
                        raise ValueError(f"cannot read --{attr.replace('_', '-')}: {exc}")
            for key, value in args.import_extra.items():   # from environment.json; flags win
                args.extra.setdefault(key, value)
        except ValueError as exc:
            parser.error(str(exc))

    @staticmethod
    def _resolve_import(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
        """Fill unset arguments from the --directory of an --import.

        Precedence: explicit flag > file in the directory > environment.json > default.
        """
        if args.import_name is None or not args.directory:
            parser.error("--import and --directory must be used together")
        if args.environment_id:
            parser.error("--import creates a new environment; it cannot be combined with --environment-id")
        if args.name:
            parser.error("--import takes the new environment's name; do not also pass --name")
        directory = args.directory
        if not os.path.isdir(directory):
            parser.error(f"--directory '{directory}' is not a directory")
        args.name = args.import_name

        def path(filename: str, required: bool = False):
            full = os.path.join(directory, filename)
            if os.path.isfile(full):
                return full
            if required:
                parser.error(f"{filename} not found in {directory}")
            return None

        if args.dockerfile is None:
            args.dockerfile = path("Dockerfile", required=True)
        if args.env_vars is None:
            args.env_vars = path("env-vars.json")
        if args.pwt is None:
            args.pwt = path("pluggable-workspace-tools.yaml")
        for attr, filename in (("pre_setup_script", "pre-setup.sh"), ("post_setup_script", "post-setup.sh"),
                               ("pre_run_script", "pre-run.sh"), ("post_run_script", "post-run.sh")):
            if getattr(args, attr) is None:
                setattr(args, attr, path(filename))

        meta_path = path("environment.json")
        if not meta_path:
            return
        try:
            with open(meta_path, encoding="utf-8") as fh:
                meta = json.load(fh)
            if not isinstance(meta, dict):
                raise ValueError("not a JSON object")
        except (OSError, ValueError) as exc:
            parser.error(f"cannot read {meta_path}: {exc}")
        if args.base_image is None:
            args.base_image = meta.get("baseImage") or None
        if args.description is None:
            args.description = meta.get("description") or ""
        if args.visibility is None and meta.get("visibility") in ("private", "global"):
            args.visibility = meta["visibility"]
        if args.supported_clusters is None and meta.get("supportedClusters"):
            args.supported_clusters = ",".join(meta["supportedClusters"])
        if meta.get("tags"):
            # kept verbatim: tags created in the UI need not be key=value
            args.import_extra["tags"] = [str(t) for t in meta["tags"]]
        if meta.get("useVpn"):
            args.import_extra["useVpn"] = True
        if meta.get("isRestricted"):
            print("note: environment.json marks the source as restricted; isRestricted is not "
                  "set on the import", file=sys.stderr)

    # -- API helpers ----------------------------------------------------------
    @staticmethod
    def _is_v2(version: str) -> bool:
        m = re.match(r"(\d+)\.(\d+)", version or "")
        return bool(m) and (int(m.group(1)), int(m.group(2))) >= (6, 4)

    @staticmethod
    def _fetch_latest(client: DominoClient, env_id: str):
        """(environment, latestRevision summary, full revision) via Internal API routes."""
        env = client.get(f"/v4/environments/{env_id}").json()
        latest = env.get("latestRevision") or {}
        rev = client.get(f"/v4/environments/environmentRevision/{latest['id']}").json()["environmentRevision"]
        return env, latest, rev

    def _inherit(self, client: DominoClient, env_id: str) -> dict:
        """Settings of the environment's latest revision, as API-named fields.

        The Public API drops anything a new revision doesn't restate (scripts, variables,
        workspace tools, image...), so an update starts from the previous revision.
        Reads Internal API routes (non-public; may change in a future Domino version).
        """
        _, latest, rev = self._fetch_latest(client, env_id)
        out: dict = {"image": rev.get("dockerImage")}
        for field in SCRIPT_FLAGS.values():
            if rev.get(field):
                out[field] = rev[field]
        if rev.get("buildEnvironmentVariables"):
            out["environmentVariables"] = [{"key": k, "value": v}
                                           for item in rev["buildEnvironmentVariables"]
                                           for k, v in item.items()]
        raw = (rev.get("workspacesProperties") or {}).get("raw")
        tools = yaml.safe_load(raw) if raw else None
        if tools:
            for tool in tools.values():     # the UI form stores [""] for "no extensions"
                tool["supportedFileExtensions"] = [e for e in tool.get("supportedFileExtensions", []) if e]
            out["workspaceTools"] = tools_to_api(tools)
        if rev.get("clusterTypes"):
            out["supportedClusters"] = [str(c).lower() for c in rev["clusterTypes"]]
        if rev.get("shouldUseVPN"):
            out["useVpn"] = True
        if latest.get("tags"):
            out["tags"] = latest["tags"]
        return out

    def _revision_body(self, args: argparse.Namespace, v2: bool, inherited: dict = None) -> dict:
        inherited = inherited or {}
        build = {"dockerfileInstructions": args.dockerfile_text}
        build.update({k: v for k, v in inherited.items() if k in SCRIPT_FLAGS.values()})
        build.update(args.scripts)
        body: dict = {"buildConfiguration": build} if v2 else dict(build)
        body.update({k: v for k, v in inherited.items()
                     if k not in SCRIPT_FLAGS.values() and k != "image"})
        body.update(args.extra)
        image = args.base_image or inherited.get("image")
        if image:
            body["image"] = image
        if args.summary:
            body["summary"] = args.summary
        return body

    def _dump(self, client: DominoClient, args: argparse.Namespace) -> int:
        env, latest, rev = self._fetch_latest(client, args.environment_id)
        out_dir = os.path.join(args.output_dir, args.environment_id)
        if os.path.isdir(out_dir) and os.listdir(out_dir):
            print(f"error: {out_dir} already exists and is not empty", file=sys.stderr)
            return 1
        os.makedirs(out_dir, exist_ok=True)
        written = []

        def write(name: str, text: str) -> None:
            with open(os.path.join(out_dir, name), "w", encoding="utf-8") as fh:
                fh.write(text if text.endswith("\n") else text + "\n")
            written.append(name)

        write("Dockerfile", rev.get("dockerfileInstructions") or "")
        if rev.get("buildEnvironmentVariables"):
            merged = {k: v for item in rev["buildEnvironmentVariables"] for k, v in item.items()}
            write("env-vars.json", json.dumps(merged, indent=2))
        raw = (rev.get("workspacesProperties") or {}).get("raw")
        tools = yaml.safe_load(raw) if raw else None
        if tools:
            for tool in tools.values():
                tool["supportedFileExtensions"] = [e for e in tool.get("supportedFileExtensions", []) if e]
                if not tool["supportedFileExtensions"]:
                    del tool["supportedFileExtensions"]
            write("pluggable-workspace-tools.yaml", yaml.safe_dump(tools, sort_keys=False))
        script_files = {"preSetupScript": "pre-setup.sh", "postSetupScript": "post-setup.sh",
                        "preRunScript": "pre-run.sh", "postRunScript": "post-run.sh"}
        for field, filename in script_files.items():
            if rev.get(field):
                write(filename, rev[field])
        meta = {
            "id": args.environment_id,
            "name": env.get("name"),
            "description": env.get("description"),
            "visibility": str(env.get("visibility", "")).lower(),
            "baseImage": rev.get("dockerImage"),
            "revisionId": rev.get("id"),
            "revisionNumber": rev.get("number"),
            "supportedClusters": [str(c).lower() for c in rev.get("clusterTypes") or []],
            "useVpn": bool(rev.get("shouldUseVPN")),
            "isRestricted": bool(rev.get("isRestricted")),
            "tags": latest.get("tags") or [],
        }
        write("environment.json", json.dumps(meta, indent=2))
        print(f"Dumped revision {rev.get('number')} of '{env.get('name')}' to {out_dir}/")
        for name in written:
            print(f"  {name}")
        return 0

    def run(self, client: DominoClient, args: argparse.Namespace) -> int:
        if args.dump:
            return self._dump(client, args)
        v2 = self._is_v2(client.platform_version())
        api = "v2" if v2 else "beta"

        if args.environment_id:
            env_id = args.environment_id
            inherited = {} if args.no_inherit else self._inherit(client, env_id)
            if not args.base_image and not inherited.get("image"):
                print("error: --base-image is required with --no-inherit", file=sys.stderr)
                return 2
            body = self._revision_body(args, v2, inherited)
            resp = client.post(f"/api/environments/{api}/environments/{env_id}/revisions", json=body)
            revision = resp.json().get("environmentRevision", {})
            print(f"Added revision {revision.get('number', '?')} (id {revision.get('id', '?')}) "
                  f"to environment {env_id}")
        else:
            rev = self._revision_body(args, v2)
            body = {
                "name": args.name,
                "description": args.description,
                "visibility": args.visibility,
                "addBaseDependencies": True,
                "rebuildOnBaseChanges": "never",
            }
            if v2:
                body["initialRevision"] = rev
            else:
                body.update(rev)
            resp = client.post(f"/api/environments/{api}/environments", json=body)
            environment = resp.json().get("environment", {})
            env_id = environment.get("id")
            revision = environment.get("latestRevision") or {}
            print(f"Created environment '{args.name}' (id {env_id})")

        revision_id = revision.get("id")
        if not args.wait:
            return 0
        return self._wait(client, env_id, revision_id, args.wait_timeout)

    def _revision_status(self, client: DominoClient, env_id: str, revision_id: str) -> str:
        """Build status of one specific revision ('' if unknown)."""
        env = client.get(f"/api/environments/v1/environments/{env_id}").json()["environment"]
        latest = env.get("latestRevision") or {}
        if not revision_id or latest.get("id") == revision_id:
            return (latest.get("status") or "").lower()
        # Another revision was submitted since: the Public API only reports the latest, so
        # read this one from the Environments web UI route (non-public; may change).
        page = client.get(f"/environments/{env_id}/json/paged/0/50").json()
        for rev in page.get("paginatedResults", {}).get("listing", []):
            if rev.get("id") == revision_id:
                return str(rev.get("buildStatus") or "").lower()
        return ""

    def _wait(self, client: DominoClient, env_id: str, revision_id: str, timeout: int) -> int:
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            status = self._revision_status(client, env_id, revision_id)
            if status != last:
                print(f"  build status: {status or 'pending'}")
                last = status
            if status in TERMINAL_STATUSES:
                return 0 if status == "succeeded" else 1
            time.sleep(10)
        print("error: timed out waiting for the build", file=sys.stderr)
        return 1


if __name__ == "__main__":
    CreateEnvironment().main()

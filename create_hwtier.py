#!/usr/bin/env python3
"""Create a Domino Hardware Tier, or update an existing one.

Create:  create_hwtier.py --file examples/hwtier.example.json
Update:  create_hwtier.py --hwtier-id HWTIER_ID --file tier.json
Dump:    create_hwtier.py --hwtier-id HWTIER_ID --dump > tier.json

The JSON file holds the hardware tier definition (Public API
``/api/hardwaretiers/v1/hardwaretiers``). When updating, the current tier is fetched and
the file's fields are merged over it, so the file may contain only the fields to change.
"""
from __future__ import annotations

import argparse
import json

from domino_common import DominoCLI, DominoClient

# Read-only fields returned by GET that the create call does not accept.
CREATE_REJECTED = {"capacity", "creationTime", "updateTime", "dataPlaneName", "metadata"}


def deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class CreateHardwareTier(DominoCLI):
    description = __doc__

    def build_parser(self) -> argparse.ArgumentParser:
        parser = super().build_parser()
        parser.formatter_class = argparse.RawDescriptionHelpFormatter
        return parser

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--hwtier-id",
                            help="Existing hardware tier id to update. Omit to create a new tier.")
        parser.add_argument("-f", "--file",
                            help="Path to a JSON file with the hardware tier definition "
                                 "(required unless --dump)")
        parser.add_argument("--dump", action="store_true",
                            help="Write the definition of the tier given by --hwtier-id to "
                                 "stdout as JSON and exit (no changes are made)")

    def validate(self, parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
        if args.dump:
            if not args.hwtier_id:
                parser.error("--dump requires --hwtier-id")
            if args.file:
                parser.error("--dump cannot be combined with --file")
            return
        if not args.file:
            parser.error("--file is required unless --dump is given")
        try:
            with open(args.file, encoding="utf-8") as fh:
                args.definition = json.load(fh)
        except (OSError, ValueError) as exc:
            parser.error(f"cannot read JSON from --file: {exc}")
        if not isinstance(args.definition, dict):
            parser.error("--file must contain a JSON object")
        file_id = args.definition.get("id")
        if args.hwtier_id and file_id and file_id != args.hwtier_id:
            parser.error(f"id in --file ('{file_id}') does not match --hwtier-id ('{args.hwtier_id}')")
        if not args.hwtier_id:
            missing = [k for k in ("id", "name", "resources", "nodePool") if k not in args.definition]
            if missing:
                parser.error("--file is missing required field(s) for create: " + ", ".join(missing))

    def run(self, client: DominoClient, args: argparse.Namespace) -> int:
        if args.dump:
            tier = client.get(f"/api/hardwaretiers/v1/hardwaretiers/{args.hwtier_id}").json()
            print(json.dumps(tier.get("hardwareTier", tier), indent=2))
            return 0
        if args.hwtier_id:
            current = client.get(f"/api/hardwaretiers/v1/hardwaretiers/{args.hwtier_id}").json()
            current = current.get("hardwareTier", current)
            body = deep_merge(current, args.definition)
            body["id"] = args.hwtier_id
            client.put("/api/hardwaretiers/v1/hardwaretiers", json=body)
            print(f"Updated hardware tier '{args.hwtier_id}'")
        else:
            body = {k: v for k, v in args.definition.items() if k not in CREATE_REJECTED}
            resp = client.post("/api/hardwaretiers/v1/hardwaretiers", json=body)
            created = resp.json() if resp.content else {}
            created = created.get("hardwareTier", created)
            print(f"Created hardware tier '{created.get('id', body['id'])}'")
        return 0


if __name__ == "__main__":
    CreateHardwareTier().main()

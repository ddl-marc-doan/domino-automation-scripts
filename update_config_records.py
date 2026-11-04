#!/usr/bin/env python3
"""Set Domino Configuration Records (central config) from a JSON file.

The file is a JSON object of {"record.name": "string value", ...}. Records that exist are
updated when the value differs; missing records are created. Values are stored as strings.

Uses the Internal API ``/v4/admin/centralConfigSettings`` (non-public route; may change
in a future Domino version) - there is no Public API route for configuration records.
Requires a Domino administrator. Changes take effect only after the Domino services are
restarted (every nucleus-* deployment and each data-plane-agent); there is no documented
API for that, so this script prints a reminder instead of restarting anything.
"""
from __future__ import annotations

import argparse
import json

from domino_common import DominoCLI, DominoClient

PATH = "/v4/admin/centralConfigSettings"


def to_string(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


class UpdateConfigRecords(DominoCLI):
    description = __doc__

    def build_parser(self) -> argparse.ArgumentParser:
        parser = super().build_parser()
        parser.formatter_class = argparse.RawDescriptionHelpFormatter
        return parser

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("-f", "--file", required=True,
                            help='JSON file of {"record name": "value"} pairs')
        parser.add_argument("--namespace", default="common",
                            help="Configuration namespace for new records (default: common)")
        parser.add_argument("--dry-run", action="store_true",
                            help="Show what would change without changing anything")

    def validate(self, parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
        try:
            with open(args.file, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            parser.error(f"cannot read JSON from --file: {exc}")
        if not isinstance(data, dict) or not data:
            parser.error("--file must contain a non-empty JSON object of name/value pairs")
        if any(isinstance(v, (dict, list)) or v is None for v in data.values()):
            parser.error("all values must be scalars (they are stored as strings)")
        args.records = {k: to_string(v) for k, v in data.items()}

    def run(self, client: DominoClient, args: argparse.Namespace) -> int:
        existing = client.get(PATH).json()
        index = {(r["setting"]["namespace"], r["setting"]["key"]): r for r in existing}
        counts = {"created": 0, "updated": 0, "unchanged": 0}

        for key, value in args.records.items():
            record = index.get((args.namespace, key))
            if record is None:
                matches = [r for (ns, k), r in index.items() if k == key]
                if len(matches) == 1:           # exists in another namespace: update that one
                    record = matches[0]
                elif len(matches) > 1:
                    print(f"  skipped  {key}: exists in several namespaces "
                          f"({', '.join(sorted(r['setting']['namespace'] for r in matches))}); "
                          "re-run with --namespace")
                    continue
            if record is None:
                action = "created"
                if not args.dry_run:
                    client.post(PATH, json={"namespace": args.namespace, "key": key, "value": value})
            elif record["setting"]["value"] == value:
                action = "unchanged"
            else:
                action = "updated"
                if not args.dry_run:
                    setting = dict(record["setting"], value=value)
                    client.put(f"{PATH}/{record['id']}", json=setting)
            counts[action] += 1
            suffix = " (dry run)" if args.dry_run and action != "unchanged" else ""
            print(f"  {action:<11}{key}{suffix}")

        print("Summary: " + ", ".join(f"{n} {k}" for k, n in counts.items()))
        if (counts["created"] or counts["updated"]) and not args.dry_run:
            print("\nNote: Domino services read configuration records at startup. Restart every "
                  "nucleus-* deployment and each data-plane-agent (local and remote) for these "
                  "changes to take effect (Configuration Records page > restart services, or kubectl).")
        return 0


if __name__ == "__main__":
    UpdateConfigRecords().main()

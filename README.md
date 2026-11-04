# Domino platform configuration scripts

Python scripts (using `requests` only) for configuring a Domino 6.3+ deployment.

| Script | Purpose |
|---|---|
| `domino_common.py` | `DominoCLI` (shared flags) and `DominoClient` (authenticated session) base classes |
| `create_environment.py` | Create a Compute Environment, or add a revision to one (`-e`) |
| `create_hwtier.py` | Create a Hardware Tier, or update one (`--hwtier-id`) from a JSON file; `--dump` prints a tier as JSON |
| `update_config_records.py` | Upsert Configuration Records from a JSON `{name: value}` file |

Not yet implemented: feature flags (no API route in the 6.3.1 specs) and Keycloak configuration.

## Install
```bash
pip install .        # or: pip install -e .   (dependencies: requests, PyYAML)
```

## Common flags
`-k/--api-key` and `-h/--host` are read from `DOMINO_USER_API_KEY` and `DOMINO_API_HOST`
when those are set; otherwise the flags are required. (`-h` is the host, so use `--help`
for help.)

**Authentication:** requests are sent with `Authorization: Bearer <key>`. A legacy user
API key is not accepted as a bearer token on 6.3.1, so on a 401/403 the client retries
once with the deprecated `X-Domino-Api-Key` header and prints a note to stderr.

## Usage
```bash
# Environment (Dockerfile instructions; an optional single FROM line sets the base image)
python create_environment.py --name my-env --base-image IMAGE --dockerfile Dockerfile --wait
python create_environment.py -e ENV_ID --dockerfile Dockerfile --wait   # new revision (inherits previous settings)

# Dump an environment's latest revision into ./ENV_ID/ (files are readable by the flags above)
python create_environment.py -e ENV_ID --dump [--output-dir DIR]

# Create a new environment from a directory (e.g. one written by --dump)
python create_environment.py --import NEW_NAME --directory ./ENV_ID [--wait]
python create_environment.py --import my-env --directory examples/environment-import   # runnable example

# Environment with everything
python create_environment.py --name my-env --base-image IMAGE --dockerfile Dockerfile \
  --env-vars examples/env-vars.example.json \
  --pluggable-workspace-tools examples/pluggable-workspace-tools.example.yaml \
  --pre-setup-script pre_setup.sh --post-setup-script post_setup.sh \
  --pre-run-script pre_run.sh --post-run-script post_run.sh \
  --supported-clusters ray,dask --skip-cache --tags team=ml,tier=gold --wait

# Hardware tier
python create_hwtier.py --file examples/hwtier.example.json
python create_hwtier.py --hwtier-id example-medium-k8s --file changes.json   # partial file is merged

# Configuration records
python update_config_records.py --file records.json [--namespace common] [--dry-run]
```

## Notes
- Environments: the API does not carry settings over to a new revision (an update that sends
  only a Dockerfile drops the scripts, variables, workspace tools and summary). Updates therefore
  read the latest revision (Internal API; non-public) and inherit its image, scripts, variables,
  workspace tools, clusters, VPN and tags; flags override. `--no-inherit` starts clean.
- `--dump` writes `ENV_ID/` with `Dockerfile`, `env-vars.json`, `pluggable-workspace-tools.yaml`,
  `pre-setup.sh`, `post-setup.sh`, `pre-run.sh`, `post-run.sh` (only the ones the revision has) and
  `environment.json` (name, description, visibility, base image, revision, clusters, VPN, restricted
  flag, tags). It refuses to write into a non-empty directory. Uses Internal API routes (non-public).
- `--import NEW_NAME --directory DIR` creates a new environment from `DIR`: `Dockerfile` (required) and
  optionally `env-vars.json`, `pluggable-workspace-tools.yaml`, `pre-setup.sh`, `post-setup.sh`,
  `pre-run.sh`, `post-run.sh`, `environment.json`. With `environment.json`, its base image,
  description, visibility, supported clusters, VPN setting and tags are used; its id, name and
  revision are ignored, and `isRestricted` is never set. Without it, pass `--base-image` or put a `FROM` line in the Dockerfile.
  Precedence: explicit flag > file in the directory > `environment.json` > default. A global
  source environment imports as `global` unless you pass `--visibility private`.
- **Base image precedence:** `--base-image` flag, then `environment.json`'s `baseImage` (import), then the
  Dockerfile's `FROM` line (on update, a `FROM` line also beats the previous revision's image). The `FROM`
  line is removed before sending, as Domino's Dockerfile Instructions can't contain one. Multi-stage
  Dockerfiles, `FROM scratch` and variable images (`FROM ${BASE}`) are rejected. If a lower-precedence
  `FROM` disagrees, a note is printed.
- `--pluggable-workspace-tools` takes the UI's YAML format; it is converted to the API's
  `workspaceTools` (`start` -> `startScripts`, `httpProxy` -> `proxyConfig`). `--tags` pairs are
  stored as `key=value` strings. `--env-vars` accepts JSON or YAML; `DOMINO_*` names are rejected.
- `isRestricted` is deliberately not exposed (see below).
- Hardware tiers and environments can only be archived, not deleted (`DELETE` archives).
- Configuration records use the Internal API (`/v4/admin/centralConfigSettings`; non-public,
  may change) and need an administrator.

## `isRestricted` (not exposed as a flag)
The API field marks an environment as restricted, for use only in restricted Projects, and it
needs the ClassifyEnvironments permission. The script never sends it, so it stays `false`, the
platform default. Set it only deliberately, per environment, by an administrator.

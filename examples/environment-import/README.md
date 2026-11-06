# Example environment import directory

Create a new environment from this directory:

```bash
python create_environment.py --import my-new-env --directory examples/environment-import [--wait]
```

| File | Required | Purpose |
|---|---|---|
| `Dockerfile` | yes | Dockerfile instructions (no `FROM`) |
| `environment.json` | no | base image, description, visibility, supported clusters, VPN, tags. Without it pass `--base-image`. Its `name` (and `id`/revision fields in dumps) are ignored: the `--import` value is the name |
| `env-vars.json` | no | `{NAME: value}` environment variables |
| `pluggable-workspace-tools.yaml` | no | workspace tools (UI format) |
| `pre-setup.sh`, `post-setup.sh`, `pre-run.sh`, `post-run.sh` | no | environment scripts |

The same layout is written by `--dump`. Check `baseImage` against the image tags available on
your deployment before building.

# Releasing v8-utils

The initial PyPI release is `1984`. The root `VERSION` is the numeric update
stamp printed by every CLI's `--version`. Both pyproject files currently use
`1984` as their distribution version too. Keep them aligned for releases and
run `uv lock` after changing the package version.

## Release 1984

- Interactive pp and vt update prompts, with daily caching, explicit consent,
  opt-outs, and protection for local/editable installs.
- `--version` on pp, vt, jsb, pd, lv, and v8-mcp.
- MIT license and repository links in the package metadata.

The updater checks GitHub's `main/VERSION` and reinstalls from GitHub, including
when the current install came from PyPI. Publish the source containing VERSION
before making the PyPI release available. Existing Git installations need a
manual upgrade to acquire the checker.

This release publishes the full `v8-utils` distribution. `v8-utils-core` remains
a checkout/Git install; its build reaches outside `packaging/core`, so it does
not yet have a standalone source distribution for PyPI.

## Build and validate

From the repository root:

```sh
uv sync --locked
uv run pytest -q
uv build --out-dir dist
uvx twine check dist/v8_utils-1984.tar.gz dist/v8_utils-1984-py3-none-any.whl
```

`uv build` builds the wheel from the source distribution. Check that both
contain VERSION=1984. Test the wheel in an isolated tool directory:

```sh
release_tmp=$(mktemp -d)
UV_TOOL_DIR="$release_tmp/tools" UV_TOOL_BIN_DIR="$release_tmp/bin" \
  uv tool install ./dist/v8_utils-1984-py3-none-any.whl
for cli in pp vt jsb pd lv v8-mcp; do
  "$release_tmp/bin/$cli" --version
  "$release_tmp/bin/$cli" --help >/dev/null
done
```

Each version command should print `<cli> 1984`. Check that no development paths
or unexpected files are included in the archives.

## Publish

Publishing is a separate step, after the release commit is reviewed and pushed.
Use a PyPI account authorized to publish `v8-utils`, with an API token supplied
through `UV_PUBLISH_TOKEN` (or configure trusted publishing). A public JSON API
404 does not establish that a project name can be registered.

Upload only the two verified artifacts:

```sh
uv publish dist/v8_utils-1984.tar.gz dist/v8_utils-1984-py3-none-any.whl
```

Then verify a fresh install from PyPI and its `--version` output:

```sh
uv tool install v8-utils==1984
pp --version
vt --version
```

PyPI release files cannot be replaced. Corrections need a new numeric release.

# Releasing gseek

This checklist is for a maintainer publishing a wheel/sdist to PyPI. It assumes
a clean git worktree and a PyPI API token. Never commit or paste the token.

## 0. Before you start

- PyPI project `gseek` exists (first upload creates it).
- For the **first** upload the token must be account-scoped / All projects;
  a project-scoped token can only be created after the project exists.
- `dist/` is gitignored; never commit build artifacts.
- Confirm no secrets are in the tree or manifest.

## 1. Version and changelog

- [ ] `pyproject.toml` → `[project].version`
- [ ] `src/gseek/version.py` → `__version__`
- [ ] `pixi.toml` → `[workspace].version`
- [ ] `tests/test_project_identity.py` expected version
- [ ] `docs/api.md` version references
- [ ] `README.md` status line
- [ ] `CHANGELOG.md`: move `Unreleased` to `## [X.Y.Z] - YYYY-MM-DD`

## 2. Gate

```bash
cd /path/to/gseek

PYTHONPATH=src .pixi/envs/default/bin/python3.12 -m pytest -q
PYTHONPATH=src .pixi/envs/default/bin/python3.12 -m pytest --cov=gseek -q
.pixi/envs/default/bin/ruff check .
.pixi/envs/default/bin/ruff format --check src tests
PYTHONPATH=src .pixi/envs/default/bin/python3.12 -m pyright \
  --pythonpath .pixi/envs/default/bin/python3.12
```

With external tools on `PATH`:

```bash
PYTHONPATH=src .pixi/envs/default/bin/python3.12 -m pytest -m integration -q
```

## 3. Build

```bash
rm -rf dist
uv build --out-dir dist
ls -lh dist
```

The wheel is built from the sdist, which catches missing files and build-backend
mistakes.

## 4. Verify the artifacts

```bash
uvx --from twine twine check dist/*

rm -rf /tmp/gseek-release-venv
uv venv --python 3.12 /tmp/gseek-release-venv
uv pip install --python /tmp/gseek-release-venv/bin/python dist/gseek-X.Y.Z-py3-none-any.whl
/tmp/gseek-release-venv/bin/gseek --version
/tmp/gseek-release-venv/bin/gseek backends --json
/tmp/gseek-release-venv/bin/gseek --help
```

Also install the sdist in a second venv. Check `py.typed`, `LICENSE`, `METADATA`
(`Name`, `Version`, `Requires-Python`, extras).

## 5. Upload

Use a token scoped to the project once it exists; keep it out of shell history:

```bash
read -rs UV_PUBLISH_TOKEN
export UV_PUBLISH_TOKEN
uv publish dist/gseek-X.Y.Z-py3-none-any.whl dist/gseek-X.Y.Z.tar.gz
unset UV_PUBLISH_TOKEN
```

Do not re-upload an existing version; PyPI rejects duplicate filenames. Bump the
version instead.

## 6. Verify from PyPI

```bash
curl -s https://pypi.org/pypi/gseek/json | \
  python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["info"]["version"])'

rm -rf /tmp/gseek-pypi-venv
uv venv --python 3.12 /tmp/gseek-pypi-venv
uv pip install --python /tmp/gseek-pypi-venv/bin/python gseek==X.Y.Z
/tmp/gseek-pypi-venv/bin/gseek --version
```

## 7. Tag and downstream

```bash
git tag -a vX.Y.Z -m "gseek X.Y.Z"
git push origin main --tags
```

Update consumers (for example gside) from `PYTHONPATH=../gseek/src` to the
published `gseek==X.Y.Z` and rerun their parity tests.

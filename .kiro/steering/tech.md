# Tech Stack

## Language & Runtime
- Python 3.10+ (supports up to 3.15, PyPy 3.10/3.11)
- Async I/O via **Twisted** (core framework — reactor, protocols, transports)
- `twisted[conch]` provides SSH protocol support

## Key Dependencies
- `twisted[conch]` — SSH/Telnet protocol implementation
- `cryptography`, `bcrypt`, `service_identity` — auth and crypto
- `treq` — async HTTP client (used in output plugins)
- `tftpy` — TFTP support
- `requests` — sync HTTP (output plugins)
- `attrs` — data classes

## Optional Dependencies (output plugins)
Installed via extras: `mysql`, `mongodb`, `elasticsearch`, `s3`, `slack`, `influxdb`, `rethinkdblog`, `pool` (libvirt)

## Build System
- **setuptools** with `setuptools-scm` for version management (version derived from git tags → `src/cowrie/_version.py`)
- **tox** for test/lint/typing environments
- **ruff** for linting and formatting (line length: 88, target: Python 3.10)
- **mypy** + **pyright** + **pyre** for static type checking
- **pre-commit** for git hooks

## Common Commands

```bash
# Run all tests
make test
# or directly:
tox
coverage run -m unittest discover src --verbose

# Lint
make lint
tox -e lint        # runs ruff, yamllint, pyright, pylint

# Type checking
tox -e typing      # runs mypy, mypyc, pyre, pyright

# Build package
make build
python -m build

# Build docs
make docs

# Run pre-commit hooks
make pre-commit
pre-commit run --all-files

# Docker
make docker-build  # build image
make docker-start  # run container (ports 2222, 2223)
make docker-stop
make docker-shell  # exec bash in running container

# Upgrade pip dependencies
make pip-upgrade
```

## Configuration
- Config file: `etc/cowrie.cfg` (user) and `etc/cowrie.cfg.dist` (defaults, do not edit)
- Config values can be overridden via environment variables: `COWRIE_<SECTION>_<KEY>` (uppercased)
- `EnvironmentConfigParser` in `src/cowrie/core/config.py` handles env var overrides
- Global singleton: `CowrieConfig` imported from `cowrie.core.config`

## Import Order (isort profile: black)
Sections: `FUTURE → STDLIB → THIRDPARTY → ZOPE → TWISTED → FIRSTPARTY → LOCALFOLDER`

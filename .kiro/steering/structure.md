# Project Structure

## Root Layout
```
cowrie/
├── src/cowrie/          # Main package
├── src/backend_pool/    # Libvirt/QEMU backend pool (optional)
├── etc/                 # Configuration files
├── honeyfs/             # Fake filesystem file contents (e.g. /etc/passwd, /proc/cpuinfo)
├── docker/              # Dockerfile and docker-compose
├── docs/                # Sphinx documentation + integration guides
├── bin/                 # Entry point scripts
└── pyproject.toml       # Build config, deps, tool config
```

## `src/cowrie/` Package
```
src/cowrie/
├── core/           # Shared infrastructure: config, auth, output base, utils, ttylog
├── commands/       # Fake shell command implementations (one class per command)
├── shell/          # Shell emulation: protocol, filesystem, session handling
├── ssh/            # SSH server protocol and transport
├── telnet/         # Telnet server protocol and transport
├── ssh_proxy/      # SSH proxy mode
├── telnet_proxy/   # Telnet proxy mode
├── llm/            # LLM backend mode
├── output/         # Output plugins (one file per destination)
├── insults/        # Terminal insult/interaction layer
├── pool_interface/ # Interface to backend_pool
├── python/         # Utility helpers (logfile rotation, etc.)
├── scripts/        # CLI entry points: cowrie, fsctl, playlog, createfs, asciinema
├── data/           # fs.pickle (fake filesystem metadata), txtcmds/
├── test/           # Unit tests + test helpers
└── vendor/         # Vendored third-party code (excluded from linting/typing)
```

## Key Architectural Patterns

### Commands (`src/cowrie/commands/`)
- Each command subclasses `HoneyPotCommand` from `cowrie.shell.command`
- Override `call()` for synchronous commands; override `start()` for interactive/async ones
- Register commands in the module-level `commands: dict[str, Callable]` dict
- Multiple paths map to the same class: `commands["/bin/echo"] = commands["echo"] = Command_echo`

### Output Plugins (`src/cowrie/output/`)
- Each plugin subclasses `cowrie.core.output.Output` (abstract base)
- Must implement three methods: `start()`, `stop()`, `write(event: dict)`
- `emit()` in the base class handles session tracking, timestamps, and protocol tagging before calling `write()`
- Read plugin config via `CowrieConfig.get("output_<pluginname>", "key")`
- Event IDs follow the pattern `cowrie.<category>.<action>` (e.g. `cowrie.login.success`, `cowrie.session.connect`)

### Configuration
- Always use the `CowrieConfig` singleton from `cowrie.core.config`
- Use `fallback=` parameter on all `.get()` / `.getboolean()` calls to avoid hard failures

### Tests (`src/cowrie/test/`)
- Use `unittest.TestCase`; discovered via `unittest discover src`
- Tests use `FakeAvatar`, `FakeServer`, `FakeTransport` from `cowrie.test.fake_*`
- Set required env vars at module level: `COWRIE_HONEYPOT_DATA_PATH`, `COWRIE_SHELL_FILESYSTEM`, etc.
- Test class setup: `setUpClass` calls `proto.makeConnection(tr)`; `setUp` calls `tr.clear()`
- Send input via `proto.lineReceived(b"command\n")`, assert on `tr.value()`

## Runtime Directories (created at runtime, not in repo)
```
var/log/cowrie/     # cowrie.log, cowrie.json
var/lib/cowrie/tty/ # Session tty logs
var/lib/cowrie/downloads/ # Attacker-uploaded files
```

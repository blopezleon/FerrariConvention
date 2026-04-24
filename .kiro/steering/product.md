# Cowrie — SSH/Telnet Honeypot

Cowrie is a medium-to-high interaction SSH and Telnet honeypot designed to log brute force attacks and attacker shell interactions. It operates in three modes:

- **Shell emulation (default)**: Emulates a UNIX system in Python with a fake filesystem, fake file contents, and simulated commands. Attackers interact with a convincing but sandboxed environment.
- **Proxy mode**: Acts as an SSH/Telnet proxy to a real or QEMU-managed backend system, forwarding attacker sessions for observation.
- **LLM mode (experimental)**: Uses large language models (e.g., OpenAI GPT) to dynamically generate realistic shell responses for any command.

Key capabilities:
- Logs all attacker commands, keystrokes, and file transfers (wget/curl downloads, SFTP/SCP uploads)
- Session replay via `playlog` utility (UML-compatible tty logs)
- Extensible output plugin system supporting JSON, databases, SIEMs, and threat intel feeds
- Configurable fake filesystem (`fs.pickle`) and fake file contents (`honeyfs/`)
- Supports SSH exec commands, direct-tcp connections, and TFTP

Primary audience: Security researchers and system administrators deploying deception infrastructure.

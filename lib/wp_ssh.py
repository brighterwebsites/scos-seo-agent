"""
Shared SSH and WordPress helpers for scos-gather scripts.

All scripts that SSH into a WordPress server should import from here rather
than re-implementing connection and WP-CLI execution logic.
"""

import os
import re
import sys
from pathlib import Path

try:
    import paramiko
except ImportError:
    sys.exit("ERROR: paramiko not installed. Run: pip install -r requirements.txt")

try:
    from dotenv import load_dotenv
except ImportError:
    sys.exit("ERROR: python-dotenv not installed. Run: pip install -r requirements.txt")

# Repo root is one level above this file (lib/)
_REPO_ROOT = Path(__file__).parent.parent


def load_env() -> dict:
    env_path = _REPO_ROOT / ".env"
    load_dotenv(dotenv_path=env_path)
    required = ["SSH_HOST", "SSH_USER", "SSH_KEY_PATH", "WP_PATH"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        sys.exit(f"ERROR: .env missing required key(s): {', '.join(missing)}\n"
                 f"Expected .env at: {env_path}")
    return {k: os.environ[k] for k in required}


def parse_claude_md(site: str) -> dict:
    # Default Windows path; override via SCOS_BASE_DIR env var for dev/CI
    base_dir = os.environ.get(
        "SCOS_BASE_DIR",
        rf"C:\Users\vanes\Desktop\seo-command-center"
    )
    md_path = Path(base_dir) / site / "CLAUDE.md"
    if not md_path.exists():
        sys.exit(f"ERROR: CLAUDE.md not found at: {md_path}")

    text = md_path.read_text(encoding="utf-8")

    def extract(field: str) -> str:
        match = re.search(
            rf"(?:^|\n)\s*[-*]?\s*{re.escape(field)}\s*[:\-]\s*(.+)",
            text,
            re.IGNORECASE,
        )
        return match.group(1).strip() if match else ""

    target_wp = extract("target-wordpress-domain")
    prod_domain = extract("production-domain")
    staging_raw = extract("staging-mode")

    if not target_wp:
        sys.exit(f"ERROR: 'target-wordpress-domain' not found in {md_path}")
    if not prod_domain:
        sys.exit(f"ERROR: 'production-domain' not found in {md_path}")

    staging_mode = staging_raw.lower() in ("true", "yes", "1") if staging_raw else False

    return {
        "target_wordpress_domain": target_wp,
        "production_domain": prod_domain,
        "staging_mode": staging_mode,
        "base_dir": str(base_dir),
    }


def ssh_connect(env: dict) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    key_path = Path(env["SSH_KEY_PATH"]).expanduser()
    if not key_path.exists():
        sys.exit(f"ERROR: SSH key not found at: {key_path}")
    try:
        pkey = paramiko.RSAKey.from_private_key_file(str(key_path))
    except paramiko.ssh_exception.SSHException:
        try:
            pkey = paramiko.Ed25519Key.from_private_key_file(str(key_path))
        except Exception as e:
            sys.exit(f"ERROR: Could not load SSH key {key_path}: {e}")

    try:
        client.connect(
            hostname=env["SSH_HOST"],
            username=env["SSH_USER"],
            pkey=pkey,
            timeout=30,
        )
    except paramiko.AuthenticationException:
        sys.exit(f"ERROR: SSH authentication failed for {env['SSH_USER']}@{env['SSH_HOST']}")
    except paramiko.ssh_exception.NoValidConnectionsError as e:
        sys.exit(f"ERROR: SSH connection failed to {env['SSH_HOST']}: {e}")
    except Exception as e:
        sys.exit(f"ERROR: SSH connection error ({env['SSH_HOST']}): {e}")

    return client


def wp(client: paramiko.SSHClient, wp_path: str, cmd: str) -> str:
    full_cmd = f"wp --path={wp_path} {cmd}"
    _, stdout, stderr = client.exec_command(full_cmd)
    out = stdout.read().decode("utf-8").strip()
    err = stderr.read().decode("utf-8").strip()
    if "command not found" in err.lower() or "wp: not found" in err.lower():
        sys.exit(f"ERROR: WP-CLI not found on remote server. Tried: {full_cmd}")
    return out

#!/usr/bin/env python3
"""
A-LEMS Tunnel Agent  —  Cloudflare Named Tunnel (permanent URL)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PERMANENT URL: once created, the tunnel URL never changes.
Share it once by email. Works every time you run this script.
Zero branding. Zero cost. Zero logos. Clean for research.

HOW IT WORKS:
  - cloudflared creates a named tunnel with a UUID
  - UUID maps to a permanent *.cfargotunnel.com URL
  - OR map it to your own domain (e.g. alems.yourdomain.com)
  - When online:  full live execution + telemetry
  - When offline: dashboard falls back to offline analysis mode

FIRST TIME SETUP (~3 min):
  1. Install cloudflared:
       wget -q https://github.com/cloudflare/cloudflared/releases/latest/\
download/cloudflared-linux-amd64.deb
       sudo dpkg -i cloudflared-linux-amd64.deb

  2. Login (free Cloudflare account):
       cloudflared tunnel login
       # Opens browser → authorise → saves cert to ~/.cloudflared/cert.pem

  3. Create your permanent named tunnel:
       cloudflared tunnel create a-lems
       # Prints tunnel UUID — save it
       # Creates ~/.cloudflared/<UUID>.json (credentials file)

  4. Edit config/tunnel.yaml:
       tunnel_name: "a-lems"
       tunnel_uuid: "paste-your-UUID-here"
       token:       "choose-any-secret-passphrase"

  5. Run:  python tunnel_agent.py
       → Prints your permanent URL
       → Share once by email — done forever

USAGE:
  source venv/bin/activate
  python tunnel_agent.py

  # Background (survives terminal close):
  nohup python tunnel_agent.py > logs/tunnel.log 2>&1 &
  tail -f logs/tunnel.log
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import os, sys, time, signal, subprocess, threading, json
from pathlib import Path

try:
    import yaml as _yaml
    _YAML_OK = True
except ImportError:
    _YAML_OK = False

ROOT       = Path(__file__).parent
CFG_FILE   = ROOT / "config" / "tunnel.yaml"
STATE_FILE = ROOT / ".tunnel_state.json"
LOG_DIR    = ROOT / "logs"
PORT       = int(os.environ.get("ALEMS_PORT", 8765))

BANNER = """
╔══════════════════════════════════════════════════════════════╗
║        ⚡  A-LEMS Tunnel Agent  (Cloudflare permanent)      ║
╚══════════════════════════════════════════════════════════════╝"""


# ── Config ────────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    if not CFG_FILE.exists():
        _write_template()
        print(f"""
  ⚠️  Created config template at: {CFG_FILE}

  Complete the 3 steps in the file, then run this script again.
  Takes about 3 minutes the first time.
""")
        sys.exit(0)
    if not _YAML_OK:
        print("  pip install pyyaml")
        sys.exit(1)
    with open(CFG_FILE) as f:
        cfg = _yaml.safe_load(f)
    _validate(cfg)
    return cfg


def _write_template():
    CFG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CFG_FILE.write_text("""\
# A-LEMS Tunnel Configuration
# ════════════════════════════════════════════════════════════
# STEP 1 — Install cloudflared + login (one time):
#   sudo dpkg -i cloudflared-linux-amd64.deb
#   cloudflared tunnel login
#
# STEP 2 — Create your permanent named tunnel (one time):
#   cloudflared tunnel create a-lems
#   → note the UUID it prints
#
# STEP 3 — Fill in values below and save:
# ════════════════════════════════════════════════════════════

tunnel_name: "a-lems"
tunnel_uuid: "PASTE-UUID-FROM-STEP-2-HERE"

# Shared access token — researchers enter this in the sidebar
# Choose anything memorable, or generate one:
#   python -c "import secrets; print('alems-'+secrets.token_urlsafe(12))"
token: "alems-choose-a-passphrase"

# Optional: map to your own domain instead of *.cfargotunnel.com
# Leave blank to use the default permanent cfargotunnel.com URL
# Example: "alems.yourlabname.com"  (requires domain in Cloudflare DNS)
custom_hostname: ""
""")


def _validate(cfg: dict):
    errs = []
    if not cfg.get("tunnel_uuid") or "PASTE-UUID" in str(cfg.get("tunnel_uuid", "")):
        errs.append("tunnel_uuid not set  →  run: cloudflared tunnel create a-lems")
    if not cfg.get("tunnel_name"):
        errs.append("tunnel_name not set")
    if not cfg.get("token") or cfg.get("token") == "alems-choose-a-passphrase":
        errs.append("token not set  →  choose a passphrase")
    if errs:
        print("\n  ❌  config/tunnel.yaml is incomplete:")
        for e in errs:
            print(f"     • {e}")
        print(f"\n  Edit: {CFG_FILE}\n")
        sys.exit(1)


# ── State file (Streamlit reads this) ─────────────────────────────────────────

def _write_state(url: str, token: str, online: bool):
    LOG_DIR.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps({
        "online": online, "url": url,
        "token": token, "ts": time.time(),
    }))


def _clear_state():
    STATE_FILE.write_text(json.dumps({"online": False, "url": "", "token": ""}))


# ── FastAPI server ─────────────────────────────────────────────────────────────

def _start_server(token: str) -> subprocess.Popen:
    env = {**os.environ, "ALEMS_TOKEN": token, "ALEMS_LIVE_MODE": "1"}
    cmd = [sys.executable, "-m", "uvicorn", "server:app",
           "--host", "127.0.0.1", "--port", str(PORT),
           "--log-level", "warning"]
    print(f"  Starting FastAPI server on port {PORT}...")
    proc = subprocess.Popen(cmd, cwd=str(ROOT), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    time.sleep(2)
    if proc.poll() is not None:
        out, _ = proc.communicate()
        print(f"  ❌ Server failed:\n{out.decode()}")
        sys.exit(1)
    print(f"  ✅ Server running (pid {proc.pid})")
    return proc


# ── Cloudflare tunnel ─────────────────────────────────────────────────────────

def _build_tunnel_url(cfg: dict) -> str:
    custom = cfg.get("custom_hostname", "").strip()
    if custom:
        return f"https://{custom}"
    return f"https://{cfg['tunnel_uuid']}.cfargotunnel.com"


def _write_cloudflared_config(cfg: dict):
    """Write .cloudflared/config.yml for cloudflared to use."""
    cred_file = Path.home() / ".cloudflared" / f"{cfg['tunnel_uuid']}.json"
    if not cred_file.exists():
        print(f"""
  ❌  Credentials file not found: {cred_file}
     Run once:  cloudflared tunnel create {cfg['tunnel_name']}
""")
        sys.exit(1)

    ingress = [{"service": f"http://127.0.0.1:{PORT}"}]
    custom = cfg.get("custom_hostname", "").strip()
    if custom:
        ingress = [
            {"hostname": custom, "service": f"http://127.0.0.1:{PORT}"},
            {"service": "http_status:404"},
        ]

    cf_cfg = {
        "tunnel":          cfg["tunnel_uuid"],
        "credentials-file": str(cred_file),
        "ingress":         ingress,
    }
    cf_cfg_path = ROOT / ".cloudflared" / "config.yml"
    cf_cfg_path.parent.mkdir(exist_ok=True)

    try:
        import yaml as _y
        with open(cf_cfg_path, "w") as f:
            _y.dump(cf_cfg, f, default_flow_style=False)
    except Exception:
        # Fallback: write manually
        ingress_yaml = "\n".join(
            f"  - hostname: {r.get('hostname', '')}\n    service: {r['service']}"
            if "hostname" in r else f"  - service: {r['service']}"
            for r in ingress
        )
        cf_cfg_path.write_text(
            f"tunnel: {cfg['tunnel_uuid']}\n"
            f"credentials-file: {cred_file}\n"
            f"ingress:\n{ingress_yaml}\n"
        )
    return str(cf_cfg_path)


def _start_tunnel(cfg_path: str) -> subprocess.Popen:
    cmd = ["cloudflared", "tunnel", "--config", cfg_path,
           "--no-autoupdate", "run"]
    print("  Starting Cloudflare tunnel...")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    # Wait for "connection registered" in output
    deadline = time.time() + 25
    ready = False
    for line in proc.stdout:
        if time.time() > deadline:
            break
        if any(k in line.lower() for k in
               ["connection registered", "registered tunnel", "connected"]):
            ready = True
            break
        if "error" in line.lower() and "warn" not in line.lower():
            print(f"  Tunnel: {line.strip()}")

    # Drain in background
    threading.Thread(
        target=lambda p: [_ for _ in p.stdout],
        args=(proc,), daemon=True
    ).start()

    if proc.poll() is not None:
        print("  ❌  cloudflared exited — check credentials and tunnel name.")
        sys.exit(1)
    if not ready:
        print("  ⚠️  Tunnel may still be starting (no 'connected' line seen yet)...")
    return proc


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(BANNER)

    # Check cloudflared installed
    if subprocess.run(["which", "cloudflared"],
                      capture_output=True).returncode != 0:
        print("""
  ❌  cloudflared not installed.

  Install on Ubuntu:
    wget -q https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
    sudo dpkg -i cloudflared-linux-amd64.deb

  Then create a free Cloudflare account and run:
    cloudflared tunnel login
    cloudflared tunnel create a-lems
""")
        sys.exit(1)

    cfg   = _load_config()
    token = cfg["token"]
    url   = _build_tunnel_url(cfg)

    LOG_DIR.mkdir(exist_ok=True)
    print(f"\n  Tunnel URL  : {url}")
    print(f"  Token       : {token}")
    print(f"  Port        : {PORT}\n")

    srv      = _start_server(token)
    cfg_path = _write_cloudflared_config(cfg)
    tun      = _start_tunnel(cfg_path)

    _write_state(url, token, True)

    print(f"""
╔══════════════════════════════════════════════════════════════╗
║  🟢  A-LEMS is LIVE                                         ║
║                                                              ║
║  Permanent URL  →  {url:<42}║
║  Token          →  {token:<42}║
║                                                              ║
║  Share these once by email.                                 ║
║  URL never changes. Works every time you run this script.   ║
║                                                              ║
║  Ctrl+C to go offline.                                      ║
╚══════════════════════════════════════════════════════════════╝
""")

    # Graceful shutdown
    def _shutdown(sig, frame):
        print("\n  Shutting down...")
        _clear_state()
        tun.terminate()
        srv.terminate()
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Heartbeat — auto-restart if either process dies
    while True:
        time.sleep(30)
        ts = time.strftime("%H:%M:%S")
        restarted = False
        if srv.poll() is not None:
            print(f"  [{ts}]  ⚠️  Server died — restarting...")
            srv = _start_server(token)
            restarted = True
        if tun.poll() is not None:
            print(f"  [{ts}]  ⚠️  Tunnel died — restarting...")
            tun = _start_tunnel(cfg_path)
            restarted = True
        if restarted:
            _write_state(url, token, True)
        print(f"  [{ts}]  🟢  {url}")


if __name__ == "__main__":
    main()

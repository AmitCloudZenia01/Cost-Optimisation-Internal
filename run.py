#!/usr/bin/env python3
"""
CloudZenia — AWS Cost Optimization Report
Run: python3 run.py

Asks for credentials at runtime.
Creates temp files during the run, deletes them when done.
Nothing is stored on disk after the run.
"""

import sys, os, subprocess, shutil, tempfile, atexit, getpass, webbrowser
from pathlib import Path

BASE = Path(__file__).parent

# ─── Keep track of every temp file we create so we can delete them ───────────
#
# These live in a private directory under the system temp dir, NOT in the
# project folder. tempfile names them tmpXXXXXXXX.json, which the repo's
# .gitignore credential patterns do not match — a service-account key left
# behind by a hard kill (atexit does not run on SIGKILL) could otherwise be
# committed. mkdtemp() is created 0700, so it is also not world-readable.
_TMPDIR = tempfile.mkdtemp(prefix="cost-report-")
_tmp = []

def _cleanup():
    for f in _tmp:
        try:
            os.unlink(f)
        except Exception:
            pass
    try:
        shutil.rmtree(_TMPDIR, ignore_errors=True)
    except Exception:
        pass

atexit.register(_cleanup)   # runs even on crash or Ctrl-C

# ─── Colours (work on Mac, Linux, Windows 10+) ───────────────────────────────
if os.name == "nt":
    os.system("")   # enable ANSI on Windows
BOLD  = "\033[1m"
GREEN = "\033[92m"
BLUE  = "\033[94m"
CYAN  = "\033[96m"
YELLOW= "\033[93m"
RED   = "\033[91m"
DIM   = "\033[2m"
RESET = "\033[0m"

def b(t): return f"{BOLD}{t}{RESET}"
def g(t): return f"{GREEN}{BOLD}{t}{RESET}"
def bl(t): return f"{BLUE}{t}{RESET}"
def dim(t): return f"{DIM}{t}{RESET}"

# ─── Install missing packages automatically ───────────────────────────────────
def install_packages():
    req = BASE / "requirements.txt"
    if not req.exists():
        return
    try:
        # Check every top-level package the run actually needs. Testing only
        # boto3/gspread/yaml/rich meant a missing googleapiclient slipped
        # through and the run died at sheet-creation time instead.
        import boto3, gspread, yaml, rich          # noqa
        import googleapiclient, google.oauth2      # noqa
        import google_auth_oauthlib                # noqa
        return                                     # all good — skip silently
    except ImportError:
        pass
    print(dim("  Installing required packages (one-time, ~1 min)…"))
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "-r", str(req), "-q"],
            stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "-r", str(req), "-q", "--user"],
            stderr=subprocess.DEVNULL)
    print(g("  ✓ Packages installed."))

# ─── Helper: ask the user a question ─────────────────────────────────────────
def ask(label, default=None, secret=False, required=True):
    hint = f"  [{dim(str(default))}]" if default is not None else ""
    prompt = f"  {CYAN}▸{RESET} {label}{hint}: "
    while True:
        try:
            val = getpass.getpass(prompt) if secret else input(prompt)
        except (KeyboardInterrupt, EOFError):
            print(); sys.exit(0)
        val = val.strip()
        if not val and default is not None:
            return str(default)
        if val:
            return val
        if not required:
            return ""
        print(f"  {YELLOW}This field is required.{RESET}")

def pick(label, options, default=1):
    print(f"\n  {CYAN}▸{RESET} {label}")
    for i, o in enumerate(options, 1):
        print(f"  {b(f'  [{i}]')}  {o}")
    while True:
        raw = ask(f"Enter number", str(default))
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(options):
                return idx
        except ValueError:
            pass
        print(f"  {YELLOW}Please enter a number between 1 and {len(options)}.{RESET}")

def divider(title=""):
    line = "─" * 52
    if title:
        print(f"\n  {b(CYAN + '┌' + line + '┐' + RESET)}")
        print(f"  {b(CYAN + '│' + RESET)}  {b(title)}")
        print(f"  {b(CYAN + '└' + line + '┘' + RESET)}")
    else:
        print(f"\n  {DIM}{line}{RESET}")

# ─── Collect AWS credentials ──────────────────────────────────────────────────
def get_aws():
    divider("1 of 3 — AWS Credentials")
    mode = pick(
        "How should we connect to your AWS account?",
        [
            "Use my existing AWS CLI setup  "
            + dim("(if you already run 'aws' commands on this machine)"),
            "Enter Access Key & Secret Key  "
            + dim("(from AWS Console → IAM → Users → Security credentials)"),
            "Assume an IAM Role             "
            + dim("(for cross-account access)"),
        ],
        default=1,
    )
    if mode == 0:
        profile = ask("AWS profile name", "default")
        return {"mode": "profile", "profile": None if profile == "default" else profile}
    elif mode == 1:
        print(f"\n  {DIM}Find these in AWS Console → IAM → Users → your user → Security credentials{RESET}")
        key    = ask("Access Key ID  (starts with AKIA…)")
        secret = ask("Secret Access Key", secret=True)
        token  = ask("Session Token", required=False)
        return {"mode": "keys", "access_key": key,
                "secret_key": secret, "session_token": token or None}
    else:
        arn = ask("IAM Role ARN  (arn:aws:iam::ACCOUNT:role/NAME)")
        return {"mode": "role", "role_arn": arn}

# ─── Collect GCP credentials ──────────────────────────────────────────────────
def get_gcp():
    divider("2 of 3 — Google Sheets Credentials")
    print(f"\n  {DIM}You need a Google Cloud credentials JSON file.{RESET}")
    print(f"  {DIM}Get it: console.cloud.google.com → APIs → Credentials → Create{RESET}\n")
    while True:
        raw  = ask("Path to your GCP credentials .json file\n  "
                   + dim("  Tip: drag the file into this window, then press Enter"))
        path = Path(raw.strip().strip("'\""))
        if path.exists() and path.suffix.lower() == ".json":
            # Copy to temp — original stays untouched, temp is deleted after run
            tf = tempfile.NamedTemporaryFile(suffix=".json", delete=False, dir=_TMPDIR)
            shutil.copy2(path, tf.name)
            tf.close()
            os.chmod(tf.name, 0o600)
            _tmp.append(tf.name)
            print(g("  ✓  Credentials loaded."))
            break
        print(f"  {YELLOW}File not found or not a .json file — please try again.{RESET}")
    email = ask("Share the report with this email", required=False)
    return {"path": tf.name, "email": email}

# ─── Collect settings ─────────────────────────────────────────────────────────
REGIONS = [
    "us-east-1","us-east-2","us-west-1","us-west-2",
    "ap-south-1","ap-southeast-1","ap-southeast-2","ap-northeast-1",
    "eu-west-1","eu-west-2","eu-central-1","sa-east-1","ca-central-1",
]

def get_settings():
    divider("3 of 3 — Settings")
    print()
    cols = 2
    for i in range(0, len(REGIONS), cols):
        row = REGIONS[i:i+cols]
        line = "".join(f"    {dim(str(i+j+1).rjust(2) + '.')} {r:<22}" for j, r in enumerate(row))
        print(line)
    print()
    raw = ask("Regions to scan (comma-separated numbers, e.g. 1,4)", "1")
    try:
        idxs    = [int(x.strip()) - 1 for x in raw.split(",")]
        regions = [REGIONS[i] for i in idxs if 0 <= i < len(REGIONS)]
    except Exception:
        regions = []
    if not regions:
        regions = ["us-east-1"]
    print(g(f"  ✓  Regions: {', '.join(regions)}"))
    days = ask("Days of cost history to analyse (30 / 60 / 90)", "90")
    try:
        days = int(days)
    except ValueError:
        days = 90
    return {"regions": regions, "days": days}

# ─── Run the report ───────────────────────────────────────────────────────────
def run(aws, gcp, settings):
    import yaml

    cfg = {
        "aws": {"regions": settings["regions"], "account_id": "",
                "cost_history_days": settings["days"]},
        # Matches config.yaml, so the 15d/60d columns on the EC2 and RDS tabs
        # are actually populated.
        "metrics": {"periods": [7, 15, 30, 60, 90],
                    "spike_multiplier": 2.5, "min_datapoints": 100},
        "pricing": {"vantage_token": ""},   # resolved from env / .vantage_token
        "recommendations": {
            "phase1": {"cpu_max_avg": 40, "memory_max_avg": 50, "network_max_mbps": 100},
            "phase2": {"graviton_eligible_families": ["t3","m5","c5","r5","m6i","c6i","r6i"],
                       "min_monthly_cost_for_ri": 200},
        },
        "google_sheets": {"sheet_name_format": "{account_id} AWS Cost Report {date}",
                          "share_with": gcp["email"]},
        "snapshots": {"directory": str(BASE / "snapshots"), "keep_last": 10},
    }

    # Write temp config — deleted on exit
    tf = tempfile.NamedTemporaryFile(suffix=".yaml", delete=False, mode="w", dir=_TMPDIR)
    yaml.dump(cfg, tf); tf.close(); _tmp.append(tf.name)

    result = {}

    def log(msg):
        if msg[0] == "step":
            n, total, text = msg[1], msg[2], msg[3]
            bar = "█" * n + "░" * (total - n)
            print(f"\n  {DIM}[{bar}]{RESET}  {b(CYAN + f'Step {n}/{total}' + RESET)}  {text}")
        elif msg[0] == "info":
            print(f"    {dim('↳')}  {msg[1]}")
        elif msg[0] == "savings":
            print(f"\n  {g('Phase 1 savings:')}  ${msg[1]:,.2f}/mo   "
                  f"{bl('Phase 2 savings:')}  ${msg[2]:,.2f}/mo")
        elif msg[0] == "done":
            result.update(url=msg[1], p1=msg[2], p2=msg[3], resources=msg[4])

    import main as M
    M.run(
        config_path       = tf.name,
        gcp_sa_path       = gcp["path"],
        aws_profile       = aws.get("profile"),
        role_arn          = aws.get("role_arn"),
        aws_access_key    = aws.get("access_key"),
        aws_secret_key    = aws.get("secret_key"),
        aws_session_token = aws.get("session_token"),
        log_fn            = log,
    )

    # ── Show result ───────────────────────────────────────────
    if result.get("url"):
        url, p1, p2 = result["url"], result.get("p1",0), result.get("p2",0)
        res = result.get("resources", 0)
        print()
        print(g("  ╔═══════════════════════════════════════════════════╗"))
        print(g("  ║           ✅  Your Report is Ready!               ║"))
        print(g("  ╚═══════════════════════════════════════════════════╝"))
        print()
        print(f"  {b('Open here:')}  {bl(url)}")
        print()
        print(f"  {g('Phase 1:')}  ${p1:,.2f}/mo    {bl('Phase 2:')}  ${p2:,.2f}/mo")
        print(f"  {b('Total:')}    ${p1+p2:,.2f}/mo   across {res} resources")
        print()
        try:
            webbrowser.open(url)
            print(dim("  (Report opened in your browser)"))
        except Exception:
            pass
    # Temp files are deleted automatically by atexit

# ─── Entry point ──────────────────────────────────────────────────────────────
def main():
    print()
    print(f"  {b(CYAN)}╔══════════════════════════════════════════════════╗{RESET}")
    print(f"  {b(CYAN)}║  CloudZenia — AWS Cost Optimization Report       ║{RESET}")
    print(f"  {b(CYAN)}╚══════════════════════════════════════════════════╝{RESET}")
    print()

    install_packages()

    print(f"  {DIM}Answer a few questions and we'll generate your report.{RESET}")
    print(f"  {DIM}Credentials are used for this run only — nothing is saved.{RESET}")

    aws      = get_aws()
    gcp      = get_gcp()
    settings = get_settings()

    print()
    divider("Generating Report  (3–5 minutes)")
    print(f"  {DIM}Keep this window open…{RESET}")

    try:
        run(aws, gcp, settings)
    except KeyboardInterrupt:
        print(f"\n  {YELLOW}Cancelled.{RESET}\n")
    except Exception as e:
        print(f"\n  {RED}{b('Error:')} {e}{RESET}")
        print(f"\n  {YELLOW}Common fixes:{RESET}")
        print("    • AWS credentials need ReadOnlyAccess + ce:GetCostAndUsage permission")
        print("    • GCP JSON needs Google Sheets API and Drive API enabled")
        print("    • Check the AWS regions are correct\n")
        sys.exit(1)

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Abhyas — One-command laptop setup.
Run:  python setup.py
or:   python3 setup.py

What it does:
  1. Creates a virtual environment (venv/)
  2. Installs LOCAL dependencies (requirements-local.txt — no Docker/Postgres/Nidhi)
  3. Copies .env.example -> .env (if missing); sets ENVIRONMENT=local (SQLite)
  4. Runs migrations (SQLite)
  5. Creates a superuser (optional)
  6. Starts the dev server at http://127.0.0.1:8000

Laptop compatibility:
  Abhyas supports ENVIRONMENT=local (see pyqproject/settings.py:
  local_mode = ENVIRONMENT == local -> uses SQLite, no Nidhi/Postgres).
  This script boots that mode so a developer can run the whole app with
  zero container/Docker/DB-server setup.
"""

import os
import subprocess
import sys
import shutil
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
VENV_DIR = BASE_DIR / "venv"
REQ_FILE = BASE_DIR / "requirements-local.txt"
ENV_EXAMPLE = BASE_DIR / ".env.example"
ENV_FILE = BASE_DIR / ".env"


def run(cmd, check=True, **kwargs):
    """Run a shell command and print it."""
    print(f"  > {cmd}")
    result = subprocess.run(cmd, shell=True, cwd=str(BASE_DIR), **kwargs)
    if check and result.returncode != 0:
        print(f"\n❌ Command failed (exit {result.returncode}): {cmd}")
        sys.exit(1)
    return result


def find_python():
    """Find a usable Python 3 interpreter (3.10+)."""
    for cmd in ["python3", "python"]:
        result = subprocess.run(
            [cmd, "--version"], capture_output=True, text=True
        )
        if result.returncode == 0 and "Python 3" in result.stdout:
            version = result.stdout.strip().split()[1]
            major, minor = map(int, version.split(".")[:2])
            if major == 3 and minor >= 10:
                return cmd
    print("❌ Python 3.10+ is required. Install it from https://python.org")
    sys.exit(1)


def main():
    print("=" * 50)
    print("  Abhyas — Laptop Setup (ENVIRONMENT=local)")
    print("=" * 50)

    # ── Step 0: Check Python ──
    py = find_python()
    result = subprocess.run([py, "--version"], capture_output=True, text=True)
    print(f"\n✅ {result.stdout.strip()}")

    # ── Step 1: Create venv ──
    if not VENV_DIR.exists():
        print("\n📦 Creating virtual environment...")
        run(f"{py} -m venv venv")
    else:
        print("\n📦 Virtual environment already exists.")

    # Determine venv python/pip paths
    if sys.platform == "win32":
        venv_python = VENV_DIR / "Scripts" / "python.exe"
        venv_pip = VENV_DIR / "Scripts" / "pip.exe"
    else:
        venv_python = VENV_DIR / "bin" / "python"
        venv_pip = VENV_DIR / "bin" / "pip"

    # ── Step 2: Install dependencies (LOCAL only) ──
    print("\n📥 Installing local dependencies...")
    run(f"{venv_pip} install --upgrade pip --quiet")
    run(f"{venv_pip} install -r {REQ_FILE}")

    # ── Step 3: Copy .env (force ENVIRONMENT=local) ──
    if not ENV_FILE.exists():
        if ENV_EXAMPLE.exists():
            print("\n📝 Creating .env from .env.example (forcing local mode)...")
            content = ENV_EXAMPLE.read_text()
            # Ensure laptop/local mode is active
            if "ENVIRONMENT" not in content:
                content += "\nENVIRONMENT=local\n"
            else:
                content = "\n".join(
                    ln for ln in content.splitlines()
                    if not ln.strip().startswith("ENVIRONMENT")
                )
                content += "\nENVIRONMENT=local\n"
            ENV_FILE.write_text(content)
            print("   Edit .env to change settings if needed.")
        else:
            print("\n📝 Creating default .env (local mode)...")
            ENV_FILE.write_text(
                "ENVIRONMENT=local\n"
                "DEBUG=True\n"
                "SECRET_KEY=local-dev-only-not-for-production\n"
                "ALLOWED_HOSTS=localhost,127.0.0.1\n"
            )
    else:
        print("\n📝 .env already exists, skipping.")
        # Make sure it is NOT pointing at production/Nidhi
        content = ENV_FILE.read_text()
        if "ENVIRONMENT=production" in content:
            print("   ⚠️  .env has ENVIRONMENT=production — edit it to local for laptop use.")

    # ── Step 4: Run migrations (SQLite) ──
    print("\n🗄️  Running migrations...")
    run(f"{venv_python} manage.py migrate")

    # ── Step 5: Create superuser ──
    print("\n👤 Superuser setup (Ctrl+C to skip)...")
    try:
        run(f"{venv_python} manage.py createsuperuser", check=False)
    except (KeyboardInterrupt, SystemExit):
        print("\n   Skipped superuser creation.")

    # ── Step 6: Start server ──
    print("\n" + "=" * 50)
    print("  ✅ Setup complete!")
    print("  🌐 Starting server at http://127.0.0.1:8000")
    print("  🔑 Login with your superuser credentials")
    print("  ⏹  Press Ctrl+C to stop")
    print("=" * 50 + "\n")

    os.execv(
        str(venv_python),
        [str(venv_python), "manage.py", "runserver"],
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n👋 Setup cancelled.")

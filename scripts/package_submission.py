"""
package_submission.py — Clean Submission Packaging Script with Security Gate
===========================================================================
Creates a clean, production-grade submission ZIP archive for hackathon evaluation.

Enforces:
  1. Strict Allow-List: Only packages core code, tests, assets, data, and .env.example.
  2. Strict Block-List: Excludes .env, .venv, .git, .pytest_cache, __pycache__, logs, databases.
  3. Security Gate: Aborts and fails if any active secret (Gemini, OpenAI, Razorpay, Twilio)
     is found within any packaged file.
"""

import os
import re
import sys
import zipfile
from pathlib import Path

# Ensure console output supports UTF-8 on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Paths
WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_ZIP_PATH = WORKSPACE_ROOT.parent / "ai-revenue-recovery-agent.zip"
ARCHIVE_PREFIX = "ai-revenue-recovery-agent"

# Explicit include directories (relative to WORKSPACE_ROOT)
INCLUDE_DIRS = [
    "src",
    "api",
    "dashboard",
    "tests",
    "data",
    "assets",
    "scripts",
]

# Explicit include top-level files (relative to WORKSPACE_ROOT)
INCLUDE_FILES = [
    "benchmark.py",
    "demo.py",
    "qr_demo.py",
    "setu_demo.py",
    "upi_demo.py",
    "test_inbound_demo.py",
    "requirements.txt",
    ".env.example",
    "README.md",
    "LICENSE",
]

# Strict blacklist patterns
BLACKLIST_NAMES = {
    ".env",
    ".env.local",
    ".git",
    ".venv",
    "venv",
    ".pytest_cache",
    "__pycache__",
    ".coverage",
    "htmlcov",
    "archive",
    "scratch",
}

BLACKLIST_EXTENSIONS = {
    ".pyc",
    ".pyo",
    ".pyd",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".log",
}

# Secret scanner patterns (Real secrets that must NEVER be in submission files)
SECRET_PATTERNS = [
    (re.compile(r"GEMINI_API_KEY\s*=\s*(AIzaSy[A-Za-z0-9_-]{33})", re.IGNORECASE), "Live Gemini API Key"),
    (re.compile(r"OPENAI_API_KEY\s*=\s*(sk-[A-Za-z0-9_-]{20,})", re.IGNORECASE), "Live OpenAI API Key"),
    (re.compile(r"RAZORPAY_KEY_SECRET\s*=\s*([a-zA-Z0-9]{16,})", re.IGNORECASE), "Live Razorpay Secret"),
    (re.compile(r"TWILIO_AUTH_TOKEN\s*=\s*([a-f0-9]{32})", re.IGNORECASE), "Live Twilio Auth Token"),
    (re.compile(r"AIzaSy[A-Za-z0-9_-]{33}"), "Raw Google API Key Pattern"),
]

# Safe placeholder values that are allowed
ALLOWED_PLACEHOLDERS = {
    "your_key_secret_here",
    "your_webhook_secret_here",
    "sk-your-key-here",
    "sk-proj-your-openai-key",
    "your_gemini_api_key_here",
    "rzp_live_xxxxxxxxxxxx",
}


def is_placeholder(val: str) -> bool:
    v = val.strip().lower()
    if v in ALLOWED_PLACEHOLDERS:
        return True
    if any(p in v for p in ["your-key", "your-openai", "your-gemini", "dummy", "placeholder", "xxx"]):
        return True
    return False


def scan_file_for_secrets(file_path: Path) -> list[str]:
    """Scans text files for potential leaked API credentials."""
    # Skip binary files
    if file_path.suffix.lower() in {".png", ".jpg", ".jpeg", ".ico", ".webp", ".gif", ".mp3", ".wav"}:
        return []

    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []

    violations = []
    for pattern, description in SECRET_PATTERNS:
        matches = pattern.findall(content)
        for m in matches:
            val = m if isinstance(m, str) else m[0]
            if val.strip() and not is_placeholder(val):
                violations.append(f"{description} found in {file_path.name}: {val[:6]}***")

    return violations


def collect_submission_files() -> list[Path]:
    """Gathers all files that qualify for packaging."""
    files_to_pack = []

    # 1. Add top-level files
    for fname in INCLUDE_FILES:
        fpath = WORKSPACE_ROOT / fname
        if fpath.is_file():
            files_to_pack.append(fpath)
        else:
            print(f"[!] Warning: Top-level file missing: {fname}")

    # 2. Add files in included directories
    for dname in INCLUDE_DIRS:
        dpath = WORKSPACE_ROOT / dname
        if not dpath.is_dir():
            continue
        for root, dirs, files in os.walk(dpath):
            # Prune blacklisted directory names
            dirs[:] = [d for d in dirs if d not in BLACKLIST_NAMES and not d.endswith(".egg-info")]

            for file in files:
                if file in BLACKLIST_NAMES:
                    continue
                file_path = Path(root) / file
                if file_path.suffix.lower() in BLACKLIST_EXTENSIONS:
                    continue
                files_to_pack.append(file_path)

    return sorted(set(files_to_pack))


def build_submission_zip():
    print("=" * 70)
    print(" 📦 RECOVERIQ SUBMISSION PACKAGING & SECURITY AUDIT")
    print("=" * 70)

    print(f"[*] Workspace Root: {WORKSPACE_ROOT}")
    print(f"[*] Target Output:  {OUTPUT_ZIP_PATH}")

    # Step 1: Collect files
    files = collect_submission_files()
    print(f"[*] Collected {len(files)} files for packaging.")

    # Step 2: Strict Security Pre-Flight Check
    print("[*] Running Security & Credential Exposure Gate...")
    security_violations = []

    for f in files:
        rel_path = f.relative_to(WORKSPACE_ROOT)
        
        # Absolute block on any .env file
        if f.name == ".env" or (".env." in f.name and not f.name.endswith(".example")):
            security_violations.append(f"FATAL: Environment file queued for packaging: {rel_path}")

        # Scan content for secret patterns
        file_issues = scan_file_for_secrets(f)
        for issue in file_issues:
            security_violations.append(f"FATAL: {issue} ({rel_path})")

    if security_violations:
        print("\n" + "!" * 70)
        print(" ❌ SECURITY GATE FAILED: ACTIVE CREDENTIALS OR FORBIDDEN FILES DETECTED!")
        print("!" * 70)
        for v in security_violations:
            print(f"  • {v}")
        print("\nAborting packaging immediately. Clean credentials before packaging.")
        sys.exit(1)

    print(" [✓] Security Gate Passed: 0 active credentials or forbidden files detected.")

    # Step 3: Build ZIP Archive
    if OUTPUT_ZIP_PATH.exists():
        try:
            OUTPUT_ZIP_PATH.unlink()
            print(f"[*] Removed existing archive: {OUTPUT_ZIP_PATH.name}")
        except Exception as e:
            print(f"[-] Could not delete existing archive: {e}")

    print(f"[*] Creating ZIP archive: {OUTPUT_ZIP_PATH} ...")
    with zipfile.ZipFile(OUTPUT_ZIP_PATH, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zipf:
        for f in files:
            rel_path = f.relative_to(WORKSPACE_ROOT)
            archive_path = f"{ARCHIVE_PREFIX}/{rel_path.as_posix()}"
            zipf.write(f, arcname=archive_path)

    # Step 4: Post-Packaging Verification
    print("[*] Verifying packaged archive integrity...")
    with zipfile.ZipFile(OUTPUT_ZIP_PATH, "r") as verify_zip:
        namelist = verify_zip.namelist()
        total_items = len(namelist)
        
        # Verify forbidden files are ABSENT
        has_env = any(name.endswith("/.env") or name == ".env" for name in namelist)
        has_venv = any("/.venv/" in name for name in namelist)
        has_pytest = any("/.pytest_cache/" in name for name in namelist)
        has_pycache = any("/__pycache__/" in name for name in namelist)
        has_example = any(name.endswith("/.env.example") for name in namelist)

        assert not has_env, "Verification Failed: .env found in archive!"
        assert not has_venv, "Verification Failed: .venv found in archive!"
        assert not has_pytest, "Verification Failed: .pytest_cache found in archive!"
        assert not has_pycache, "Verification Failed: __pycache__ found in archive!"
        assert has_example, "Verification Failed: .env.example missing from archive!"

    size_bytes = OUTPUT_ZIP_PATH.stat().st_size
    size_mb = size_bytes / (1024 * 1024)

    print("=" * 70)
    print(" ✅ SUBMISSION ZIP CREATED SUCCESSFULLY & VERIFIED")
    print("=" * 70)
    print(f"  • Archive:        {OUTPUT_ZIP_PATH.name}")
    print(f"  • Full Path:      {OUTPUT_ZIP_PATH}")
    print(f"  • Total Files:    {total_items} (Clean, no .venv or build artifacts)")
    print(f"  • Archive Size:   {size_mb:.2f} MB ({size_bytes:,} bytes)")
    print(f"  • .env Excluded:  Verified (0 .env files)")
    print(f"  • .env.example:   Verified present")
    print(f"  • Security Gate:  100% Passed")
    print("=" * 70)


if __name__ == "__main__":
    build_submission_zip()

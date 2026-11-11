"""Run one request from request.txt through the whole team and save everything to last_run.txt, so the result can be
read (and a problem diagnosed) without copying anything out of a console window."""
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "last_run.txt"


def run(args: list[str], log) -> str:
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.Popen([sys.executable, "-u", "-m", "orchestra.cli", *args], cwd=ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    out = []
    for line in proc.stdout:
        out.append(line)
        log.write(line)
        log.flush()
        try:
            sys.stdout.write(line)
        except UnicodeEncodeError:
            sys.stdout.write(line.encode("ascii", "replace").decode())
    proc.wait()
    log.write(f"\n[exit code {proc.returncode}]\n")
    log.flush()
    return "".join(out)


def main() -> int:
    text = (ROOT / "request.txt").read_text(encoding="utf-8-sig").strip()
    with open(LOG, "w", encoding="utf-8") as log:
        log.write(f"درخواست: {text}\n\n")
        out = run(["ask", text], log)
        m = re.search(r"اجرا: (\S+) → awaiting_approval", out)
        if m:
            log.write("\n\n======== show ========\n")
            run(["show", m.group(1)], log)
    print("\nنتیجه در فایل last_run.txt ذخیره شد.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

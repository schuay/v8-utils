"""Re-record profile.json.gz: python3 record.py <d8>

Records work.js with samply and rewrites machine-specific paths so the
fixture does not depend on where it was recorded. The jitdump is not kept.
"""

import gzip
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> None:
    d8 = Path(sys.argv[1]).resolve()
    with tempfile.TemporaryDirectory() as tmp:
        rec = Path(tmp).resolve()
        subprocess.run(
            [
                "samply", "record", "--save-only", "-o", str(rec / "p.json.gz"),
                "--", str(d8), "--perf-prof", f"--perf-prof-path={rec}",
                "--interpreted-frames-native-stack", f"--logfile={rec / 'v8.log'}",
                str(HERE / "work.js"),
            ],
            check=True,
            cwd=rec,
        )  # fmt: skip
        text = gzip.decompress((rec / "p.json.gz").read_bytes()).decode()
    for old, new in ((str(rec), "/rec"), (str(HERE), "/work"), (str(d8), "/v8/out/d8")):
        text = text.replace(old, new)
    (HERE / "profile.json.gz").write_bytes(gzip.compress(text.encode(), mtime=0))


if __name__ == "__main__":
    main()

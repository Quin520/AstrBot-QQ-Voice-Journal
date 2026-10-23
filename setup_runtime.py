"""Create an isolated CPU speech environment; never modify AstrBot's Python."""

import argparse
import subprocess
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="Dedicated runtime directory")
    args = parser.parse_args()
    root = args.directory.expanduser().resolve()
    environment = root / "venv"
    python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if not python.exists():
        subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True)
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "torch==2.8.0",
            "torchaudio==2.8.0",
            "--index-url",
            "https://download.pytorch.org/whl/cpu",
        ],
        check=True,
    )
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "-r",
            str(Path(__file__).with_name("engine-requirements.txt")),
        ],
        check=True,
    )
    print(f"engine_python: {python}")
    print(f"engine_cache: {root / 'models'}")
    print("Models are downloaded on first transcription. No recording has started.")


if __name__ == "__main__":
    main()

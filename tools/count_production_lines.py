from __future__ import annotations

from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    total = 0
    files = []
    for path in sorted((root / "app").rglob("*.py")):
        lines = sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
        total += lines
        files.append((str(path.relative_to(root)), lines))
    print(f"production_nonblank_lines={total}")
    for name, lines in files:
        print(f"{lines:5d} {name}")


if __name__ == "__main__":
    main()

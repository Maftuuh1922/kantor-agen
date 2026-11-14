"""Run entry for Kantor Jasa (placeholder; a later task fills it in).

The folder name has a hyphen, so it cannot be imported. Importable code will
live in an ``apps/kantor_jasa`` package, like ``apps/kantor_warung``.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def main() -> None:
    print("Kantor Jasa: app entry point not implemented yet.")


if __name__ == "__main__":
    main()

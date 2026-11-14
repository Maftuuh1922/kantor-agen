"""Run entry for Kantor Warung (placeholder; a later task fills it in).

The folder name has a hyphen, so it cannot be imported. All real code lives in
the ``apps/kantor_warung`` package. This script only makes the repo root
importable and then hands over to that package.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from apps.kantor_warung import data  # noqa: E402,F401  (checks the package is importable)


def main() -> None:
    print("Kantor Warung: app entry point not implemented yet.")


if __name__ == "__main__":
    main()

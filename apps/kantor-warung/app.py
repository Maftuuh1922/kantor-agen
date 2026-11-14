"""Run entry for Kantor Warung.

    .venv/bin/python apps/kantor-warung/app.py --port 8000

The folder name has a hyphen, so it cannot be imported. All real code lives in
the ``apps/kantor_warung`` package. This script only makes the repo root
importable and then hands over to ``apps.kantor_warung.app.main``.
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from apps.kantor_warung.app import main  # noqa: E402

if __name__ == "__main__":
    main()

"""Kantor Warung: all importable Python code of the Warung application.

Why two folders
---------------
Requirement 9.1 puts each application in its own folder, ``apps/kantor-warung``
and ``apps/kantor-jasa``. A folder name with a hyphen is not a valid Python
package name, so nothing inside it can be imported by tests or by other code.

The choice made here:

* ``apps/kantor_warung/`` (underscore) is the real package. ALL importable
  Warung code lives here (``data.py``, ``roles.py``, ``template.py`` for the
  onboarding, ``seed.py`` for the demo data, ``app.py`` for the wiring), so it
  can be unit tested with ``from apps.kantor_warung import data``.
* ``apps/kantor-warung/`` (hyphen) stays as the documented run entry folder.
  It only holds a tiny ``app.py`` that puts the repo root on ``sys.path`` and
  calls ``apps.kantor_warung.app.main``. Start it with
  ``.venv/bin/python apps/kantor-warung/app.py``.

This ``__init__`` imports nothing, so importing one module of the package
does not pull in the web server.

Dependency direction: this package imports only from ``skeleton``. The
skeleton never imports from ``apps`` (Req 9).
"""

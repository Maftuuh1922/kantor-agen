"""Kantor Jasa: all importable Python code of the Jasa application.

Why two folders
---------------
Requirement 9.1 puts each application in its own folder, ``apps/kantor-warung``
and ``apps/kantor-jasa``. A folder name with a hyphen is not a valid Python
package name, so nothing inside it can be imported by tests or by other code.

The choice made here:

* ``apps/kantor_jasa/`` (underscore) is the real package. ALL importable
  Jasa code lives here (schema, data layer, roles, triggers, seed data), so it
  can be unit tested with ``from apps.kantor_jasa import data``.
* ``apps/kantor-jasa/`` (hyphen) stays as the documented run entry folder.
  It only holds a tiny ``app.py`` that puts the repo root on ``sys.path`` and
  imports from ``apps.kantor_jasa``. Start it with
  ``python apps/kantor-jasa/app.py``.

Dependency direction: this package imports only from ``skeleton``. The
skeleton never imports from ``apps`` (Req 9). The skeleton schema has no Jasa
tables, so this package creates its own (``apps.kantor_jasa.schema``).
"""

# Explicit application storage

`JARVIS_DATA_DIR`, when set before application imports, is an absolute root for
application-owned default data: SQLite, dictation history, location caches,
GeoIP and downloaded Piper models, optional prompt dumps, provider probe/state
files, desktop logs and its instance lock. An invalid relative override fails
before creating directories. Without the override, legacy defaults remain.

Explicit config/state/database/model paths retain precedence. HOME, CODEX_HOME,
provider authentication and installed executable lookup are never redirected.
Third-party cache/session paths require their own explicit environment options.
Desktop logs cannot silently fall back outside an explicit data root. Location
cache filenames are selected at import time, so the environment is a startup
setting, not a live configuration change. This override does not migrate files;
existing user data must be copied and verified before switching a productive run.

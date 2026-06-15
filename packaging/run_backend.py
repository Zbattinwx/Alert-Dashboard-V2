"""
Frozen-build entrypoint for the Alert Dashboard backend.

PyInstaller freezes THIS script (not backend/main.py's __main__) so we can import
the FastAPI `app` object directly and run uvicorn with the reloader disabled —
the string-import + reload path in backend/main.py:main() does not survive
freezing. Dev/Docker keep using `python backend/main.py` unchanged.

Run from source for the boot test:
    python packaging/run_backend.py          (HOST/PORT from env)
"""

import os
import sys


def _ensure_importable() -> None:
    """Make the `backend` package importable in both source and frozen runs."""
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if base not in sys.path:
        sys.path.insert(0, base)


def main() -> None:
    _ensure_importable()
    import uvicorn
    from backend.main import app

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "3074"))
    uvicorn.run(app, host=host, port=port, reload=False, log_level="info")


if __name__ == "__main__":
    main()

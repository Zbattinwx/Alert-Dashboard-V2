"""Route modules split out of main.py.

Each module owns one group and exposes `router`. main.py keeps startup
orchestration and includes them; the split is by URL group because that
is how the routes are read and changed.
"""

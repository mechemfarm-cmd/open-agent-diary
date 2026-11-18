"""Make the package importable when the whole repository is collected.

The package lives under ``src/`` and the project directory deliberately uses a
hyphen, so nothing here is importable by accident. Running the repository's
suite from the root must not fail on collection, so the source directory is
added explicitly.
"""

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

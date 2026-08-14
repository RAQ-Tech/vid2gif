r"""Keep a bare `python -m pytest` from writing outside the repository.

``app/config.py`` creates its directories at import time from ``STATE_ROOT``,
which defaults to ``/state``.  Importing any ``app`` module therefore has a
filesystem side effect before a single test runs: a stray ``state`` folder
appears at the drive root on Windows, and a Linux checkout fails outright
unless the run happens to be root.  CI sets ``STATE_ROOT`` explicitly to work
around it; every other contributor had to know to do the same.

pytest imports this file before any test module, so setting the variable here
puts it in place ahead of the first ``import app.*``.  An explicitly exported
``STATE_ROOT`` always wins -- this only supplies a safe default where there was
none.

``LIB_ROOT`` is deliberately left alone.  Nothing creates it at import time, and
several tests assert against the ``/library`` container path directly.
"""

import os
import tempfile
from pathlib import Path


SCRATCH_STATE_ROOT = Path(tempfile.gettempdir()) / "vid2gif-pytest-state"


if not os.environ.get("STATE_ROOT"):
    SCRATCH_STATE_ROOT.mkdir(parents=True, exist_ok=True)
    os.environ["STATE_ROOT"] = str(SCRATCH_STATE_ROOT)

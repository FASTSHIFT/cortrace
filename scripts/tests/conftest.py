"""Put scripts/ on sys.path so tests can `import perfetto_open` etc. without a
per-file sys.path hack."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

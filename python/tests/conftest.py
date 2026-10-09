"""Put python/ on sys.path so tests can `import cortrace` from the source tree."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

"""Windows launcher: starts the C1LUT GUI without a console window."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gui import main

sys.exit(main())

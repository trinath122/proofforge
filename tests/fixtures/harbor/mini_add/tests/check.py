import sys

sys.path.insert(0, ".")
import calc  # noqa: E402

sys.exit(0 if calc.add(2, 3) == 5 and calc.add(-1, 1) == 0 else 1)

"""Entry point for PyInstaller (it cannot start from a module that uses relative imports)."""
import sys

from oracle_jump_tool.__main__ import main

if __name__ == "__main__":
    sys.exit(main())

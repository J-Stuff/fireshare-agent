"""Convenience launcher so `python main.py` and the PyInstaller build both have a simple entry point."""
from fireshare_agent.__main__ import main

if __name__ == "__main__":
    main()
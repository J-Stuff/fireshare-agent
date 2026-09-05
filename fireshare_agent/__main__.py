"""Entry point: `python -m fireshare_agent`, `python main.py`, or the packaged executable."""
from __future__ import annotations

import logging
import logging.handlers
import sys

from fireshare_agent.config.store import app_data_dir


def _configure_logging() -> None:
    log_dir = app_data_dir()
    log_dir.mkdir(parents=True, exist_ok=True)

    handlers: list[logging.Handler] = [
        logging.handlers.RotatingFileHandler(
            log_dir / "agent.log", maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
    ]
    if not getattr(sys, "frozen", False):
        handlers.append(logging.StreamHandler(sys.stdout))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        handlers=handlers,
    )


def main() -> None:
    _configure_logging()
    # Imported after logging is configured so any import-time log calls land in the file too.
    from fireshare_agent import single_instance

    if not single_instance.acquire():
        logging.getLogger(__name__).warning("Another instance of Fireshare Agent is already running - exiting.")
        _notify_already_running()
        return

    from fireshare_agent.app import FireshareAgentApp

    app = FireshareAgentApp()
    app.run()


def _notify_already_running() -> None:
    import tkinter as tk
    import tkinter.messagebox as messagebox

    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo("Fireshare Agent", "Fireshare Agent is already running - check your system tray.")
        root.destroy()
    except Exception:
        pass  # best-effort notice only; exiting quietly is still the right outcome either way


if __name__ == "__main__":
    main()

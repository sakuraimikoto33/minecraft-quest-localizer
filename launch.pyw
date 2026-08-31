"""Double-click launcher for Windows source checkouts."""

from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))


def _show_startup_error(error: BaseException) -> None:
    message = f"Minecraft Quest Localizer を起動できませんでした。\n\n{type(error).__name__}: {error}"
    message = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[API KEY REDACTED]", message, flags=re.IGNORECASE)
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("起動エラー", message, parent=root)
        root.destroy()
    except BaseException:
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(0, message, "Minecraft Quest Localizer", 0x10)
        except BaseException:
            pass


try:
    from mq_localizer.app import main

    main()
except BaseException as exc:
    _show_startup_error(exc)

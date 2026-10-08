"""Run regression tests with withdrawn windows and isolated local state; no screenshots."""
import contextlib
import os
from pathlib import Path
import sys
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import nas_danmaku as d


class HiddenTk(tk.Tk):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.withdraw()


class HiddenTop(tk.Toplevel):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.withdraw()


class IsolatedResult(unittest.TextTestResult):
    def startTest(self, test):
        self.context = contextlib.ExitStack()
        folder = self.context.enter_context(tempfile.TemporaryDirectory())
        self.context.enter_context(patch.dict(os.environ, {'LOCALAPPDATA': folder,
            'DANDANPLAY_APP_ID': '', 'DANDANPLAY_APP_SECRET': ''}))
        self.context.enter_context(patch.object(d, 'local_backup_folder', return_value=Path(folder) / 'backups'))
        super().startTest(test)

    def stopTest(self, test):
        self.context.close()
        super().stopTest(test)


with contextlib.ExitStack() as stack:
    stack.enter_context(patch.object(tk, 'Tk', HiddenTk))
    stack.enter_context(patch.object(tk, 'Toplevel', HiddenTop))
    for method in ('showinfo', 'showwarning', 'showerror', 'askyesno', 'askyesnocancel'):
        stack.enter_context(patch.object(d.messagebox, method, return_value=False))
    suite = unittest.defaultTestLoader.discover('tests', pattern=sys.argv[1] if len(sys.argv) > 1 else 'test*.py')
    result = unittest.TextTestRunner(verbosity=2, resultclass=IsolatedResult).run(suite)
raise SystemExit(not result.wasSuccessful())

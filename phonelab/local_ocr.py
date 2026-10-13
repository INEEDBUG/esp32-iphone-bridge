"""Local, bounded Windows OCR of the nonce-validated synthetic test surface."""
import json
from pathlib import Path
import shutil
import subprocess


def read_probe_ocr(image_path, nonce, *, powershell=None, timeout=30):
    executable = powershell or shutil.which('powershell.exe')
    if not executable:
        raise RuntimeError('Windows PowerShell 5 OCR unavailable')
    script = Path(__file__).resolve().parents[1] / 'scripts' / 'read_synthetic_screen.ps1'
    try:
        completed = subprocess.run([executable, '-NoProfile', '-ExecutionPolicy', 'RemoteSigned', '-File', str(script),
            '-ImagePath', str(Path(image_path).resolve()), '-ExpectedNonce', nonce],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            timeout=min(timeout, 30), check=False)
        if completed.returncode != 0:
            raise ValueError()
        result = json.loads(completed.stdout)
        if (not isinstance(result, dict) or result.get('status') not in {'OK', 'PARTIAL'}
                or not isinstance(result.get('visible_probes'), list)):
            raise ValueError()
        return result
    except Exception:
        # Exception text, stderr and non-synthetic raw OCR never enter reports.
        raise RuntimeError('local synthetic OCR unavailable') from None

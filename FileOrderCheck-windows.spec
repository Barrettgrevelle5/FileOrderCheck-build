# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the Windows one-file FileOrderCheck.exe.
#
# Bundles everything a Windows machine needs (no installs required):
#   - the served UI  (File Checklist Validator.html)
#   - Tesseract OCR binary + DLLs
#   - Poppler's pdftoppm / pdftocairo / pdfinfo + DLLs
#   - Tesseract language data (tessdata/eng.traineddata)
#
# Native binaries are located on the BUILD machine via:
#   TESSERACT_BIN env var, or the standard Chocolatey install path.
#   POPPLER_BIN   env var (set by the GitHub Actions workflow).
# At RUNTIME, launch.py prepends sys._MEIPASS to PATH so the bundled
# binaries are found without any system install.
import glob
import os
import shutil

PROJECT_DIR = os.path.abspath(os.getcwd())


def _require(path, what):
    if not path or not os.path.exists(path):
        raise SystemExit(
            f"build error: could not find {what} ({path!r}). "
            f"Install it on this build machine and set the env var, then retry."
        )
    return path


# ── Tesseract ────────────────────────────────────────────────────────────────
TESS_CANDIDATES = [
    os.environ.get('TESSERACT_BIN', ''),
    r'C:\Program Files\Tesseract-OCR\tesseract.exe',
    r'C:\Program Files (x86)\Tesseract-OCR\tesseract.exe',
    shutil.which('tesseract') or '',
]
tesseract_bin = next((p for p in TESS_CANDIDATES if p and os.path.exists(p)), None)
_require(tesseract_bin, 'tesseract.exe')
tesseract_dir = os.path.dirname(tesseract_bin)

tessdata_dir = None
for cand in [
    os.path.join(tesseract_dir, 'tessdata'),
    r'C:\Program Files\Tesseract-OCR\tessdata',
    r'C:\Program Files (x86)\Tesseract-OCR\tessdata',
]:
    if os.path.exists(os.path.join(cand, 'eng.traineddata')):
        tessdata_dir = cand
        break
_require(os.path.join(tessdata_dir or '', 'eng.traineddata'), 'eng.traineddata')

# ── Poppler ──────────────────────────────────────────────────────────────────
poppler_bin_dir = os.environ.get('POPPLER_BIN', '')
if not poppler_bin_dir or not os.path.exists(os.path.join(poppler_bin_dir, 'pdftoppm.exe')):
    # Fallback: search common locations
    for pattern in [
        r'C:\ProgramData\chocolatey\lib\poppler*\tools',
        r'C:\tools\poppler*\Library\bin',
    ]:
        matches = [m for m in glob.glob(pattern)
                   if os.path.exists(os.path.join(m, 'pdftoppm.exe'))]
        if matches:
            poppler_bin_dir = matches[0]
            break
    if not poppler_bin_dir:
        found = shutil.which('pdftoppm')
        poppler_bin_dir = os.path.dirname(found) if found else None

_require(
    os.path.join(poppler_bin_dir or '', 'pdftoppm.exe'),
    'pdftoppm.exe (set POPPLER_BIN env var to the folder containing pdftoppm.exe)',
)


def _collect_dir_binaries(bin_dir, dest='.'):
    """Bundle every .exe and .dll in a directory."""
    entries = []
    for pattern in ('*.exe', '*.dll'):
        for path in glob.glob(os.path.join(bin_dir, pattern)):
            entries.append((path, dest))
    return entries


binaries = (
    [(tesseract_bin, '.')]
    + _collect_dir_binaries(tesseract_dir)     # tesseract DLLs
    + _collect_dir_binaries(poppler_bin_dir)   # pdftoppm, pdftocairo, pdfinfo + DLLs
)

datas = [
    (os.path.join(PROJECT_DIR, 'File Checklist Validator.html'), '.'),
    (tessdata_dir, 'tessdata'),
]

# certifi: bundles the CA cert file so the update check's HTTPS fetch can verify
# raw.githubusercontent.com in the frozen Windows build — without it urlopen raises
# CERTIFICATE_VERIFY_FAILED (no OS CA store) and the update banner never shows.
# PyInstaller's certifi hook also ships cacert.pem so certifi.where() resolves at
# runtime. See app.check_update. (2026-07-07)
hiddenimports = ['app', 'flask', 'pytesseract', 'pdf2image', 'PIL', 'rapidfuzz', 'certifi']

a = Analysis(
    ['launch.py'],
    pathex=[PROJECT_DIR],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

# One-file build: all binaries/datas go directly into EXE (no COLLECT).
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='FileOrderCheck',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,   # no CMD window — app opens a browser tab
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

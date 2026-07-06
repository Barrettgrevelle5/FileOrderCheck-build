# FileOrderCheck — build inputs (Windows CI)

This repo holds ONLY the files needed to build the Windows version of
FileOrderCheck on a GitHub-hosted Windows runner. It contains **no applicant
data** (no case files, no OCR fixtures, no test data — those live elsewhere and
never come here).

On every push to `main` (or via **Actions → Build Windows exe → Run workflow**),
GitHub builds `FileOrderCheck.exe` and uploads it as a workflow **artifact**. To
publish it, the maintainer downloads that artifact and attaches it to the matching
release in the `FileOrderCheck-releases` repo.

Files:
- `app.py`, `launch.py`, `File Checklist Validator.html` — the app.
- `requirements.txt` — Python packages.
- `FileOrderCheck-windows.spec` — PyInstaller spec (bundles Tesseract + Poppler).
- `.github/workflows/windows-build.yml` — the build workflow.

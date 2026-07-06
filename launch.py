#!/usr/bin/env python3
"""Frozen-app entrypoint for FileOrderCheck (PyInstaller .app).

This file contains NO compliance / OCR / document-detection logic — all of that
lives in app.py and the served HTML. Its only jobs are:

  1. In a frozen bundle, point the OCR toolchain at the native binaries shipped
     INSIDE the .app — Tesseract, Poppler's pdftoppm/pdftocairo, and Tesseract's
     tessdata — so an office Mac needs no Homebrew / Python / Tesseract / Poppler
     install and never reads from /opt/homebrew at runtime.
  2. Start the existing Flask app (app.app) on 127.0.0.1:5050 and open the browser,
     reusing app.py's own open_browser helper.

Run directly when NOT frozen and it just launches the dev server like
`python3 app.py` does (the toolchain configuration below is a no-op off-bundle).
"""
import os
import sys
import threading


def _configure_bundled_ocr_toolchain():
    """Point pytesseract / pdf2image at binaries bundled in the .app (frozen only).

    No-op when not frozen, so a normal `python3 launch.py` keeps using whatever
    Tesseract/Poppler are on PATH — unchanged behavior off-bundle.
    """
    meipass = getattr(sys, '_MEIPASS', None)
    if not getattr(sys, 'frozen', False) or not meipass:
        return

    # Bundled native CLIs sit at the bundle root with their dylibs beside them.
    # Prepending PATH lets pdf2image discover pdftoppm/pdftocairo and removes any
    # dependence on Homebrew at runtime.
    os.environ['PATH'] = meipass + os.pathsep + os.environ.get('PATH', '')

    # Tesseract language data (eng.traineddata) is bundled under tessdata/.
    tessdata = os.path.join(meipass, 'tessdata')
    if os.path.isdir(tessdata):
        os.environ['TESSDATA_PREFIX'] = tessdata

    # Point pytesseract explicitly at the bundled tesseract binary.
    tess_bin = os.path.join(meipass, 'tesseract')
    if os.path.isfile(tess_bin):
        try:
            import pytesseract
            pytesseract.pytesseract.tesseract_cmd = tess_bin
        except Exception:
            # If pytesseract can't be imported here, the OCR route will surface the
            # error normally — don't crash the launcher over toolchain config.
            pass


HOST = '127.0.0.1'
PORT = 5050
URL = 'http://127.0.0.1:5050'


def _port_is_serving(host=HOST, port=PORT, timeout=0.5):
    """True if something is already accepting TCP connections on host:port."""
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# Stable markers from our served HTML. If a service already on 5050 returns one of
# these, it is (an existing) FileOrderCheck; otherwise it is an unrelated app and we
# must NOT open the browser to it. Both are long-standing: the <title> brand string
# and the startup function name in the inline JS.
FOC_MARKERS = ('BC File Approval Checker', 'renderQuestionnaire')


def _existing_server_is_ours(host=HOST, port=PORT, timeout=2.0):
    """GET / on the already-listening port and confirm it is FileOrderCheck by a
    stable served-HTML marker — so an unrelated service on 5050 is never mistaken for
    us. Stdlib-only (works in the frozen bundle). Returns True only on a confident
    match; any error / non-HTTP / missing-marker response returns False."""
    import urllib.request
    try:
        with urllib.request.urlopen('http://%s:%d/' % (host, port), timeout=timeout) as r:
            body = r.read(65536).decode('utf-8', 'replace')
    except Exception:
        return False
    return any(m in body for m in FOC_MARKERS)


def _log_launch(msg):
    """Frozen-only diagnostics. A windowed app hides stdout/stderr, so a startup
    failure (most often "port already in use") is otherwise completely silent — the
    server never comes up and the browser can't load. Record it to a local log the
    user/maintainer can read. No-op (and no file) in normal dev runs."""
    if not getattr(sys, 'frozen', False):
        return
    try:
        import datetime, platform
        if platform.system() == 'Windows':
            d = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'FileOrderCheck')
        else:
            d = os.path.expanduser('~/Library/Application Support/FileOrderCheck')
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'launch.log'), 'a', encoding='utf-8') as f:
            f.write('[%s] %s\n' % (datetime.datetime.now().isoformat(timespec='seconds'), msg))
    except Exception:
        pass


def _alert_port_conflict():
    """Frozen-only user-facing dialog (a windowed app has no console to print to).
    Best-effort; silently skipped off-bundle or if the dialog mechanism fails."""
    if not getattr(sys, 'frozen', False):
        return
    msg = ('FileOrderCheck could not start because another app is already using '
           'port 5050. Quit that app or restart your computer, then open '
           'FileOrderCheck again.')
    try:
        import platform
        if platform.system() == 'Windows':
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, msg, 'FileOrderCheck', 0x30)  # MB_ICONWARNING
        else:
            import subprocess
            subprocess.Popen(['/usr/bin/osascript', '-e',
                'display dialog "%s" with title "FileOrderCheck" buttons {"OK"} '
                'default button "OK" with icon caution' % msg])
    except Exception:
        pass


def _open_browser_when_ready():
    """Open the browser only AFTER the server is actually accepting connections, so the
    tab never lands on a not-yet-bound port (PyInstaller cold start can take several
    seconds — longer than a fixed sleep would wait). Falls back to the macOS `open`
    command if webbrowser can't drive a browser in the frozen GUI context."""
    import time, webbrowser
    for _ in range(60):  # up to ~30s of cold start
        if _port_is_serving():
            break
        time.sleep(0.5)
    else:
        return  # server never came up; nothing to open
    try:
        if not webbrowser.open(URL):
            raise RuntimeError('webbrowser.open returned False')
    except Exception:
        try:
            import platform, subprocess
            if platform.system() == 'Windows':
                subprocess.Popen(['cmd', '/c', 'start', URL])
            else:
                subprocess.Popen(['/usr/bin/open', URL])
        except Exception:
            pass


def main():
    _configure_bundled_ocr_toolchain()
    frozen = getattr(sys, 'frozen', False)

    # If the port is already serving, find out WHAT. Starting a second server would
    # crash with "Address already in use" — and under console=False that crash is
    # invisible, so the user just sees "nothing loads".
    if _port_is_serving():
        if _existing_server_is_ours():
            # A duplicate double-click or a leftover FileOrderCheck — reopen it.
            _log_launch('port %d already serving FileOrderCheck — opened existing instance, no duplicate started' % PORT)
            try:
                import webbrowser
                webbrowser.open(URL)
            except Exception:
                pass
        else:
            # Some UNRELATED app owns 5050. Do NOT open the browser to it; tell the
            # user plainly and exit. (We don't fall back to another port — app.py / the
            # UI / docs all assume 127.0.0.1:5050.)
            _log_launch('port %d is in use by a NON-FileOrderCheck service — aborting, did not open the browser to it' % PORT)
            _alert_port_conflict()
        return

    # Import the existing Flask app. All real logic stays in app.py.
    import app as appmod

    print("=" * 50)
    print("  FileOrderCheck — " + URL)
    print("  Opening browser automatically...")
    print("=" * 50)
    _log_launch('starting server on ' + URL)

    # Browser opens once the server is confirmed up (readiness-gated, fires once —
    # frozen mode has no reloader child).
    threading.Thread(target=_open_browser_when_ready, daemon=True).start()

    # Frozen bundles must NOT use the Werkzeug reloader (it re-execs the bundle).
    try:
        appmod.app.run(host=HOST, port=PORT,
                       debug=not frozen, use_reloader=not frozen, threaded=True)
    except Exception as e:
        # console=False hides this traceback; record it so the failure isn't silent.
        import traceback
        _log_launch('SERVER FAILED TO START: %r\n%s' % (e, traceback.format_exc()))
        raise


if __name__ == '__main__':
    main()

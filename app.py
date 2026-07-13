from flask import Flask, request, jsonify, send_from_directory, Response, stream_with_context
import base64
import binascii
import json
import os
import sys

import re
import logging
import threading
import webbrowser

# rapidfuzz powers the OCR-tolerant anchor matching in the checklist locator.
# If it isn't installed, fall back to a normalized substring match so the
# module still imports (see _anchor_ratio).
try:
    from rapidfuzz import fuzz
    _HAVE_RAPIDFUZZ = True
except ImportError:
    fuzz = None
    _HAVE_RAPIDFUZZ = False

app = Flask(__name__)
# PyInstaller sets sys.frozen and unpacks bundled data under sys._MEIPASS. Neither
# is set under a normal `python3 app.py` run, so every frozen-aware branch below
# falls back to the existing repo-relative behavior, unchanged.
FROZEN = getattr(sys, 'frozen', False)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
logger = logging.getLogger(__name__)


def resource_path(*parts):
    """Absolute path to a bundled read-only resource (e.g. the served HTML).

    Frozen: resolve under sys._MEIPASS (PyInstaller's extraction dir).
    Non-frozen: resolve next to this file (BASE_DIR) — identical to before.
    """
    base = getattr(sys, '_MEIPASS', BASE_DIR)
    return os.path.join(base, *parts)


# ── App version + update check (Tier 1: notify-only) ──────────────────────────
# APP_VERSION is the single source of truth for "which build is this". Bump it on
# every release and set the SAME value as "version" in the hosted manifest below.
APP_VERSION = '2026.07.30'

# URL of the hosted update manifest — a tiny JSON file you control. Leave EMPTY to
# disable the update check entirely (it becomes a silent no-op). The manifest is
# PII-FREE (it only names the latest version + a download link), so it can live on
# any static public host: a code-free GitHub "releases" repo's raw file, an S3/R2
# bucket, a Netlify page, or a Gist. Cross-platform — identical on macOS/Windows.
# Expected JSON shape:
#   { "version": "2026.07.20",
#     "download_url": "https://.../FileOrderCheck.zip",
#     "notes": "optional one-line what's-new" }
UPDATE_MANIFEST_URL = 'https://raw.githubusercontent.com/Barrettgrevelle5/FileOrderCheck-releases/main/version.json'


def _version_tuple(v):
    """Split a version string into a tuple of ints for ordered comparison.

    '2026.07.20' -> (2026, 7, 20). Non-numeric characters are ignored so both
    date (YYYY.MM.DD) and plain dotted-number schemes compare correctly; an
    unparseable/empty value yields () which sorts lowest.
    """
    return tuple(int(n) for n in re.findall(r'\d+', str(v if v is not None else '')))


def _update_available(current, latest):
    lt = _version_tuple(latest)
    return bool(lt) and lt > _version_tuple(current)


# A tiny FROZEN-ONLY "Quit" control. A browser-tab app has no window to close, so
# without this an office user has no obvious way to stop the bundled .app's headless
# Flask server (closing the tab leaves it running). It is injected at SERVE time only
# in a frozen bundle — the dev path below is byte-for-byte unchanged — and only ever
# calls the loopback /shutdown route. It is self-contained and does not touch any of
# the page's existing render logic.
_QUIT_WIDGET = """
<div id="foc-quit" style="position:fixed;bottom:12px;right:12px;z-index:2147483647;">
  <button id="foc-quit-btn" style="font:600 12px -apple-system,system-ui,sans-serif;padding:7px 11px;background:#b00020;color:#fff;border:none;border-radius:6px;cursor:pointer;box-shadow:0 1px 4px rgba(0,0,0,.25)">Quit FileOrderCheck</button>
</div>
<script>
(function(){
  var b=document.getElementById('foc-quit-btn');
  if(!b)return;
  b.addEventListener('click',function(){
    if(!confirm('Quit FileOrderCheck? The local app will stop running.'))return;
    fetch('/shutdown',{method:'POST'}).catch(function(){});
    document.body.innerHTML='<div style="font:16px -apple-system,system-ui,sans-serif;padding:48px;color:#333">FileOrderCheck has stopped. You can close this tab.</div>';
  });
})();
</script>
"""


@app.route('/')
def index():
    # Dev (non-frozen): serve the file unchanged, exactly as before.
    if not FROZEN:
        return send_from_directory(resource_path(), 'File Checklist Validator.html')
    # Frozen (.app): inject the Quit control so the headless server is stoppable.
    with open(resource_path('File Checklist Validator.html'), 'r', encoding='utf-8') as f:
        html = f.read()
    if '</body>' in html:
        html = html.replace('</body>', _QUIT_WIDGET + '</body>', 1)
    else:
        html += _QUIT_WIDGET
    return Response(html, mimetype='text/html')


@app.route('/shutdown', methods=['POST'])
def shutdown():
    """Frozen-only, loopback-only kill switch for the bundled app's headless server.

    Disabled entirely in dev (404) so it can never affect normal `python3 app.py`
    runs or the test suite. Werkzeug 2.1+ removed the in-request shutdown function,
    so we flush this response and then exit the whole process.
    """
    if not FROZEN:
        return ('', 404)
    if request.remote_addr not in ('127.0.0.1', '::1'):
        return ('', 403)

    def _stop():
        import time
        time.sleep(0.3)  # let the response flush before the process exits
        os._exit(0)

    threading.Thread(target=_stop, daemon=True).start()
    return jsonify({'stopped': True})


@app.route('/api/check-update', methods=['GET'])
def check_update():
    """Tier-1 notify-only update check (cross-platform, macOS + Windows).

    Fetches the hosted manifest server-side (so there is no browser CORS issue)
    and reports whether a newer build exists. It NEVER blocks or breaks the app:
    any failure — no URL configured, offline, timeout, bad JSON — returns
    updateAvailable=False so the app runs normally with no error surfaced. No PII
    is transmitted; this is an outbound GET for a version number only.
    """
    result = {'current': APP_VERSION, 'latest': None, 'updateAvailable': False,
              'downloadUrl': None, 'notes': None, 'error': None}
    if not UPDATE_MANIFEST_URL:
        return jsonify(result)   # feature not configured yet → silent no-op
    try:
        import urllib.request
        # SSL trust: a frozen Windows PyInstaller build has no OS CA store to fall
        # back on, so the default context raises CERTIFICATE_VERIFY_FAILED and the
        # HTTPS fetch below silently fails (the whole update check appears dead on
        # Windows while working on macOS). Build the context from certifi's bundled
        # CA file when available; if certifi is missing (e.g. a dev box without it),
        # ctx stays None and urlopen uses the platform default — unchanged macOS/dev
        # behavior. certifi must be bundled in the .spec files for this to help the
        # frozen build. (2026-07-07)
        ctx = None
        try:
            import ssl
            import certifi
            ctx = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            ctx = None
        req = urllib.request.Request(
            UPDATE_MANIFEST_URL, headers={'User-Agent': 'FileOrderCheck'})
        with urllib.request.urlopen(req, timeout=4, context=ctx) as resp:
            manifest = json.loads(resp.read().decode('utf-8'))
        result['latest'] = manifest.get('version')
        result['downloadUrl'] = manifest.get('download_url')
        result['notes'] = manifest.get('notes')
        result['updateAvailable'] = _update_available(APP_VERSION, result['latest'])
    except Exception as e:
        # Offline / unreachable / malformed manifest must not nag the user — the UI
        # ignores this field entirely — but a maintainer hitting this endpoint
        # directly needs the real reason, since nothing configures `logging` in this
        # app (so logger.info() below goes nowhere) and a frozen Windows build with
        # console=False has no console to print to either.
        result['error'] = '%s: %s' % (type(e).__name__, e)
        logger.info('update check skipped: %s', e)
    return jsonify(result)


# ── /api/scan-checklist ───────────────────────────────────────────────────────
# Locate the FILE APPROVAL CHECKLIST page (it isn't always page 1) with a fast
# low-DPI fingerprint pass, then OCR just that page at dual DPI and detect which
# boxes are checked.
@app.route('/api/scan-checklist', methods=['POST'])
def scan_checklist():
    if 'pdf' not in request.files:
        return jsonify({'error': 'No PDF uploaded'}), 400

    pdf_bytes = request.files['pdf'].read()

    try:
        from pdf2image import convert_from_bytes
        import pytesseract

        # Step 1: find the real checklist page (low-DPI, short-circuits early).
        loc = locate_checklist_page(pdf_bytes)
        if loc['page_index'] is None:
            # Flag for a human instead of silently scanning the wrong page.
            return jsonify({
                'needs_manual_review': True,
                'reason': 'checklist page not found',
                'profile': None,
            })

        p = loc['page_index'] + 1  # pdf2image is 1-based

        # Step 2: run the existing dual-DPI logic on the located page.
        # 200 DPI is more reliable for most items (program, child_support); 250 DPI
        # captures checkbox chars that 200 DPI drops (e.g. Special Needs) because
        # they end up displaced to the prior line.
        imgs_200 = convert_from_bytes(pdf_bytes, dpi=200, first_page=p, last_page=p)
        imgs_250 = convert_from_bytes(pdf_bytes, dpi=250, first_page=p, last_page=p)
        text_200 = pytesseract.image_to_string(imgs_200[0], lang='eng')
        text_250 = pytesseract.image_to_string(imgs_250[0], lang='eng')
        p200 = detect_checklist_profile(text_200)
        p250 = detect_checklist_profile(text_250)
        # Pixel pass (see detect_checklist_profile_pixels): reads each checkbox from
        # its actual pixels, recovering checked boxes whose glyph Tesseract dropped
        # (e.g. Case A Special Needs / Bank Statement). Run on the 250-DPI image —
        # sharper box edges. UNIONed with the text passes below: a box is checked if
        # the text OR the pixel pass sees it checked. The mutual-exclusion dedup that
        # detect_checklist_profile applies to the TEXT pass is deliberately NOT
        # re-applied here — managers on the real files check both the under- and
        # over-threshold asset boxes, and the pixel pass reads both correctly; nulling
        # them would resurface the very false "checkbox unchecked" advisory this fixes.
        ppix = detect_checklist_profile_pixels(imgs_250[0])
        # UNION the two text passes with the pixel-CHECKED items, then apply the pixel
        # confident-EMPTY veto: an item whose every checkbox the pixel pass located and
        # measured empty is dropped even if a text pass read it checked. This kills the
        # text-pass false positives where an empty box outline OCRs as a checkmark
        # artifact ('[]', '(1', a stray digit) — the box the pixel pass actually saw is
        # empty. The veto never fires against a pixel-CHECKED item (mutually exclusive by
        # construction) nor against an item with an unlocatable box (UNKNOWN, not EMPTY).
        empty_income = set(ppix['empty']['income'])
        empty_conditions = set(ppix['empty']['conditions'])
        profile = {
            'program':    p200['program'] or p250['program'],
            'income':     [v for v in dict.fromkeys(p200['income'] + p250['income'] + ppix['income'])
                           if v not in empty_income],
            'conditions': [v for v in dict.fromkeys(p200['conditions'] + p250['conditions'] + ppix['conditions'])
                           if v not in empty_conditions],
        }
        return jsonify({
            'profile': profile,
            'checklist_page': p,
            'checklist_confidence': loc['confidence'],
            '_debug_ocr': text_200,
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# Directory for per-PDF OCR debug dumps. Local-only, holds PII (raw OCR text) — never
# served by any route; keep it off the network.
#   Non-frozen (repo run): BASE_DIR/ocr_debug — .gitignore excludes it from commits.
#   Frozen (.app): the bundle dir is read-only/ephemeral, so write to a per-user,
#   non-cloud-synced location instead. Never inside the bundle.
if FROZEN:
    OCR_DEBUG_DIR = os.path.join(
        os.path.expanduser('~/Library/Application Support/FileOrderCheck'),
        'ocr_debug',
    )
else:
    OCR_DEBUG_DIR = os.path.join(BASE_DIR, 'ocr_debug')


def _ocr_dump_path(original_name):
    """Build the dump file path for an upload: ocr_debug/<sanitized-stem>_<timestamp>.txt."""
    import time
    stem = os.path.splitext(os.path.basename(original_name or 'upload'))[0]
    safe = re.sub(r'[^A-Za-z0-9._-]+', '_', stem).strip('_') or 'upload'
    os.makedirs(OCR_DEBUG_DIR, exist_ok=True)
    return os.path.join(OCR_DEBUG_DIR, f"{safe}_{time.strftime('%Y%m%d-%H%M%S')}.txt")


# ── /api/ocr ──────────────────────────────────────────────────────────────────
# OCR all pages, streaming progress events.
@app.route('/api/ocr', methods=['POST'])
def ocr():
    if 'pdf' not in request.files:
        return jsonify({'error': 'No PDF uploaded'}), 400

    pdf_bytes = request.files['pdf'].read()
    pdf_filename = request.files['pdf'].filename

    def generate():
        try:
            from pdf2image import convert_from_bytes
            import pytesseract

            images = convert_from_bytes(pdf_bytes, dpi=200)
            total = len(images)
            pages_text = []

            # Per-PDF OCR debug dump: one .txt per upload, all pages concatenated with
            # a "--- PAGE n ---" delimiter, written as a SIDE EFFECT of this OCR pass
            # (no separate re-OCR). PII, same as _debug_ocr — local file only, never
            # served over the network. Writing incrementally means a partial dump
            # survives if OCR is interrupted. If the dump can't be opened, OCR proceeds
            # anyway (debug aid must never break the pipeline).
            dump_path = _ocr_dump_path(pdf_filename)
            try:
                dump = open(dump_path, 'w', encoding='utf-8')
            except Exception as e:
                dump = None
                logger.warning("OCR debug dump disabled (%s): %s", dump_path, e)

            try:
                for i, img in enumerate(images):
                    # The OCR result is what the app consumes — compute and record it FIRST,
                    # independent of the debug dump.
                    text = pytesseract.image_to_string(img, lang='eng')
                    pages_text.append(text)

                    # The dump is a best-effort side-effect. Its write is fully isolated in
                    # its own try/except: any failure (disk full, I/O error) is swallowed and
                    # logged, dumping is disabled for the rest of the run, and the OCR loop —
                    # the real pipeline output (pages_text + the SSE stream) — continues
                    # completely unaffected. A debug-logging failure can never degrade results.
                    if dump:
                        try:
                            dump.write(f"--- PAGE {i + 1} ---\n{text}\n")
                            dump.flush()
                        except Exception as e:
                            logger.warning("OCR debug dump write failed (%s); continuing OCR without it: %s", dump_path, e)
                            try:
                                dump.close()
                            except Exception:
                                pass
                            dump = None

                    yield f"data: {json.dumps({'progress': i + 1, 'total': total})}\n\n"
            finally:
                if dump:
                    try:
                        dump.close()
                    except Exception:
                        pass

            yield f"data: {json.dumps({'done': True, 'pageTexts': pages_text})}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'}
    )


# ── Bank statement transaction analysis ──────────────────────────────────────

@app.route('/api/analyze-bank', methods=['POST'])
def analyze_bank():
    # PII / DATA-FLOW NOTE (2026-07-10 geometric rebuild): this route now receives the
    # full applicant PDF as base64 (data['pdf']) and RE-OCRs the selected pages with
    # image_to_data — so raw applicant PDF bytes now flow to this endpoint, not just OCR
    # text. Same local-only PII posture as the rest of the app; never expose to a network.
    # data['pages'] now supplies only the selected page INDICES; the parser no longer
    # consumes its OCR text (geometry re-OCRs the pages from the PDF bytes instead).
    data = request.get_json(force=True, silent=True)
    if not data or 'pages' not in data:
        return jsonify({'error': 'Missing pages'}), 400

    # Geometric parse: a second image_to_data OCR pass on the selected pages rebuilds
    # rows the shared PSM-3 text linearizes away (see _extract_bank_rows). It NEEDS the
    # page images, so the client sends the PDF (base64) alongside the page list.
    pdf_b64 = data.get('pdf')
    indices = [p.get('index') for p in data['pages']]

    # There is NO text-only fallback: without the PDF bytes there is nothing to OCR
    # geometrically. Returning an empty result for a missing/blank 'pdf' would silently
    # turn an upload/integration failure into "no income detected" — unacceptable in a
    # compliance workflow — so a missing, null, blank, or non-string 'pdf' is a 400.
    if not isinstance(pdf_b64, str) or not pdf_b64.strip():
        return jsonify({'error': 'A base64-encoded PDF is required for bank analysis'}), 400

    # Narrow catch #1: base64 decoding only. base64.b64decode raises binascii.Error on a
    # non-base64 payload; that (and an empty decode) is client input, so → 400.
    try:
        pdf_bytes = base64.b64decode(pdf_b64.strip(), validate=True)
    except (binascii.Error, ValueError):
        return jsonify({'error': 'The supplied PDF is not valid base64'}), 400
    if not pdf_bytes:
        return jsonify({'error': 'The supplied PDF is empty'}), 400

    # Narrow catch #2: PDF rasterization only. _ocr_bank_pages_tsv raises BankPdfError
    # solely when the decoded bytes are not a renderable PDF (the recognized client-input
    # failure). Any OTHER exception from OCR, geometric extraction, or transaction parsing
    # is NOT caught here and surfaces as a normal 500 — a real defect must never be
    # disguised as "invalid PDF". No traceback, path, base64, or applicant data is echoed.
    try:
        pages_words = _ocr_bank_pages_tsv(pdf_bytes, indices)
    except BankPdfError:
        logger.warning("analyze-bank: supplied bytes are not a renderable PDF")
        return jsonify({'error': 'Could not read the supplied PDF for bank analysis'}), 400
    return jsonify({'transactions': _parse_bank_transactions(pages_words)})


def _classify_tx_type(desc):
    d = desc.lower()
    if re.search(r'zel[* ]|zelle', d):
        return 'p2p'
    if re.search(r'venmo|cash\s*app|cashapp|paypal|apple\s*cash|chime', d):
        return 'p2p'
    if re.search(r'payroll|direct\s*dep', d):
        return 'payroll'
    if re.search(r'\btransfer\b', d):
        return 'transfer'
    return 'deposit'


# ── Geometric bank-statement parser ───────────────────────────────────────────
# WHY GEOMETRY. The shared /api/ocr PSM-3 pass linearizes a multi-column statement
# COLUMN-FIRST: on UFCU's form it emits every date, then every description, then every
# amount, then every balance — so no text line is ever a "row", the register parses to
# zero transactions, and the old balance-delta spine drifted (stale anchors, phantom
# credits). For the BANK PATH ONLY we take a second OCR pass with image_to_data (word
# boxes) on the selected pages and rebuild rows by geometry: group words by shared
# y-coordinate, then assign each numeric token to a money column by x-position anchored
# to the page's own column-header words. Because geometry — not text order — isolates
# the AMOUNT column, the printed amount is clean and is the PRIMARY signal; the balance
# column (which carries OCR spikes like 4,404.09 between two ~1,400 rows) is only a
# cross-check and a fallback when the printed token is corrupt. Nothing here touches the
# global PSM-3 text every other consumer reads.

_BANK_MONEY_RE = re.compile(r'^[-~+]?\$?\d[\d,]*\.\d{2}[.,]?$')   # -8.68 / 1,730.48 / ~41.99
_BANK_DATE_RE = re.compile(r'^\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?$')
_BANK_SEED_RE = re.compile(r'\b(?:starting|beginning)\b', re.IGNORECASE)
_BANK_OUT_RE = re.compile(r'\bwithdrawal\b|\bdebit\b|withdrawal by check', re.IGNORECASE)
_BANK_IN_RE = re.compile(r'\bdeposit\b|\bcredit\b', re.IGNORECASE)
_BANK_HDR_KEYS = ('withdrawals', 'deposits', 'balance', 'amount', 'description', 'date', 'type')
_BANK_YBAND = 14           # px tolerance for grouping words into one row at 200 DPI
_BANK_SPIKE_MIN = 400.0    # a balance that jumps this far from the anchor and reverts is untrusted
# Printed-vs-balance amount ratio at/above which the printed token is treated as grossly
# wrong (a leading-digit-class OCR blunder, e.g. '4,202.85' for ~1,202.85). NOT an
# "order of magnitude" (10x) — a ~3x gross disagreement. Only then, and only when the
# balance delta is trusted, is the printed amount overridden by the balance amount.
_BANK_AMOUNT_RATIO_OUTLIER = 3
# Continuation-fold bounds (px at 200 DPI). A real continuation band (a payee tail, a
# CO:/TYPE: line) sits within ~one row height BELOW its transaction; statement footers and
# the full-page legal disclosure that can follow are separated by a larger gap or a page
# break. Fold only within the SAME page, only across a small vertical gap, and only until a
# generous length cap — so disclosure prose can never be absorbed into a transaction as a
# giant phantom (UFCU p40's back-of-statement legal text used to fold into the p40 Ending
# Balance row → a 4,300-char phantom 'in'). The length cap is a conservative final guard,
# not the primary rule (same-page + gap + page-break are the structural ones).
_BANK_FOLD_MAX_GAP = 45
_BANK_FOLD_MAX_LEN = 500
# Row TYPE keywords used to admit a date-less-but-otherwise-structured transaction (below).
_BANK_TYPE_RE = re.compile(r'\bwithdrawal\b|\bdeposit\b|\bdebit\b|\bcredit\b|\bpos\b|\bach\b'
                           r'|point of sale|faster payments', re.IGNORECASE)


def _bank_norm(t):
    return t.lower().strip('.:,')


def _bank_cx(w):
    """Horizontal center of a word box."""
    return w['left'] + w['width'] / 2.0


def _bank_num(tok):
    """Signed float of a clean 2-decimal money token, or None when it is OCR-corrupt
    ('my', '300 eb', '-220.0' one-decimal, '~41,99' comma-decimal, trailing junk)."""
    if tok is None:
        return None
    t = tok.strip().rstrip('.,').lstrip('~').replace('$', '')
    neg = t.startswith('-')
    t = t.lstrip('+-').replace(',', '')
    if not re.fullmatch(r'\d+\.\d{2}', t):
        return None
    v = float(t)
    return -v if neg else v


def _bank_label_direction(desc):
    """Direction from the statement's own row TYPE words ('Withdrawal'/'Deposit'), the
    most reliable signal on a single-amount-column form where the balance can spike."""
    if _BANK_OUT_RE.search(desc):
        return 'out'
    if _BANK_IN_RE.search(desc):
        return 'in'
    return None


def _bank_bands(words):
    """Group image_to_data words into rows by shared y-coordinate (a statement row)."""
    ordered = sorted(words, key=lambda w: (w['top'], w['left']))
    bands, cur, cy = [], [], None
    for w in ordered:
        if cy is None or abs(w['top'] - cy) <= _BANK_YBAND:
            cur.append(w)
            cy = w['top'] if cy is None else cy
        else:
            bands.append(cur)
            cur, cy = [w], w['top']
    if cur:
        bands.append(cur)
    return bands


def _bank_header_anchors(page_bands):
    """x-center of each column header on this page, from the band that holds 'balance'
    plus a money column. Rejects a summary line (e.g. 'N Withdrawals = … Deposits =')
    by requiring balance to be the RIGHTMOST header and a money column to its left."""
    for band in page_bands:
        keys = {_bank_norm(w['text']) for w in band}
        if 'balance' not in keys or not ({'deposits', 'amount'} & keys):
            continue
        cols = {}
        for w in band:
            k = _bank_norm(w['text'])
            if k in _BANK_HDR_KEYS and k not in cols:
                cols[k] = _bank_cx(w)
        money = [cols[k] for k in ('withdrawals', 'deposits', 'amount') if k in cols]
        if money and cols['balance'] >= max(money) and cols.get('description', 0) < cols['balance']:
            return cols
    return None


def _extract_bank_rows(pages_words):
    """Geometry pass: turn per-page image_to_data word boxes into structured rows.

    `pages_words` is [{'index': <0-based PDF page>, 'words': [{text,left,top,width,
    height}, …]}, …] — exactly what the test fixtures store, so the tests are hermetic
    (no live OCR). Column anchors are found per page and INHERITED across continuation
    pages that repeat no header. Each row: {page, date, description, amount_raw,
    amount_col, printed, balance, is_tx, is_seed}. Continuation bands (a ZEL* payee, a
    CO:/TYPE: line, an address tail) carry no date/balance and fold into the row above.
    """
    anchors = None
    raw = []
    for page in pages_words:
        idx = page.get('index', 0)
        page_bands = _bank_bands(page.get('words', []))
        found = _bank_header_anchors(page_bands)
        if found:
            anchors = found
        if not anchors:
            continue
        bal_x = anchors['balance']
        money_x = [anchors[k] for k in ('withdrawals', 'deposits', 'amount') if k in anchors]
        desc_x = anchors.get('description', 0)
        # Split point between the money column(s) and the balance column.
        amt_bal_bound = (max(money_x) + bal_x) / 2.0 if money_x else bal_x - 100

        for band in page_bands:
            toks = sorted(band, key=lambda w: w['left'])
            if toks and all(_bank_norm(w['text']) in _BANK_HDR_KEYS for w in toks):
                continue                                    # the column-header band itself
            dates = [w for w in toks if _BANK_DATE_RE.fullmatch(w['text'].strip('.'))]
            nums = [w for w in toks if _BANK_MONEY_RE.fullmatch(w['text'])]
            date = dates[0]['text'] if dates else None

            bal = amt = amt_col = None
            bal_cands = [w for w in nums if _bank_cx(w) >= amt_bal_bound]
            if bal_cands:
                bal = max(bal_cands, key=_bank_cx)          # rightmost number = balance
            amt_cands = [w for w in nums
                         if w is not bal and _bank_cx(w) < amt_bal_bound and _bank_cx(w) > desc_x - 60]
            if amt_cands:
                amt = max(amt_cands, key=_bank_cx)          # number in the money column
            if amt is not None and money_x:
                if 'withdrawals' in anchors and 'deposits' in anchors:
                    amt_col = ('deposits'
                               if abs(_bank_cx(amt) - anchors['deposits']) < abs(_bank_cx(amt) - anchors['withdrawals'])
                               else 'withdrawals')
                else:
                    amt_col = 'amount'
            desc = ' '.join(w['text'] for w in toks
                            if w not in dates and w is not bal and w is not amt
                            and _bank_norm(w['text']) not in _BANK_HDR_KEYS).strip()

            # A row is a transaction when it carries a balance AND either a date OR — when
            # OCR dropped/garbled the date — a printed amount in the money column plus a
            # transaction TYPE word. The second arm recovers a real row that would otherwise
            # be mistaken for continuation prose and folded away (UFCU p38's $40 Faster
            # Payments DEPOSIT: amount 40.00 + balance 816.96 but its date OCR'd to garbage;
            # losing it also corrupted the next row's balance delta). Summary/total lines are
            # excluded here because their totals do not sit in the balance column (bal=None).
            has_type = bool(_BANK_TYPE_RE.search(desc))
            is_tx = bal is not None and (date is not None or (amt is not None and has_type))
            raw.append({
                'page': idx, 'top': min(w['top'] for w in band), 'date': date,
                'description': desc,
                'amount_raw': amt['text'] if amt else None, 'amount_col': amt_col,
                'printed': _bank_num(amt['text']) if amt else None,
                'balance': _bank_num(bal['text']) if bal else None,
                'is_tx': is_tx,
                'is_seed': bool(bal is not None and _BANK_SEED_RE.search(desc)),
            })

    # Fold continuation bands into the previous transaction's description — but only within
    # the SAME page, only across a small vertical gap from the last folded line, and only up
    # to a length cap (see _BANK_FOLD_* above). This stops a page break or a large gap into
    # footer/legal prose from being absorbed as a giant phantom transaction.
    folded = []
    for r in raw:
        if r['is_tx'] or r['is_seed']:
            r['_lasttop'] = r['top']
            folded.append(r)
        elif (folded and r['description']
              and folded[-1]['page'] == r['page']
              and r['top'] - folded[-1]['_lasttop'] <= _BANK_FOLD_MAX_GAP
              and len(folded[-1]['description']) < _BANK_FOLD_MAX_LEN):
            folded[-1]['description'] = (folded[-1]['description'] + ' ' + r['description']).strip()
            folded[-1]['_lasttop'] = r['top']
    for r in folded:
        r.pop('_lasttop', None)
        r.pop('top', None)
    return folded


def _parse_bank_rows(pages_words):
    """Classify each structured row's direction and amount by EVIDENCE (not bank name).

    DIRECTION, most reliable first: the money COLUMN a two-column form put the amount in
    (TDECU withdrawals/deposits) → the statement's explicit Withdrawal/Deposit row LABEL
    (UFCU) → balance movement → printed sign.

    AMOUNT: the printed amount is PRIMARY (geometry isolated its column cleanly); the
    balance delta is a cross-check, and the fallback only when the printed token is
    OCR-corrupt. A balance that spikes far from the anchor and reverts on the next row is
    untrusted: it is neither used for the delta nor carried forward as the anchor. Across
    a gap in the selected pages (e.g. a case file's pages 34→36, page 35 filtered out) the chain is
    broken — the anchor resets so no phantom cross-gap delta is emitted.
    """
    rows = _extract_bank_rows(pages_words)
    out = []
    anchor = None
    prev_page = None
    for i, r in enumerate(rows):
        if r['is_seed']:
            anchor = r['balance']
            prev_page = r['page']
            continue
        if not r['is_tx']:
            continue

        bal = r['balance']
        if prev_page is not None and r['page'] - prev_page > 1:
            anchor = None                                   # page gap → break the chain

        spike = False
        if anchor is not None and bal is not None:
            nxt = next((rows[j]['balance'] for j in range(i + 1, len(rows)) if rows[j]['is_tx']), None)
            if abs(bal - anchor) > _BANK_SPIKE_MIN and nxt is not None and abs(nxt - anchor) < abs(bal - anchor) / 2:
                spike = True

        col_dir = ('out' if r['amount_col'] == 'withdrawals'
                   else 'in' if r['amount_col'] == 'deposits' else None)
        label = _bank_label_direction(r['description'])
        printed = r['printed']
        delta = round(bal - anchor, 2) if (anchor is not None and bal is not None and not spike) else None

        direction = (col_dir or label
                     or (None if delta is None else ('in' if delta > 0 else 'out'))
                     or (None if printed is None else ('in' if printed > 0 else 'out')))

        # amount_alt carries the OTHER candidate whenever printed and a trusted balance
        # delta disagree, so the UI can show both and never present one as unquestioned.
        amount_alt = None
        if printed is not None:
            amount = abs(printed)
            flag = None
            if delta is not None and abs(abs(printed) - abs(delta)) >= 0.01:
                # Printed and a TRUSTED balance delta disagree. `delta` is non-None only
                # when the chain is consistent here (anchor trusted, this balance not a
                # spike, not across a page gap / register boundary), so the delta is
                # reliable. Guarded reconciliation: when the printed amount GROSSLY
                # disagrees with that delta — a ratio at/above _BANK_AMOUNT_RATIO_OUTLIER
                # (~3x, a leading-digit-class miss like UFCU p39 payroll '4,202.85' OCR'd
                # for ~1,202.85) — the printed token is almost certainly an OCR blunder, so
                # correct to the balance amount, but never silently. Smaller disagreements
                # stay printed-primary (printed is usually the clean column) BUT surface both
                # candidates: e.g. UFCU p37's $50 zelle DEPOSIT OCR's as '50.06' (conf 76 —
                # confidently wrong) while the balance moves exactly 50.00. The 'manual' floor
                # still wins wherever delta is None.
                hi, lo = max(abs(printed), abs(delta)), min(abs(printed), abs(delta))
                if lo > 0 and hi / lo >= _BANK_AMOUNT_RATIO_OUTLIER:
                    amount = abs(delta)
                    amount_alt = abs(printed)               # the suspect printed token
                    flag = 'verify: amount corrected from balance, printed OCR suspect'
                else:
                    amount_alt = abs(delta)                 # the balance-movement candidate
                    flag = 'verify: balance delta disagrees'
        elif delta is not None:
            amount = abs(delta)
            flag = 'verify: amount recovered from balance'
        else:
            amount = None
            flag = 'manual: unreadable'

        if not spike and bal is not None:
            anchor = bal                                    # advance only to a trusted balance
        prev_page = r['page']

        # page: 0-based OCR/PDF index (internal — arrays, rasterization, navigation).
        # page_number: 1-based, for human display ONLY. Emit both; the UI must show
        # page_number and never render the raw 0-based index.
        out.append({
            'date': r['date'], 'description': r['description'],
            'type': _classify_tx_type(r['description']),
            'page': r['page'], 'page_index': r['page'], 'page_number': r['page'] + 1,
            'balance': bal, 'amount': amount, 'amount_alt': amount_alt,
            'direction': direction, 'flag': flag,
        })
    return out


def _parse_bank_transactions(pages_words):
    """Income view: only inflows (direction == 'in') across the selected pages."""
    return [r for r in _parse_bank_rows(pages_words) if r.get('direction') == 'in']


class BankPdfError(ValueError):
    """The bank 'pdf' payload decoded, but is not a renderable PDF. Raised ONLY for that
    recognized client-input failure so the route can map it to a controlled 400; every
    other failure (poppler missing, tesseract error, a parser bug) propagates normally."""


def _ocr_bank_pages_tsv(pdf_bytes, indices):
    """LIVE second OCR pass for the bank path: image_to_data (word boxes) on just the
    selected statement pages, at the same 200 DPI as /api/ocr. Returns the structured
    input _extract_bank_rows expects. Not unit-tested (needs the page image); the tests
    fixture this pass's output. Isolated to the bank path — the global PSM-3 text pass
    is untouched, so every other consumer keeps reading identical text."""
    from pdf2image import convert_from_bytes
    from pdf2image.exceptions import PDFPageCountError, PDFSyntaxError
    import pytesseract
    pages_words = []
    for idx in indices:
        if idx is None:
            continue
        try:
            imgs = convert_from_bytes(pdf_bytes, dpi=200, first_page=idx + 1, last_page=idx + 1)
        except (PDFPageCountError, PDFSyntaxError) as e:
            # Recognized "not a renderable PDF" — surface as the one client-input error the
            # route converts to 400. Poppler-not-installed and any other fault are distinct
            # exception types, so they are NOT swallowed here.
            raise BankPdfError(str(e)) from e
        if not imgs:
            continue
        d = pytesseract.image_to_data(imgs[0], lang='eng', output_type=pytesseract.Output.DICT)
        words = [{'text': d['text'][i], 'left': d['left'][i], 'top': d['top'][i],
                  'width': d['width'][i], 'height': d['height'][i]}
                 for i in range(len(d['text'])) if d['text'][i].strip()]
        pages_words.append({'index': idx, 'words': words})
    return pages_words


# ── Checklist page locator ────────────────────────────────────────────────────
# The FILE APPROVAL CHECKLIST is a fixed Bonner Carrington template: the static
# labels are identical on every file, only the checkmarks/values vary. It's
# usually near the front of a scanned PDF but NOT always page 1. We fingerprint
# pages by fuzzy-matching invariant template phrases (never names, dates, unit
# numbers, or checkbox state) so the result is independent of file contents.
#
# Edit CHECKLIST_ANCHORS to tune the fingerprint. Each entry is (phrase, weight):
#   PILLARS     (weight 5) — strong structural identity; also gate the candidate
#   FIELD LABELS (weight 2)
#   LINE ITEMS   (weight 1)
CHECKLIST_ANCHORS = [
    # ── Pillars (weight 5) ──
    ("FILE APPROVAL CHECKLIST", 5),
    ("APPROVAL DOCUMENTATION", 5),
    ("MANAGER FILE APPROVAL", 5),
    # ── Field labels (weight 2) ──
    ("Property Name", 2),
    ("HOH Name", 2),
    ("Unit #", 2),
    ("BR Size", 2),
    ("Estimated Move In Date", 2),
    ("Recert Effective Date", 2),
    ("DESIRED PROGRAM DESIGNATION", 2),
    ("DESIRED INCOME SET ASIDE", 2),
    ("ENTER THE TENANT RENT FOR REQUESTED SET ASIDE", 2),
    ("UTILITY ALLOWANCE", 2),
    ("RESIDENTS", 2),
    # ── Line items (weight 1) ──
    ("Items Required Prior to Move-in", 1),
    ("Household Certification", 1),
    ("Tenant Income Certification", 1),
    ("Tenant Release and Consent", 1),
    ("Income Calculation Worksheet", 1),
    ("Tenant Selection Plan", 1),
    ("Credit Report Reviewed", 1),
    ("File Approved for Compliance Review", 1),
]

# Pillars carry weight 5 and also gate whether a page is even a candidate.
_PILLAR_WEIGHT = 5


def _normalize(s: str) -> str:
    """Uppercase, drop non-alphanumerics, collapse whitespace to single spaces."""
    return re.sub(r'[^A-Z0-9]+', ' ', s.upper()).strip()


def _anchor_ratio(anchor: str, text: str) -> float:
    """
    Fuzzy similarity (0..100) of an anchor phrase against page text.

    Uses rapidfuzz partial_ratio / token_set_ratio when available (tolerant of the
    noise OCR produces on low-DPI scans). Falls back to a normalized substring
    match (100 if present, else 0) so the module still works without rapidfuzz.
    """
    if _HAVE_RAPIDFUZZ:
        a = anchor.upper()
        t = text.upper()
        return max(fuzz.partial_ratio(a, t), fuzz.token_set_ratio(a, t))
    return 100.0 if _normalize(anchor) in _normalize(text) else 0.0


def score_checklist_page(text: str) -> dict:
    """
    Fingerprint one page's OCR text against the checklist template.

    Counts anchors present at fuzzy ratio >= 80 (OCR is noisy at low DPI, so we
    stay lenient). A page is only a candidate if >= 2 of the 3 pillar anchors are
    present (the STRUCTURAL GATE); otherwise its score is forced to 0. The score
    is the matched anchor weight divided by the total possible weight (0..1).

    Returns {"score": float, "pillars": int, "matched": [anchor phrases]}.
    """
    matched = []
    matched_weight = 0
    total_weight = 0
    pillars = 0

    for phrase, weight in CHECKLIST_ANCHORS:
        total_weight += weight
        if _anchor_ratio(phrase, text) >= 80:
            matched.append(phrase)
            matched_weight += weight
            if weight == _PILLAR_WEIGHT:
                pillars += 1

    # Structural gate: without >= 2 pillars it isn't the checklist page.
    if pillars < 2 or total_weight == 0:
        score = 0.0
    else:
        score = matched_weight / total_weight

    return {"score": score, "pillars": pillars, "matched": matched}


def locate_checklist_page(pdf_bytes, max_pages=10, threshold=0.55, early_exit=0.85) -> dict:
    """
    Find the checklist page by rasterizing pages ONE at a time at low DPI (150)
    and fingerprinting each. Short-circuits as soon as a page clears `early_exit`
    (the checklist is usually near the front, so this keeps the common case fast).
    Otherwise, after scanning up to `max_pages`, returns the best page if it clears
    `threshold`, else page_index=None.

    Returns {"page_index": int|None, "confidence": float,
             "matched_anchors": [...], "pillars_found": int}.
    """
    from pdf2image import convert_from_bytes
    import pytesseract

    best = {"page_index": None, "confidence": 0.0,
            "matched_anchors": [], "pillars_found": 0}

    for i in range(1, max_pages + 1):
        # Render exactly this page — never the whole document up front.
        imgs = convert_from_bytes(pdf_bytes, dpi=150, first_page=i, last_page=i)
        if not imgs:
            break  # past the end of the document

        text = pytesseract.image_to_string(imgs[0], lang='eng')
        result = score_checklist_page(text)

        logger.debug(
            "checklist locate: page=%d score=%.3f pillars=%d matched=%s",
            i, result["score"], result["pillars"], result["matched"],
        )

        if result["score"] > best["confidence"]:
            best = {
                "page_index": i - 1,
                "confidence": result["score"],
                "matched_anchors": result["matched"],
                "pillars_found": result["pillars"],
            }

        if result["score"] >= early_exit:
            logger.debug("checklist locate: early exit on page %d (>= %.2f)", i, early_exit)
            return {
                "page_index": i - 1,
                "confidence": result["score"],
                "matched_anchors": result["matched"],
                "pillars_found": result["pillars"],
            }

    if best["confidence"] >= threshold:
        return best

    return {"page_index": None, "confidence": best["confidence"],
            "matched_anchors": best["matched_anchors"],
            "pillars_found": best["pillars_found"]}


# ── Checklist detection logic ─────────────────────────────────────────────────

# Each entry: (profile_value, group, [keywords_to_try_most_specific_first]).
# Shared by BOTH the text pass (detect_checklist_profile → _is_checked) and the
# pixel pass (detect_checklist_profile_pixels): the keyword strings double as the
# label phrases the pixel pass locates via image_to_data before measuring the box.
CHECKLIST_ITEMS = [
    # Income sources
    ('employed',        'income', ['Employment Verification', 'Paystubs']),
    ('school_employee', 'income', ['School Employee Questionnaire', 'School Employee']),
    ('self_employed',   'income', ['Self-Employment Affidavit', 'Self Employment Affidavit']),
    ('new_business',    'income', ['Profit and Loss if New Business']),
    ('gig_income',      'income', ['Gig Income Verification', 'Gig Income']),
    ('social_security', 'income', ['Social Security or Retirement Verification']),
    ('pension',         'income', ['Pension / Retirement Benefit', 'Pension Verification']),
    ('section8',        'income', ['Income Verification for Households with Section 8']),
    ('zero_income',     'income', ['Certification of Zero Income']),
    ('non_employed',    'income', ['Non-Employed Certification']),
    ('unemployment',    'income', ['Unemployment Benefits Verification']),
    ('tips',            'income', ['Tips and Commissions Affidavit']),
    ('child_support',   'income', ['Child Support/Alimony Certification',
                                   'Child Support Alimony Certification']),
    ('recurring_gift',  'income', ['Recurring Gift Affidavit']),
    ('rental_income',   'income', ['Rental Payment Worksheet']),

    # Household conditions
    ('special_needs',      'conditions', ['Special Needs Certification']),
    ('live_in_aide',       'conditions', ['Live in Care Attendant Affidavit',
                                          'Live-in Care Attendant Affidavit']),
    ('marital_separation', 'conditions', ['Marital Separation Certification',
                                          'Maritial Separation Certification']),
    ('student',            'conditions', ['Student Verification', 'Certification of Student Eligibility']),
    ('has_minor_children', 'conditions', ['Birth Certificate', 'Minor Child']),
    # 'multiple_adults' removed 2026-06-18 (Bug 5): no such required doc — adult count
    # affects the RIS income threshold, not the document set. Nothing reads this flag.
    ('homeowner',          'conditions', ['Home Ownership Documents']),
    ('home_listing',       'conditions', ['Listing Contract']),
    ('new_court_order',    'conditions', ['Court Order']),
    ('assets_under_50k',   'conditions', [r'Under .{0,3}50,000 Asset',
                                          'Under 50,000 Asset Certification']),
    ('assets_over_50k',    'conditions', ['Bank Statement or Bank Verification']),
]


def detect_checklist_profile(text: str) -> dict:
    """
    Parse page 1 OCR from the Bonner Carrington FILE APPROVAL CHECKLIST.

    How Tesseract reads the checkboxes in this specific form:
      Checked   (☑) → various chars: @  2}  2]  [|  |  ~]  4  i}  z]  q  a
      Unchecked (☐) → O  Oo  o   — or sometimes nothing at all

    Detection strategy for each item:
      - Look at 1-6 chars immediately before the keyword in the OCR text.
      - Ends in O/o (short run) → UNCHECKED
      - Ends in any other non-whitespace char → CHECKED
      - Empty / whitespace / newline before keyword → UNCHECKED (conservative default)
    """
    profile: dict = {'program': None, 'income': [], 'conditions': []}

    # ── Program designation ───────────────────────────────────────────────────
    # The form reads "HTC [☑/☐]  HOME [☑/☐]" so the checkbox char sits AFTER
    # the program name.  Checked ☑ → a letter like 'a'; unchecked ☐ → nothing.
    for line in text.split('\n'):
        if 'HTC' not in line.upper():
            continue
        m = re.search(r'HTC\s*(.{0,4})\s*HOME', line, re.IGNORECASE)
        if m:
            between = m.group(1).strip()
            if between and between.upper() not in ('O', 'OO', ''):
                profile['program'] = 'HTC'
                break
        m = re.search(r'HOME\s*(.{0,4})\s*BOND', line, re.IGNORECASE)
        if m:
            between = m.group(1).strip()
            if between and between.upper() not in ('O', 'OO', ''):
                profile['program'] = 'HOME'
                break
        break

    if profile['program'] is None:
        for line in text.split('\n'):
            if 'BOND' not in line.upper():
                continue
            m = re.search(r'BOND\s*(.{0,4})\s*Other', line, re.IGNORECASE)
            if m:
                between = m.group(1).strip()
                if between and between.upper() not in ('O', 'OO', ''):
                    profile['program'] = 'BOND'
            break

    # ── Checklist items ───────────────────────────────────────────────────────
    # Item table is module-level (CHECKLIST_ITEMS) so the pixel pass shares it.
    for value, group, keywords in CHECKLIST_ITEMS:
        if _is_checked(text, keywords):
            if group == 'income' and value not in profile['income']:
                profile['income'].append(value)
            elif group == 'conditions' and value not in profile['conditions']:
                profile['conditions'].append(value)

    # Under/over $50k are mutually exclusive. When both fire (OCR bleed between adjacent
    # checkboxes is common), remove BOTH so the user sets it manually — picking one would
    # be a 50/50 guess that causes exactly the wrong pill to appear in the UI.
    if 'assets_under_50k' in profile['conditions'] and 'assets_over_50k' in profile['conditions']:
        profile['conditions'].remove('assets_under_50k')
        profile['conditions'].remove('assets_over_50k')

    return profile


def _is_checked(text: str, keywords: list) -> bool:
    """
    Return True if any keyword is found and preceded by a checked-box marker.

    Look-back window: 15 chars (was 6) so a checkbox placed on the line above
    the label — common in two-column OCR output — is still captured.
    Newlines in the window are collapsed to spaces before analysis, so
    'a\\nSpecial Needs Certification' is treated identically to 'a Special Needs Certification'.

    Checked box chars seen in this form's OCR output:
      @  |  ~  [  ]  {  }  (special chars)
      2  4  (digits — appear as '2]', '4 ', etc.)
      Isolated letters: a, i, z, q (preceded by a space — not part of a word)

    Unchecked (☐): O / o
    Regular text: punctuation  )  (  .  ,  -  or any letter that's part of a word
    """
    for kw in keywords:
        try:
            pattern = re.compile(kw, re.IGNORECASE)
        except re.error:
            pattern = re.compile(re.escape(kw), re.IGNORECASE)

        m = pattern.search(text)
        if not m:
            continue

        # A keyword that is FOUND but reads UNCHECKED does NOT end the search. One
        # profile item can carry several DISTINCT checklist boxes (notably 'student' →
        # both 'Student Verification' AND 'Certification of Student Eligibility'); the
        # item is checked if ANY of its boxes is checked. So an unchecked read falls
        # through (`continue`) to the next keyword; only a checked read returns early.
        # (Case A p3: 'Student Verification' (line 41) reads unchecked at a line
        # start, but '4 Certification of Student Eligibility' (line 39) is checked —
        # under the old first-found-wins logic the first keyword masked the second and
        # the box was wrongly read as unchecked.)
        pos = m.start()

        # Text from start of this line up to the keyword — avoids cross-line bleed.
        line_start = text.rfind('\n', 0, pos)
        line_start = line_start + 1 if line_start >= 0 else 0
        line_prefix = text[line_start:pos].rstrip(' \t')

        if not line_prefix:
            # Keyword is at line start — check end of previous line for a stray
            # checked char (two-column OCR sometimes displaces the marker there).
            if line_start > 1:
                prev_end = text[max(0, line_start - 20):line_start - 1].rstrip(' \t')
                if prev_end and prev_end[-1] in '@|~[]{}':
                    return True
            continue

        last = line_prefix[-1]

        # A checkbox that OCRs as a bracket PAIR must be judged by its INTERIOR, not by
        # the closing bracket. An EMPTY box outline reads as edges only — '[]', '[_]',
        # '[-]', '(_]' (the interior is nothing / an underscore / a hyphen / the box's
        # bottom stroke) — while a CHECKED box carries the checkmark glyph inside:
        # '[Y]', '[vy]', '[¥]'. The old rule looked only at the trailing ']' and so read
        # EVERY bracketed box as checked, turning empty boxes into false positives
        # (one case file: 11 empty boxes flagged; Rental Payment Worksheet on multiple files).
        # Close-bracket set is ']}' only — deliberately NOT ')', because parenthetical
        # form text like '(if applicable)' legitimately ends in ')' with text inside and
        # would otherwise read as a checkmark. A trailing bracket with no matching open
        # bracket falls through to the legacy single-char rules below (unchanged).
        if last in ']}':
            op = max(line_prefix.rfind(c) for c in '[({')
            if op >= 0:
                interior = line_prefix[op + 1:len(line_prefix) - 1]
                if re.sub(r'[\s_\-|.=]', '', interior) == '':
                    continue      # empty box outline → UNCHECKED
                return True        # a mark sits inside the box → CHECKED

        # A prefix that is *exactly* a lone '1' is the OCR of an empty box's vertical
        # edge (☐) — or a stray list-item number — NOT a checked-box fill. This must
        # be tested on the WHOLE prefix, not `last`, so it does not catch a multi-char
        # checked glyph that merely ends in '1' (e.g. '21' before 'Household
        # Certification', which is genuinely checked). (Case A p3 line 38:
        # '1 Maritial Separation Certification' — box unchecked; the '1' is the empty
        # box's stroke, not the checked-digit artifact the bare isdigit() rule below
        # would mistake it for.)
        if line_prefix.strip() == '1':
            continue

        if last in ('O', 'o', '0'):
            continue  # empty box: letter-O or zero-glyph both read as ☐

        if last in '@|~[]{}':
            return True  # checked box symbol

        if last.isdigit():
            return True  # digit directly before a doc title = checkbox artifact

        if last.isalpha():
            prev = line_prefix[-2] if len(line_prefix) >= 2 else ''
            if prev in (' ', '\t'):
                return True   # lone letter → checkbox artifact
            continue          # letter is part of a word → this occurrence unchecked

        continue  # punctuation → regular text, this occurrence unchecked

    return False  # no keyword's box read as checked


# ── Pixel-based checkbox detection ────────────────────────────────────────────
# The text pass above reads the checkbox from the CHARACTER Tesseract transcribes
# in front of the label ('@', a digit, 'O', …). Tesseract frequently DROPS the
# checkmark glyph entirely (it emits nothing before the label), and on this form's
# two-column layout an empty-vs-checked box is then indistinguishable from text —
# e.g. Case A's Special Needs and Bank Statement boxes are visibly checked (☑) but
# their glyph is lost at every DPI, so the text pass reads them UNCHECKED. Those are
# false negatives no text heuristic can fix (an empty-prefix box is genuinely
# ambiguous: on the same page, Special Needs is checked-but-dropped while Student
# Verification is unchecked-and-dropped).
#
# So we ALSO look at the actual pixels: find each label's bounding box via
# image_to_data, then measure ink in the CENTER of the small square to its left. An
# empty box (☐) is a hollow outline → white center → ~0.0 fill; a checked box (☑/☒)
# has a mark crossing the center → ~0.20–0.26 fill. Measured across all three real
# case files the separation is clean (checked ≥0.19, unchecked exactly 0.0), so a
# 0.10 threshold is safe. The result is UNIONed with the text pass (see
# scan_checklist): pixels recover dropped-glyph checkmarks; text covers the rare
# label the image_to_data word-sequence match can't locate (a number/hyphen token
# splitting the label). Neither pass can regress the other.
_CHECKBOX_INK_THRESHOLD = 0.10


def _locate_label_boxes(words, phrase):
    """Find where a checklist label appears in an image_to_data word list.

    `words` is a list of (text, left, top, width, height). Returns a list of
    (left, top, width, height) tuples for the FIRST word of each occurrence of
    `phrase`. Matching uses the label's alphabetic tokens (len ≥ 3, so connectors
    like "of"/"or" and the "$50,000" in "Under $50,000 Asset Certification" drop
    out) and must be CONTIGUOUS: between two label tokens only SHORT image words
    (clean length < 3 -- an OCR'd number/symbol like "$50,000", or a dropped
    connector like "or"/"of") may be skipped, never another full word. Allowing
    arbitrary alphabetic filler was too loose and mis-matched a "Zero Income" label
    onto "Asset Certification" in the opposite column.
    """
    toks = [t for t in re.sub(r'[^a-z ]', ' ', phrase.lower()).split() if len(t) >= 3]
    if not toks:
        return []
    clean = [re.sub(r'[^a-z]', '', w[0].lower()) for w in words]
    hits = []
    for k in range(len(words)):
        if not clean[k].startswith(toks[0]):
            continue
        kk = k
        ok = True
        for tk in toks[1:]:
            found = False
            while True:
                kk += 1
                if kk >= len(words):
                    break
                if len(clean[kk]) < 3:
                    continue  # number/punctuation or dropped connector — skip, don't fail
                found = clean[kk].startswith(tk[:4])
                break         # first full word must match, else no match
            if not found:
                ok = False
                break
        if ok:
            hits.append(words[k][1:])
    return hits


def _checkbox_box_ink(px, W, H, l, tp, h):
    """Locate the checkbox left of a label word and measure its center ink.

    Returns (located, ink):
      - located: True iff a checkbox-sized SQUARE was bracketed in the band left of the
        label (passes the dark-strokes + squareness gate). This is what lets the caller
        tell a box that is MEASURED empty (located=True, ink~0.0) apart from a box that
        could not be found at all (located=False) — the two used to be indistinguishable
        because both returned 0.0, and the difference is what makes a pixel-empty→veto
        of the text pass safe (only veto when we actually saw an empty box).
      - ink: dark-pixel fraction of the box's inner region (border margin excluded).
        Empty box → ~0.0; checked box → ~0.2+. 0.0 when not located.
    """
    THR = 128
    x1 = max(0, l - int(h * 3.0)); x2 = max(0, l - int(h * 0.6))
    y1 = max(0, tp - int(h * 0.25)); y2 = min(H, tp + int(h * 1.15))
    if x2 - x1 < 5 or y2 - y1 < 5:
        return (False, 0.0)
    # Columns that are substantially dark = the box's vertical strokes.
    cols = [x for x in range(x1, x2)
            if sum(1 for y in range(y1, y2) if px[x, y] < THR) >= 0.4 * (y2 - y1)]
    if len(cols) >= 2:
        bx1, bx2 = min(cols), max(cols)
    else:
        darkcols = [x for x in range(x1, x2)
                    if any(px[x, y] < THR for y in range(y1, y2))]
        if len(darkcols) < 3:
            return (False, 0.0)
        bx1, bx2 = min(darkcols), max(darkcols)
    darkrows = [y for y in range(y1, y2)
                if any(px[x, y] < THR for x in range(bx1, bx2 + 1))]
    if len(darkrows) < 3:
        return (False, 0.0)
    by1, by2 = min(darkrows), max(darkrows)
    # Reject anything that isn't roughly a checkbox-sized square (stray glyph/margin).
    if (bx2 - bx1) < 0.4 * h or (bx2 - bx1) > 2.2 * h:
        return (False, 0.0)
    mx = int((bx2 - bx1) * 0.28); my = int((by2 - by1) * 0.28)
    ix1, ix2, iy1, iy2 = bx1 + mx, bx2 - mx, by1 + my, by2 - my
    if ix2 <= ix1 or iy2 <= iy1:
        return (False, 0.0)
    dark = sum(1 for x in range(ix1, ix2) for y in range(iy1, iy2) if px[x, y] < THR)
    return (True, dark / ((ix2 - ix1) * (iy2 - iy1)))


def _checkbox_center_ink(px, W, H, l, tp, h):
    """Center ink fraction of the checkbox left of a label word (0.0 if none found).

    Thin wrapper over _checkbox_box_ink for callers that only need the ink value.
    """
    return _checkbox_box_ink(px, W, H, l, tp, h)[1]


def detect_checklist_profile_pixels(img) -> dict:
    """Pixel-based checkbox read of the located checklist page (income + conditions).

    Complements the text pass (detect_checklist_profile); the caller unions the two and
    then applies the confident-empty VETO below.

    For each item this computes a tri-state from the actual box pixels:
      - CHECKED  → some located box's center ink ≥ threshold. Added to income/conditions
                   (recovers checkmarks whose glyph Tesseract dropped, e.g. the asset
                   boxes and Special Needs).
      - EMPTY    → EVERY keyword-label's box was located AND none is checked. Added to
                   result['empty'][group]. The caller removes these from the union, which
                   VETOES a text-pass false positive where an empty box outline OCRs as a
                   checkmark artifact ('[]', '(1', a stray digit). Requiring that ALL of
                   an item's boxes were located is what makes the veto safe: an item like
                   'student' (two boxes: 'Student Verification' + 'Certification of Student
                   Eligibility') where the CHECKED box sits too far from its label to be
                   located falls into UNKNOWN, not EMPTY, so a genuine check is never
                   vetoed away.
      - UNKNOWN  → a box could not be located (punctuation-joined label, displaced box).
                   Neither asserted nor vetoed; the text pass decides.

    Program designation (HTC/HOME/BOND) is left to the text pass — those boxes sit AFTER
    the label, a different geometry, and the text pass already handles them. Any failure
    returns an empty profile so this can never break the pipeline.
    """
    result = {'program': None, 'income': [], 'conditions': [],
              'empty': {'income': [], 'conditions': []}}
    try:
        import pytesseract
        gray = img.convert('L')
        W, H = gray.size
        px = gray.load()
        data = pytesseract.image_to_data(gray, lang='eng',
                                         output_type=pytesseract.Output.DICT)
        words = [(data['text'][i].strip(), data['left'][i], data['top'][i],
                  data['width'][i], data['height'][i])
                 for i in range(len(data['text'])) if data['text'][i].strip()]
        for value, group, keywords in CHECKLIST_ITEMS:
            best = 0.0
            kw_located = 0
            for kw in keywords:
                located_this = False
                for (l, tp, w, h) in _locate_label_boxes(words, kw):
                    located, ink = _checkbox_box_ink(px, W, H, l, tp, h)
                    if located:
                        located_this = True
                        if ink > best:
                            best = ink
                if located_this:
                    kw_located += 1
            if best >= _CHECKBOX_INK_THRESHOLD:
                if value not in result[group]:
                    result[group].append(value)
            elif kw_located == len(keywords) and kw_located > 0:
                # every label's box was found and none is checked → confidently EMPTY
                result['empty'][group].append(value)
    except Exception as e:
        logger.warning('pixel checkbox pass skipped: %s', e)
    return result


# ── Browser launcher ──────────────────────────────────────────────────────────

def open_browser():
    import time
    time.sleep(1.5)
    webbrowser.open('http://localhost:5050')


if __name__ == '__main__':
    print("=" * 50)
    print("  BC File Checker — http://localhost:5050")
    print("  Opening browser automatically...")
    print("  Press Ctrl+C to stop the server.")
    print("=" * 50)
    # Only open the browser in the parent process — the reloader spawns a child
    # process (WERKZEUG_RUN_MAIN=true) and we don't want two tabs opening.
    if os.environ.get('WERKZEUG_RUN_MAIN') != 'true':
        threading.Thread(target=open_browser, daemon=True).start()
    # Frozen (.app) must NOT use the debug reloader: it re-execs the interpreter,
    # which would relaunch the whole bundle. Non-frozen keeps full local-dev
    # behavior (debug + reloader on), exactly as before.
    app.run(host='127.0.0.1', port=5050,
            debug=not FROZEN, use_reloader=not FROZEN, threaded=True)

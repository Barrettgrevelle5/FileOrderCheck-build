from flask import Flask, request, jsonify, send_from_directory, Response, stream_with_context
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
APP_VERSION = '2026.07.22'

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
        req = urllib.request.Request(
            UPDATE_MANIFEST_URL, headers={'User-Agent': 'FileOrderCheck'})
        with urllib.request.urlopen(req, timeout=4) as resp:
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
        profile = {
            'program':    p200['program'] or p250['program'],
            'income':     list(dict.fromkeys(p200['income'] + p250['income'])),
            'conditions': list(dict.fromkeys(p200['conditions'] + p250['conditions'])),
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
    data = request.get_json(force=True, silent=True)
    if not data or 'pages' not in data:
        return jsonify({'error': 'Missing pages'}), 400

    seen_keys = set()
    all_transactions = []

    for page in data['pages']:
        text = page.get('text', '')
        page_idx = page.get('index', 0)
        for tx in _parse_bank_transactions(text, page_idx):
            # Deduplicate within a page only — different pages = different payroll deposits.
            key = (tx['type'], tx['description'][:60], tx.get('date'), tx.get('page'))
            if key not in seen_keys:
                seen_keys.add(key)
                all_transactions.append(tx)

    return jsonify({'transactions': all_transactions})


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


def _strip_zelle_ref(desc):
    """Remove trailing Zelle reference codes (12+ char alphanumeric tokens) from description."""
    return re.sub(r'\s+[A-Za-z0-9]{12,}\s*$', '', desc).strip()


def _normalize_amount(s):
    """Normalize OCR sign artifacts: ~12.00 → -12.00, "12.00 → -12.00."""
    if s and s[0] in ('~', '“', '”', '"'):
        return '-' + s[1:]
    return s


def _parse_bank_transactions(page_text, page_index=0):
    """Extract income-related transactions from one bank statement OCR page.

    Strategy 1 — full structured line (pages where each tx is one line):
        MM/DD [MM/DD] TYPE DESCRIPTION AMOUNT [BALANCE]
        Handles both Deposit and Withdrawal Faster Payments; looks ahead for ZEL* payee.
    Strategy 2 — ACH payroll block (fragmented multi-column pages):
        Finds TYPE: PAYROLL keyword; pulls amount from the AMOUNT column block.
    Strategy 3 — P2P keyword lines (ZEL*, VENMO, etc.):
        Falls back to backward-scan for amount when on structured pages.
    """
    transactions = []
    lines = [l.strip() for l in page_text.split('\n') if l.strip()]

    # ── AMOUNT block (fragmented pages) ───────────────────────────────────────
    # Handles "AMOUNT\n...BALANCE\n" (page 36) and "AMOUNT BALANCE\n..." (page 37+).
    _amount_block = None
    _ab = re.search(r'\bAMOUNT(?:\s+BALANCE)?\b\s*\n(.*?)$', page_text, re.DOTALL | re.IGNORECASE)
    if _ab:
        _amount_block = _ab.group(1)

    def _first_large_credit(min_val=300):
        """First unsigned-positive amount ≥ min_val in the AMOUNT block = payroll credit."""
        if not _amount_block:
            return None
        for aline in _amount_block.split('\n'):
            aline = aline.strip()
            if not aline or aline[0] in ('-', '~', '“', '”', '"'):
                continue
            am = re.match(r'^\+?(\d{1,3}(?:,\d{3})*\.\d{2})$', aline)
            if am:
                val = float(am.group(1).replace(',', ''))
                if val >= min_val:
                    return f'+${am.group(1)}'
        return None

    # ── Strategy 1: full structured line ──────────────────────────────────────
    structured_re = re.compile(
        r'^(\d{1,2}/\d{2})'
        r'(?:\s+\d{1,2}/\d{2})?'
        r'\s+(Withdrawal|Deposit|Credit)'
        r'\s+(.+?)'
        r'\s+([+\-~""“”]?\$?\d{1,3}(?:,\d{3})*\.\d{2})'
        r'(?:\s+\d{1,3}(?:,\d{3})*\.\d{2})?$',
        re.IGNORECASE,
    )
    zel_absorbed = set()  # lines[] indices whose ZEL* was merged into a Strategy 1 hit

    for i, line in enumerate(lines):
        m = structured_re.match(line)
        if not m:
            continue
        date, tx_type, desc, amount = m.group(1), m.group(2), m.group(3).strip(), m.group(4)
        is_deposit = tx_type.lower() in ('deposit', 'credit')
        is_faster = bool(re.search(r'faster\s*payments?', desc, re.IGNORECASE))
        is_payroll_ach = is_deposit and bool(re.search(r'\bach\b', desc, re.IGNORECASE))

        # Surface deposits and Faster Payments transfers (withdrawal or deposit)
        if not is_deposit and not is_faster:
            continue

        # Look ahead for ZEL* continuation line (Faster Payments → Zelle pattern)
        zelle_payee = None
        if is_faster and i + 1 < len(lines):
            next_line = lines[i + 1]
            if re.match(r'^zel[*\s]', next_line, re.IGNORECASE):
                zelle_payee = _strip_zelle_ref(next_line)
                zel_absorbed.add(i + 1)

        # Look ahead for TYPE: PAYROLL on ACH deposit lines
        if is_payroll_ach:
            for k in range(i + 1, min(i + 5, len(lines))):
                if re.search(r'\btype\s*:\s*payroll\b', lines[k], re.IGNORECASE):
                    employer = re.sub(r'(?i)^ach\s+', '', desc).strip()
                    desc = f'PAYROLL — {employer}'
                    break

        final_desc = zelle_payee if zelle_payee else desc
        transactions.append({
            'date': date,
            'description': final_desc,
            'amount': _normalize_amount(amount),
            'type': _classify_tx_type(final_desc),
            'page': page_index + 1,
            'direction': 'in' if is_deposit else 'out',
        })

    # ── Strategy 2: ACH payroll block (fragmented pages) ──────────────────────
    payroll_credit = _first_large_credit(min_val=300)

    for i, line in enumerate(lines):
        if not re.search(r'\btype\s*:\s*payroll\b', line, re.IGNORECASE):
            continue
        employer_line = lines[i - 1] if i > 0 else ''
        # Handle both "ACH EMPLOYER" and "MM/DD MM/DD Deposit ACH EMPLOYER" formats
        ach_m = re.search(r'\bACH\s+(.+?)(?:\s+TYPE:|\s*$)', employer_line, re.IGNORECASE)
        if ach_m:
            employer = ach_m.group(1).strip()
        else:
            employer = re.sub(r'(?i)^ach\s+', '', employer_line).strip()
        if not employer or len(employer) <= 2:
            continue
        # Skip if Strategy 1 already captured a payroll (date is set on structured pages)
        if any(t['type'] == 'payroll' and t.get('date') for t in transactions):
            continue
        transactions.append({
            'date': None,
            'description': f'PAYROLL — {employer}',
            'amount': payroll_credit,
            'type': 'payroll',
            'page': page_index + 1,
            'direction': 'in',
        })

    # ── Strategy 3: P2P keyword lines (ZEL*, VENMO, CASH APP, etc.) ──────────
    p2p_re = re.compile(
        r'^(ZEL\*\s*\S+(?:\s+\S+)?'
        r'|VENMO\b[^\n]*'
        r'|CASH\s*APP\b[^\n]*'
        r'|CASHAPP\b[^\n]*'
        r'|PAYPAL\b[^\n]*)',
        re.IGNORECASE,
    )
    for i, line in enumerate(lines):
        if i in zel_absorbed:
            continue
        m = p2p_re.match(line)
        if not m:
            continue
        desc = _strip_zelle_ref(m.group(1).strip())
        if len(desc.split()) > 7:
            continue

        # On structured pages the ZEL* line follows a "Faster Payments MM/DD AMOUNT" line —
        # scan backward to pick up that date and amount.
        amount = None
        date = None
        for j in range(i - 1, max(i - 4, -1), -1):
            prev = lines[j]
            if re.search(r'faster\s*payments?', prev, re.IGNORECASE):
                am = re.search(r'([+\-~""""]?\$?\d{1,3}(?:,\d{3})*\.\d{2})', prev)
                if am:
                    amount = _normalize_amount(am.group(1))
                dm = re.match(r'^(\d{1,2}/\d{2})', prev)
                if dm:
                    date = dm.group(1)
                break

        transactions.append({
            'date': date,
            'description': desc,
            'amount': amount,
            'type': 'p2p',
            'page': page_index + 1,
        })

    return transactions


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
    # Each entry: (profile_value, group, [keywords_to_try_most_specific_first])
    ITEMS = [
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

    for value, group, keywords in ITEMS:
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

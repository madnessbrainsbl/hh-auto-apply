"""Bounded CLI catalog discovery and reference-price ranking, without credentials."""

import json
import logging
import math
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from html.parser import HTMLParser

OPENAI_PRICING_URL = 'https://developers.openai.com/api/docs/pricing'


@contextmanager
def temporary_cli_directory(prefix):
    path = tempfile.mkdtemp(prefix=prefix)
    try:
        yield path
    finally:
        # Python 3.10 TemporaryDirectory retries locked directories recursively.
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            pass
        except OSError as e:
            logging.getLogger(__name__).warning(
                'Временная папка CLI пока недоступна для удаления (%s); работа продолжается',
                type(e).__name__)


class _PriceTables(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables, self.table, self.row, self.cell = [], None, None, None

    def handle_starttag(self, tag, attrs):
        if tag == 'table':
            self.table = []
        elif tag == 'tr' and self.table is not None:
            self.row = []
        elif tag in ('th', 'td') and self.row is not None:
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ('th', 'td') and self.cell is not None:
            self.row.append(' '.join(''.join(self.cell).split()))
            self.cell = None
        elif tag == 'tr' and self.row is not None:
            self.table.append(self.row)
            self.row = None
        elif tag == 'table' and self.table is not None:
            self.tables.append(self.table)
            self.table = None


def parse_openai_prices(html):
    parser = _PriceTables()
    parser.feed(html)
    prices = {}
    for table in parser.tables:
        headers = next((row for row in table if row and row[0].lower() == 'model'), [])
        lower = [cell.lower() for cell in headers]
        if not all(key in lower for key in ('model', 'input', 'output')):
            continue
        if 'modality' in lower or 'use case' in lower:
            continue
        inp, out = lower.index('input'), lower.index('output')
        for row in table:
            if len(row) <= max(inp, out) or not row[0].startswith(('gpt-', 'o1', 'o3', 'o4')):
                continue
            try:
                values = [float(row[i].removeprefix('$').replace(',', '')) for i in (inp, out)]
            except ValueError:
                continue
            if all(math.isfinite(v) and v >= 0 for v in values):
                # Standard pricing precedes discounted batch/flex and priority tables.
                prices.setdefault(row[0], {'input': values[0], 'output': values[1]})
    return prices


def fetch_openai_prices():
    import requests
    response = requests.get(OPENAI_PRICING_URL, timeout=10)
    response.raise_for_status()
    prices = parse_openai_prices(response.text)
    if not prices:
        raise ValueError('Official pricing tables were not found')
    return prices


def rank_codex_models(models, prices):
    rows = []
    for model in models:
        name = model.get('model') or model.get('id')
        if not name or model.get('hidden') or 'text' not in model.get('inputModalities', ['text']):
            continue
        price = prices.get(name)
        economical = bool(re.search(r'(?:^|-)(luna|mini|nano)(?:-|$)', name))
        if not price and not economical:
            continue  # Unknown prices must not silently select the expensive default.
        efforts = [item.get('reasoningEffort') for item in model.get('supportedReasoningEfforts', [])]
        effort = next((e for e in ('none', 'minimal', 'low', 'medium', 'high') if e in efforts), None)
        effort = effort or model.get('defaultReasoningEffort') or 'low'
        rows.append({'model': name, 'effort': effort,
                     'reference_cost': price['input'] + price['output'] if price else None,
                     'reference_price': price,
                     'selection_basis': 'official_api_prices' if price else 'economy_family'})
    # Newest economy version breaks equal-price ties; latency is measured after access verification.
    def key(row):
        version = tuple(-int(n) for n in re.findall(r'\d+', row['model']))
        return (row['reference_cost'] is None, row['reference_cost'] or 0, version, row['model'])
    return sorted(rows, key=key)


def codex_catalog(executable, timeout=20):
    """Read paginated model/list over stdio; always reap the temporary server."""
    with temporary_cli_directory(prefix='hh_catalog_') as cwd:
        process = subprocess.Popen(
            [executable, 'app-server'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, cwd=cwd, text=True, encoding='utf-8', errors='replace',
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        messages = queue.Queue()
        def read():
            try:
                for line in process.stdout:
                    try:
                        messages.put(json.loads(line))
                    except ValueError:
                        continue
            finally:
                messages.put(None)
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        deadline = time.monotonic() + timeout
        def send(payload):
            process.stdin.write(json.dumps(payload) + '\n')
            process.stdin.flush()
        def request(identifier, method, params):
            send({'id': identifier, 'method': method, 'params': params})
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Codex model catalog timed out')
                try:
                    message = messages.get(timeout=remaining)
                except queue.Empty:
                    raise TimeoutError('Codex model catalog timed out') from None
                if message is None:
                    raise RuntimeError('Codex model catalog process exited')
                if message.get('id') == identifier:
                    if 'error' in message:
                        raise RuntimeError('Codex model catalog request failed')
                    return message.get('result') or {}
        try:
            request(1, 'initialize', {'clientInfo': {'name': 'hh_model_probe', 'version': '1.0'},
                                      'capabilities': {'experimentalApi': True}})
            send({'method': 'initialized'})
            result, cursor = [], None
            for identifier in range(2, 12):
                params = {'includeHidden': False, 'limit': 100}
                if cursor:
                    params['cursor'] = cursor
                page = request(identifier, 'model/list', params)
                result.extend(page.get('data') or [])
                cursor = page.get('nextCursor')
                if not cursor:
                    return result
            raise RuntimeError('Codex model catalog pagination exceeded limit')
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            reader.join(timeout=2)
            process.stdin.close()
            process.stdout.close()


def cli_problem(detail):
    low = detail.lower()
    if 'weekly limit' in low:
        return 'weekly_quota'
    if 'daily limit' in low:
        return 'daily_quota'
    if any(k in low for k in ('usage limit', 'hit your limit', 'rate limit', 'quota', '429')):
        return 'quota'
    if any(k in low for k in ('not logged in', 'authentication', 'login', 'unauthorized', '401')):
        return 'auth'
    if any(k in low for k in ('model not found', 'model_not_found', 'model_not_supported',
                              'unsupported_model', 'model is not supported',
                              'model is not available', 'invalid model', 'unknown model',
                              'does not exist', 'no access to model', 'not have access to the model')) or re.search(
                                  r'\bmodel\b[^\n]{0,150}\bis not (?:available|supported)\b', low):
        return 'model_unavailable'
    return 'request_failed'

"""Bounded image inference through already configured providers, without profile data."""
import base64
import hashlib
import logging
import math
import os
import re
import time
from urllib.parse import quote, urlparse

import requests

logger = logging.getLogger(__name__)


def image_support(row):
    for source in (row, row.get('capabilities') or {}):
        if not isinstance(source, dict):
            continue
        for field in ('supports_vision', 'vision'):
            if type(source.get(field)) is bool:
                return source[field]
        modalities = source.get('input_modalities')
        if isinstance(modalities, list):
            return 'image' in modalities
    architecture = row.get('architecture') or {}
    if isinstance(architecture, dict) and isinstance(architecture.get('input_modalities'), list):
        return 'image' in architecture['input_modalities']
    return None


def model_price(row):
    pricing = row.get('pricing') or {}
    if not isinstance(pricing, dict):
        return None
    for names, divisor in ((('prompt', 'completion'), 1),
                           (('input_per_million', 'output_per_million'), 1_000_000)):
        try:
            values = [float(pricing[name]) / divisor for name in names]
            if all(math.isfinite(value) and value >= 0 for value in values):
                return sum(values)
        except (KeyError, ValueError, TypeError):
            pass
    return None


class VisionRouter:
    def __init__(self, assistant):
        self.ai = assistant

    @staticmethod
    def fingerprint(base, key, model=''):
        return hashlib.sha256(f'{base}\0{key}\0{model}'.encode()).hexdigest()

    def state(self):
        return self.ai._load_stats().setdefault('_vision', {})

    def remember(self, section, key, value):
        with self.ai._provider_lock('stats'):
            self.state().setdefault(section, {})[key] = value
            self.ai._save_stats()

    @staticmethod
    def local(base):
        return urlparse(base).hostname in ('127.0.0.1', 'localhost', '::1')

    def catalog(self, provider, deadline):
        base, key = provider['base_url'].rstrip('/'), provider.get('api_key') or ''
        fingerprint = self.fingerprint(base, key)
        cached = (self.state().get('catalogs') or {}).get(fingerprint) or {}
        if time.time() - cached.get('at', 0) < 600:
            return cached.get('models', [])
        if deadline - time.monotonic() <= 0:
            return cached.get('models', [])
        try:
            response = requests.get(base + '/models', timeout=min(2, deadline - time.monotonic()),
                                    headers={'Authorization': f'Bearer {key or "none"}'})
            response.raise_for_status()
            rows = response.json().get('data')
            if not isinstance(rows, list):
                raise ValueError('Invalid vision catalog')
            models = []
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get('id'), str):
                    continue
                models.append({'id': row['id'], 'vision': image_support(row),
                               'price': model_price(row), 'free': row.get('free') is True})
            self.remember('catalogs', fingerprint, {'at': time.time(), 'models': models})
            return models
        except Exception as exc:
            logger.info('Vision: каталог %s недоступен (%s); использую известные модели',
                        provider.get('name', 'API'), type(exc).__name__)
            models = cached.get('models') or [{'id': mid, 'vision': None, 'price': None, 'free': False}
                                            for mid in provider.get('models') or [provider.get('model')] if mid]
            self.remember('catalogs', fingerprint, {'at': time.time() - 540, 'models': models})
            return models

    def primary(self, settings):
        from ai_assistant import PROVIDER_BASE_URL
        provider = str(settings.get('provider') or self.ai.provider).lower()
        if provider == 'auto':
            provider = self.ai.provider
        if provider not in ('gemini', 'google', 'openai', 'groq', 'openrouter', 'ollama', 'deepseek'):
            return None
        env = {'gemini': 'GEMINI_API_KEY', 'google': 'GEMINI_API_KEY', 'groq': 'GROQ_API_KEY',
               'openrouter': 'OPENROUTER_API_KEY'}.get(provider, 'OPENAI_API_KEY')
        key = settings.get('api_key') or os.environ.get(env) or (
            self.ai.api_key if provider == self.ai.provider else '')
        model = settings.get('model') or self.ai.model_name
        if not model or (not key and provider != 'ollama'):
            return None
        base = ('https://generativelanguage.googleapis.com/v1beta' if provider in ('gemini', 'google') else
                settings.get('base_url') or (self.ai.base_url if provider == self.ai.provider else '')
                or PROVIDER_BASE_URL.get(provider) or 'https://api.openai.com/v1')
        return {'name': provider, 'kind': provider, 'base': base.rstrip('/'), 'key': key or 'ollama',
                'model': str(model), 'free': False, 'price': None}

    def candidates(self, settings, deadline):
        primary = self.primary(settings)
        explicit = settings.get('provider') != 'auto' and (
            any(settings.get(field) for field in ('model', 'api_key', 'base_url')) or
            settings.get('provider') not in (None, ''))
        if explicit and primary:
            yield primary
        candidates = []
        for raw in self.ai.ai_config.get('openai_compatible') or []:
            if not isinstance(raw, dict) or not raw.get('enabled', True) or not raw.get('base_url'):
                continue
            p = dict(raw)
            p['api_key'] = p.get('api_key') or os.environ.get(p.get('api_key_env') or '', '')
            if not p['api_key'] and not self.local(p['base_url']):
                continue
            base = p['base_url'].rstrip('/')
            service = self.fingerprint(base, p['api_key'])
            if (self.state().get('services') or {}).get(service, 0) > time.time():
                continue
            for row in self.catalog(p, deadline):
                mid = row['id']
                # Missing metadata permits only a small, recognizable vision family, never GPT-OSS/text-only.
                hint = re.search(r'gemini-.*flash|claude-.*haiku|(?:^|[-_/])(?:vision|vl)(?:[-_:]|$)', mid, re.I)
                if row['vision'] is False or (row['vision'] is None and not hint):
                    continue
                if base == 'https://tokenharbor.ai/v1' and not mid.endswith(':free') and mid not in (p.get('models') or []):
                    continue  # Do not silently expand the configured free-only TokenHarbor access.
                candidates.append({'name': p.get('name') or 'API', 'kind': 'openai', 'base': base,
                                   'key': p['api_key'], 'model': mid,
                                   'free': row['free'] or mid.endswith(':free'), 'price': row['price']})
        def rank(candidate):
            prior = (self.state().get('models') or {}).get(self.fingerprint(
                candidate['base'], candidate['key'], candidate['model'])) or {}
            tier = 0 if candidate['free'] else 1 if self.local(candidate['base']) else 2
            price = 0 if tier < 2 else candidate['price'] if candidate['price'] is not None else float('inf')
            return (tier, price, not prior.get('ok'), prior.get('seconds', 20),
                    not any(tag in candidate['model'].lower() for tag in ('flash', 'lite', 'mini')), candidate['model'])
        yield from sorted(candidates, key=rank)
        if not explicit and primary:
            yield primary

    def request(self, candidate, encoded, timeout):
        prompt = 'Прочитай текст на картинке. Верни только символы с картинки, без пояснений.'
        if candidate['kind'] in ('gemini', 'google'):
            response = requests.post(candidate['base'] + '/models/' + quote(candidate['model'], safe='') + ':generateContent',
                headers={'x-goog-api-key': candidate['key']}, timeout=timeout,
                json={'contents': [{'parts': [{'text': prompt}, {'inline_data': {
                    'mime_type': 'image/png', 'data': encoded}}]}]})
        else:
            response = requests.post(candidate['base'] + '/chat/completions',
                headers={'Authorization': f'Bearer {candidate["key"] or "none"}'}, timeout=timeout,
                json={'model': candidate['model'], 'messages': [{'role': 'user', 'content': [
                    {'type': 'text', 'text': prompt}, {'type': 'image_url', 'image_url': {
                        'url': 'data:image/png;base64,' + encoded}}]}], 'max_completion_tokens': 64})
        response.raise_for_status()
        payload = response.json()
        if candidate['kind'] in ('gemini', 'google'):
            return ''.join(part.get('text', '') for part in payload['candidates'][0]['content']['parts'])
        return payload['choices'][0]['message']['content']

    def recognize(self, image_data):
        settings = self.ai.config.get('captcha') or {}
        self.ai.last_captcha_source = None
        if not settings.get('enabled') or not image_data or len(image_data) > 5_000_000:
            return None
        try:
            per_attempt = min(60, max(1, float(settings.get('timeout_seconds', 20))))
            total = min(60, max(1, float(settings.get('total_timeout_seconds', 45))))
        except (ValueError, TypeError):
            per_attempt, total = 20, 45
        deadline = time.monotonic() + total
        encoded = base64.b64encode(image_data).decode('ascii')
        seen = set()
        for candidate in self.candidates(settings, deadline):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning('Vision: бюджет запроса исчерпан; требуется ручная проверка')
                break
            service = self.fingerprint(candidate['base'], candidate['key'])
            fingerprint = self.fingerprint(candidate['base'], candidate['key'], candidate['model'])
            prior = (self.state().get('models') or {}).get(fingerprint) or {}
            if fingerprint in seen or prior.get('until', 0) > time.time() or (
                    self.state().get('services') or {}).get(service, 0) > time.time():
                continue
            seen.add(fingerprint)
            started = time.monotonic()
            logger.info('Vision: запрос %s, модель %s', candidate['name'], candidate['model'])
            try:
                answer = self.request(candidate, encoded, min(per_attempt, remaining))
                if not isinstance(answer, str) or not re.fullmatch(r'[\w -]{1,32}', answer.strip(), re.UNICODE):
                    raise ValueError('Invalid captcha text')
                elapsed = round(time.monotonic() - started, 2)
                self.remember('models', fingerprint, {'ok': True, 'seconds': elapsed, 'until': 0})
                self.ai.last_captcha_source = {'provider': candidate['name'], 'model': candidate['model'],
                                               'seconds': elapsed, 'fingerprint': fingerprint}
                logger.info('Vision: ответ получен от %s, модель %s, %.1f с', candidate['name'], candidate['model'], elapsed)
                return answer.strip()
            except Exception as exc:
                response = getattr(exc, 'response', None)
                status = getattr(response, 'status_code', None)
                reason = ''
                try:
                    details = response.json().get('error', {}).get('details', []) if response is not None else []
                    reason = next((d.get('reason') for d in details if isinstance(d, dict) and d.get('reason')
                                   in ('API_KEY_INVALID', 'API_KEY_EXPIRED', 'API_KEY_SERVICE_BLOCKED')), '')
                except (ValueError, TypeError, AttributeError):
                    pass
                if status in (401, 403, 429, 500, 502, 503, 504) or reason or isinstance(
                        exc, (requests.ConnectionError, requests.Timeout)):
                    self.remember('services', service, time.time() + (600 if reason or status in (401, 403) else 60))
                self.remember('models', fingerprint, {'ok': False, 'seconds': round(time.monotonic() - started, 2),
                                                      'until': time.time() + (600 if status in (400, 404) else 60)})
                logger.warning('Vision: %s — %s (%s, HTTP %s%s); пробую другой доступный ИИ',
                               candidate['name'], 'неподходящий формат ответа' if isinstance(exc, ValueError) else 'ошибка запроса',
                               type(exc).__name__, status if isinstance(status, int) else 'нет',
                               ', ' + reason if reason else '')
        return None

    def feedback(self, outcome):
        source = getattr(self.ai, 'last_captcha_source', None)
        if outcome not in ('accepted', 'server_rejected') or not isinstance(source, dict):
            return
        fingerprint = source.get('fingerprint')
        if not isinstance(fingerprint, str) or not re.fullmatch('[0-9a-f]{64}', fingerprint):
            return
        prior = (self.state().get('models') or {}).get(fingerprint) or {}
        result = dict(prior, site_outcome=outcome)
        if outcome == 'server_rejected':
            result.update(ok=False, until=time.time() + 60)
            logger.info('Vision: HH отклонил ответ %s; следующая попытка использует другую модель', source.get('model'))
        self.remember('models', fingerprint, result)


def check_vision_provider(config):
    """Live API test using fresh local control images, never an HH application."""
    import secrets
    from pathlib import Path
    from selenium import webdriver
    from ai_assistant import AIAssistant
    from cli_models import temporary_cli_directory

    ai = AIAssistant(config)
    if not (config.get('captcha') or {}).get('enabled'):
        print('[!] Vision выключен. Включите распознавание в N → F.')
        return False
    options = webdriver.ChromeOptions()
    options.add_argument('--headless=new')
    options.add_argument('--disable-background-networking')
    options.add_argument('--window-size=640,320')
    options.add_argument('--log-level=3')
    options.add_experimental_option('excludeSwitches', ['enable-logging'])
    if os.environ.get('CHROME_BINARY'):
        options.binary_location = os.environ['CHROME_BINARY']
    with temporary_cli_directory('hh_vision_control_') as directory:
        options.add_argument('--user-data-dir=' + str(Path(directory) / 'chrome'))
        from app_paths import DATA_DIR
        from terminal_ui import chrome_service
        driver = webdriver.Chrome(service=chrome_service(DATA_DIR), options=options)
        try:
            print('Проверка реального Vision API. Картинки контрольные, не капча HH; отклики не отправляются.')
            for index in range(2):
                expected = ''.join(secrets.choice('ABCDEFGHJKMNPQRSTUVWXYZ23456789') for _ in range(5))
                html = ('<div id="vision-control" style="width:220px;height:80px;background:white;'
                        'color:black;font:42px monospace;padding:10px">' + expected + '</div>')
                driver.get('data:text/html;charset=utf-8,' + quote(html))
                crop = driver.find_element('id', 'vision-control').screenshot_as_png
                answer = ai.recognize_captcha(crop)
                if answer != expected:
                    print(f'[!] Контрольная картинка {index + 1}/2 не распознана точно. Успех не подтверждён.')
                    return False
                source = ai.last_captcha_source
                print(f'[OK] Контрольная картинка {index + 1}/2: {source["provider"]}, '
                      f'{source["model"]}, {source["seconds"]:.1f} с')
            print('[OK] Реальный Vision API распознал обе контрольные картинки. Принятие капчи HH этим не проверяется.')
            return True
        finally:
            driver.quit()

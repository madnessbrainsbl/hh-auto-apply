"""OpenCode 2 text inference with isolated settings and explicit Free/Go access."""
import json
import os
from pathlib import Path
import re
import sqlite3
from contextlib import closing

import requests

from cli_models import cli_problem, run_cli_process, temporary_cli_directory


def settings(ai_config):
    row = ai_config.get('opencode') or {}
    if not isinstance(row, dict):
        raise ValueError('OpenCode: настройки должны быть объектом')
    mode, model = row.get('mode', 'free'), row.get('model', 'auto')
    if mode not in ('free', 'go', 'auto'):
        raise ValueError('OpenCode: выберите Free, Go или Free → Go')
    if not isinstance(model, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,159}', model):
        raise ValueError('OpenCode: неверное имя модели')
    if mode == 'auto' and model != 'auto':
        raise ValueError('OpenCode Free → Go требует автоматического выбора модели')
    return mode, model


def catalog(mode='free', timeout=8):
    settings({'opencode': {'mode': mode}})
    if mode == 'auto':
        raise ValueError('Каталоги Free и Go читаются отдельно')
    suffix = '/go' if mode == 'go' else ''
    response = requests.get('https://opencode.ai/inference' + suffix + '/v1/models',
                            headers={'Accept': 'application/json'}, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    rows = payload.get('data') if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError('OpenCode: неверный каталог моделей')
    result = []
    for row in rows:
        model = row.get('id') if isinstance(row, dict) else None
        if not isinstance(model, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,159}', model):
            continue
        if model.startswith('jev-') or model in result:
            continue  # Jev produces typed decisions rather than conversational answers.
        if mode == 'free' and not (model.endswith('-free') or model == 'big-pickle'):
            continue
        result.append(model)
    if not result:
        raise ValueError('OpenCode: доступных текстовых моделей нет')
    return result


def retryable(model, problem):
    return (problem in ('model_unavailable', 'timeout', 'invalid_probe', 'request_failed', 'global_regions')
            or (model.startswith('opencode/') and problem in ('quota', 'daily_quota', 'weekly_quota')))


def go_key():
    data = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share') / 'opencode'
    database = data / 'opencode.db'
    if database.is_file():
        try:
            with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
                row = db.execute('SELECT value FROM credential WHERE integration_id=? AND active=1 '
                                 'ORDER BY time_updated DESC LIMIT 1', ('opencode-go',)).fetchone()
                key = json.loads(row[0]).get('key') if row else None
                if isinstance(key, str) and key.strip():
                    return key.strip()
        except (sqlite3.Error, ValueError, TypeError, AttributeError):
            pass
    try:
        key = json.loads((data / 'auth.json').read_text(encoding='utf-8')).get('opencode-go', {}).get('key')
        if isinstance(key, str) and key.strip():
            return key.strip()
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    raise ValueError('OpenCode Go: сначала подключите подписку в самом OpenCode')


def request(executable, model, prompt, system_prompt, timeout):
    provider, separator, identifier = model.partition('/')
    mode = {'opencode': 'free', 'opencode-go': 'go'}.get(provider)
    if not separator or mode is None:
        raise ValueError('OpenCode: неверный провайдер модели')
    settings({'opencode': {'mode': mode, 'model': identifier}})
    if mode == 'free' and not (identifier.endswith('-free') or identifier == 'big-pickle'):
        raise ValueError('OpenCode Free: платная модель запрещена')
    key = go_key() if mode == 'go' else ''
    with temporary_cli_directory('hh_opencode_') as directory:
        root = Path(directory)
        # Free requires the stock agent; no --auto and EOF on stdin grant no tools.
        config = {'update': 'disable', 'snapshots': False,
                  'permissions': [{'action': '*', 'resource': '*', 'effect': 'ask'}],
                  'policies': [{'action': 'provider.use', 'resource': '*', 'effect': 'deny'},
                               {'action': 'provider.use', 'resource': provider, 'effect': 'allow'}]}
        if key:
            config['providers'] = {provider: {'settings': {'apiKey': '{env:HH_OPENCODE_GO_KEY}'}}}
        (root / 'opencode.json').write_text(json.dumps(config), encoding='utf-8')
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(('OPENCODE_', 'XDG_', 'OPENAI_', 'ANTHROPIC_', 'GOOGLE_', 'GEMINI_'))
               and not k.endswith(('_API_KEY', '_API_TOKEN')) and k != 'HH_OPENCODE_GO_KEY'}
        for name, folder in (('XDG_CONFIG_HOME', 'config'), ('XDG_DATA_HOME', 'data'),
                             ('XDG_STATE_HOME', 'state'), ('XDG_CACHE_HOME', 'cache')):
            env[name] = str(root / folder)
        env['OPENCODE_DISABLE_CLAUDE_CODE'] = 'true'
        if key:
            env['HH_OPENCODE_GO_KEY'] = key
        command = [executable, 'run', '--standalone', '--model', model,
                   '--agent', 'build', '--format', 'json']
        text = ('Answer directly without tools, files, commands or permission requests.\n\n'
                f'{system_prompt}\n\n{prompt}')
        process = run_cli_process(command, input=text.encode('utf-8'), cwd=directory,
                                  timeout=timeout, env=env)
        parts, problem = {}, ''
        for line in process.stdout.decode('utf-8', errors='replace').splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            part = event.get('part', {})
            if not isinstance(part, dict):
                problem = 'request_failed'
                continue
            if event.get('type') == 'error':
                detail = json.dumps(event.get('error') or {})
                problem = ('global_regions' if mode == 'go' and 'requires global regions' in detail.lower()
                           else cli_problem(detail))
            elif any(word in str(event.get('type', '')) for word in ('tool', 'permission')) or part.get('type') == 'tool':
                problem = 'tool_requested'
            elif event.get('type') == 'text' and isinstance(part.get('text'), str):
                parts[part.get('id') or 'answer'] = part['text']
        if process.returncode and not problem:
            problem = cli_problem(process.stderr.decode('utf-8', errors='replace'))
        answer = '\n'.join(parts.values()).strip()
        return {'ok': bool(answer) and not problem, 'text': answer if not problem else '',
                'model': model, 'problem': problem or ('' if answer else 'request_failed')}

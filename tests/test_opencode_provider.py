import copy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from unittest.mock import Mock

import pytest

from ai_assistant import AIAssistant
import config_manager
import opencode_provider as provider

REAL_FIND_CLI = AIAssistant.find_cli


def assistant(monkeypatch, mode='free', model='auto'):
    ai = AIAssistant.__new__(AIAssistant)
    ai.ai_config = {'cli_providers': ['opencode'], 'auto_explore': 0,
                    'opencode': {'mode': mode, 'model': model}}
    ai._ai_stats = {}
    ai.find_cli = lambda name: 'synthetic-opencode' if name == 'opencode' else None
    monkeypatch.setattr(ai, '_save_stats', Mock())
    return ai


def events(*rows, code=0):
    return subprocess.CompletedProcess([], code,
        '\n'.join(json.dumps(row) for row in rows).encode(), b'')


def test_catalog_keeps_free_text_models_and_rejects_invalid_ids(monkeypatch):
    response = Mock()
    response.json.return_value = {'data': [{'id': name} for name in (
        'big-pickle', 'new-free', 'jev-1-free', 'paid-model', 'new-free', '../free', '-bad/free')]}
    get = Mock(return_value=response)
    monkeypatch.setattr(provider.requests, 'get', get)
    assert provider.catalog() == ['big-pickle', 'new-free']
    assert get.call_args.args == ('https://opencode.ai/inference/v1/models',)
    assert 'Authorization' not in get.call_args.kwargs['headers']
    assert provider.catalog('go') == ['big-pickle', 'new-free', 'paid-model']
    assert '/inference/go/' in get.call_args.args[0]
    response.json.return_value = {'data': 'invalid'}
    with pytest.raises(ValueError):
        provider.catalog()
    response.json.return_value = {'data': []}
    with pytest.raises(ValueError):
        provider.catalog()


@pytest.mark.parametrize('options', [
    {'mode': 'paid'}, {'model': '../secret'}, {'model': 'opencode/paid'},
    {'model': None}, {'model': '--help'}, {'mode': 'auto', 'model': 'one-free'}, 'bad',
])
def test_invalid_settings_are_rejected(options):
    with pytest.raises(ValueError):
        provider.settings({'opencode': options})


def test_free_request_is_isolated_and_does_not_read_paid_credentials(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'private-openai')
    monkeypatch.setenv('OPENCODE_CONFIG', 'private-settings')
    monkeypatch.setenv('XDG_DATA_HOME', 'private-data')
    monkeypatch.setenv('HH_OPENCODE_GO_KEY', 'private-paid-key')
    key = Mock(side_effect=AssertionError('Free must not read Go credentials'))
    monkeypatch.setattr(provider, 'go_key', key)
    directories = []

    def run(command, **kwargs):
        directory = Path(kwargs['cwd'])
        directories.append(directory)
        config = json.loads((directory / 'opencode.json').read_text())
        assert command == ['synthetic', 'run', '--standalone', '--model', 'opencode/new-free',
                           '--agent', 'build', '--format', 'json']
        assert config['permissions'] == [{'action': '*', 'resource': '*', 'effect': 'ask'}]
        assert '--auto' not in command
        assert {'action': 'provider.use', 'resource': 'opencode', 'effect': 'allow'} in config['policies']
        assert 'providers' not in config
        env = kwargs['env']
        for secret in ('OPENAI_API_KEY', 'OPENCODE_CONFIG', 'HH_OPENCODE_GO_KEY'):
            assert secret not in env
        assert Path(env['XDG_DATA_HOME']).parent == directory
        assert 'Русский запрос'.encode() in kwargs['input']
        return events({'type': 'text', 'part': {'id': '1', 'text': 'го'}},
                      {'type': 'text', 'part': {'id': '1', 'text': 'готов'}})

    monkeypatch.setattr(provider, 'run_cli_process', run)
    result = provider.request('synthetic', 'opencode/new-free', 'Русский запрос', 'system', 3)
    assert result['ok'] and result['text'] == 'готов'
    assert not directories[0].exists()
    assert os.environ['HH_OPENCODE_GO_KEY'] == 'private-paid-key'
    key.assert_not_called()


@pytest.mark.parametrize('failure,problem', [
    ({'type': 'error', 'error': {'message': '429 quota exceeded'}}, 'quota'),
    ({'type': 'tool_use', 'part': {'type': 'tool'}}, 'tool_requested'),
    ({'type': 'text', 'part': []}, 'request_failed'),
])
def test_partial_text_is_discarded_on_error_or_tool_request(monkeypatch, failure, problem):
    monkeypatch.setattr(provider, 'run_cli_process', lambda *args, **kwargs: events(
        {'type': 'text', 'part': {'id': '1', 'text': 'partial'}}, failure))
    result = provider.request('synthetic', 'opencode/new-free', 'test', 'system', 3)
    assert not result['ok'] and result['text'] == '' and result['problem'] == problem


def test_go_global_regions_error_is_distinct_and_discards_partial_text(monkeypatch):
    monkeypatch.setattr(provider, 'go_key', lambda: 'synthetic-go-key')
    monkeypatch.setattr(provider, 'run_cli_process', lambda *args, **kwargs: events(
        {'type': 'text', 'part': {'text': 'partial'}},
        {'type': 'error', 'error': {'status': 400, 'type': 'provider.invalid-request',
            'message': 'This Go model requires Global regions. Select Global in your workspace Privacy settings.'}},
        code=1))
    result = provider.request('synthetic', 'opencode-go/model', 'test', '', 3)
    assert result == {'ok': False, 'text': '', 'model': 'opencode-go/model', 'problem': 'global_regions'}


def test_nonzero_exit_cannot_confirm_text(monkeypatch):
    monkeypatch.setattr(provider, 'run_cli_process', lambda *args, **kwargs: events(
        {'type': 'text', 'part': {'text': 'partial'}}, code=1))
    assert not provider.request('synthetic', 'opencode/new-free', 'test', 'system', 3)['ok']


def test_paid_model_is_rejected_in_free_provider(monkeypatch):
    run = Mock()
    monkeypatch.setattr(provider, 'run_cli_process', run)
    with pytest.raises(ValueError):
        provider.request('synthetic', 'opencode/paid-model', 'test', '', 3)
    run.assert_not_called()


def test_go_uses_only_explicit_key_in_child_environment(monkeypatch):
    monkeypatch.setattr(provider, 'go_key', lambda: 'synthetic-go-key')
    def run(command, **kwargs):
        config = (Path(kwargs['cwd']) / 'opencode.json').read_text()
        assert 'synthetic-go-key' not in config
        assert kwargs['env']['HH_OPENCODE_GO_KEY'] == 'synthetic-go-key'
        assert command[command.index('--model') + 1] == 'opencode-go/model'
        return events({'type': 'text', 'part': {'text': 'готов'}})
    monkeypatch.setattr(provider, 'run_cli_process', run)
    assert provider.request('synthetic', 'opencode-go/model', 'test', '', 3)['ok']


def test_go_credentials_are_read_without_changing_database(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path))
    data = tmp_path / 'opencode'
    data.mkdir()
    path = data / 'opencode.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE credential (integration_id TEXT, active INT, time_updated INT, value TEXT)')
        db.executemany('INSERT INTO credential VALUES (?, ?, ?, ?)', [
            ('other', 1, 3, '{"key":"unrelated-key"}'),
            ('opencode-go', 0, 2, '{"key":"inactive"}'),
            ('opencode-go', 1, 1, '{"key":"synthetic-go-key"}')])
    db.close()
    before = path.read_bytes()
    assert provider.go_key() == 'synthetic-go-key'
    assert path.read_bytes() == before
    path.unlink()
    (data / 'auth.json').write_text('{"opencode-go":{"key":"legacy-key"}}')
    assert provider.go_key() == 'legacy-key'


def test_windows_npm_binary_is_discovered_without_path(tmp_path, monkeypatch):
    binary = tmp_path / 'npm/node_modules/@opencode/cli/bin/opencode.exe'
    binary.parent.mkdir(parents=True)
    binary.touch()
    monkeypatch.setenv('APPDATA', str(tmp_path))
    monkeypatch.setattr('shutil.which', lambda name: None)
    assert REAL_FIND_CLI('opencode') == str(binary)


def test_discovery_caches_catalog_and_settings_change_invalidates_probe(monkeypatch):
    ai = assistant(monkeypatch)
    catalog = Mock(return_value=['one-free', 'two-free'])
    monkeypatch.setattr(provider, 'catalog', catalog)
    assert len(ai._discover_cli_models('opencode')) == 2
    assert len(ai._discover_cli_models('opencode')) == 2
    assert catalog.call_count == 1
    before = ai._cli_fingerprint('opencode')
    ai.ai_config['opencode']['model'] = 'two-free'
    assert ai._cli_fingerprint('opencode') != before
    assert ai._discover_cli_models('opencode') == [
        {'model': 'opencode/two-free', 'selection_basis': 'opencode_free'}]


def test_free_model_quota_tries_next_free_model_and_pins_actual_answer(monkeypatch):
    ai = assistant(monkeypatch)
    monkeypatch.setattr(provider, 'catalog', lambda *args, **kwargs: ['one-free', 'two-free'])
    run = Mock(side_effect=[{'ok': False, 'problem': 'quota'},
                           {'ok': True, 'text': 'готов', 'model': 'opencode/two-free'}])
    monkeypatch.setattr(provider, 'request', run)
    selected = ai._ensure_cli_model('opencode')
    assert selected['model'] == 'opencode/two-free'
    assert run.call_count == 2
    ai._run_cli_request = Mock(return_value={'ok': True, 'text': 'Ответ', 'seconds': .1,
                                            'model': 'opencode/two-free'})
    assert ai._call_step('opencode', 'test', None) == 'Ответ'
    assert ai._using_cli == 'opencode'


def test_go_quota_does_not_try_other_paid_models(monkeypatch):
    ai = assistant(monkeypatch, mode='go')
    monkeypatch.setattr(provider, 'catalog', lambda *args, **kwargs: ['one', 'two'])
    run = Mock(return_value={'ok': False, 'problem': 'quota'})
    monkeypatch.setattr(provider, 'request', run)
    assert ai._ensure_cli_model('opencode') is None
    assert run.call_count == 1


def test_combined_catalog_reads_go_only_after_free_and_caches_accesses_separately(monkeypatch):
    ai = assistant(monkeypatch, mode='auto')
    catalog = Mock(side_effect=lambda mode, **kwargs: ['one-free'] if mode == 'free' else ['working'])
    monkeypatch.setattr(provider, 'catalog', catalog)
    rows = iter(ai._discover_cli_models('opencode'))
    assert next(rows)['model'] == 'opencode/one-free'
    assert [call.args[0] for call in catalog.call_args_list] == ['free']
    assert next(rows)['model'] == 'opencode-go/working'
    assert [call.args[0] for call in catalog.call_args_list] == ['free', 'go']
    assert len(list(ai._discover_cli_models('opencode'))) == 2
    assert catalog.call_count == 2
    assert set(ai._load_stats()['_opencode_catalogs']) == {'free', 'go'}


@pytest.mark.parametrize('stage', ['probe', 'generation'])
def test_combined_access_exhausts_all_free_before_selecting_usable_go(monkeypatch, stage):
    ai = assistant(monkeypatch, mode='auto')
    free = ['one-free', 'two-free', 'three-free', 'four-free']
    monkeypatch.setattr(provider, 'catalog', lambda mode, **kwargs: free if mode == 'free' else ['blocked', 'working'])
    first = next(iter(ai._discover_cli_models('opencode')))
    if stage == 'generation':
        ai._remember_cli_model('opencode', first, {'ok': True, 'model': first['model'], 'seconds': .1})
    calls = []
    def run(name, candidate, *args, **kwargs):
        model = candidate['model']
        calls.append(model)
        good = model == 'opencode-go/working'
        return {'ok': good, 'text': 'готов' if good else '', 'model': model, 'seconds': .1,
                'problem': '' if good else ('quota' if model.startswith('opencode/') else 'global_regions')}
    monkeypatch.setattr(ai, '_run_cli_request', run)
    if stage == 'probe':
        assert ai._ensure_cli_model('opencode')['model'] == 'opencode-go/working'
    else:
        assert ai._call_cli_provider('opencode', 'Вопрос', None) == 'готов'
    assert calls == ['opencode/' + model for model in free] + ['opencode-go/blocked', 'opencode-go/working']
    assert ai.ai_config['opencode'] == {'mode': 'auto', 'model': 'auto'}


@pytest.mark.parametrize('stage', ['probe', 'generation'])
def test_combined_access_never_reads_go_catalog_when_free_answers(monkeypatch, stage):
    ai = assistant(monkeypatch, mode='auto')
    def catalog(mode, **kwargs):
        assert mode == 'free', 'Go must not be read while Free answers'
        return ['one-free', 'two-free']
    monkeypatch.setattr(provider, 'catalog', catalog)
    run = Mock(return_value={'ok': True, 'text': 'готов', 'model': 'opencode/one-free', 'seconds': .1})
    monkeypatch.setattr(ai, '_run_cli_request', run)
    if stage == 'probe':
        assert ai._ensure_cli_model('opencode')['model'] == 'opencode/one-free'
    else:
        assert ai._call_cli_provider('opencode', 'Вопрос', None) == 'готов'
    assert all(call.args[1]['model'].startswith('opencode/') for call in run.call_args_list)
    assert 'go' not in ai._load_stats().get('_opencode_catalogs', {})


def test_cached_go_is_reused_only_after_free_check_and_returns_to_recovered_free(monkeypatch):
    ai = assistant(monkeypatch, mode='auto')
    monkeypatch.setattr(provider, 'catalog', lambda mode, **kwargs: ['one-free'] if mode == 'free' else ['working'])
    free_ready = [False]
    calls = []
    def run(name, candidate, *args, **kwargs):
        model = candidate['model']
        calls.append(model)
        good = model.startswith('opencode-go/') or free_ready[0]
        return {'ok': good, 'text': 'готов' if good else '', 'model': model,
                'problem': '' if good else 'quota', 'seconds': .1}
    monkeypatch.setattr(ai, '_run_cli_request', run)
    assert ai._call_cli_provider('opencode', 'Вопрос', None) == 'готов'
    assert ai._load_stats()['_cli_models']['opencode']['model'] == 'opencode-go/working'
    count = len(calls)
    assert ai._call_cli_provider('opencode', 'Вопрос', None) == 'готов'
    assert calls[count:] == ['opencode-go/working']
    ai._opencode_model_rest.clear()
    free_ready[0] = True
    count = len(calls)
    assert ai._call_cli_provider('opencode', 'Вопрос', None) == 'готов'
    assert calls[count:] == ['opencode/one-free', 'opencode/one-free']
    assert ai._load_stats()['_cli_models']['opencode']['model'] == 'opencode/one-free'


def test_combined_access_does_not_switch_to_go_after_task_deadline(monkeypatch):
    from ai_assistant import _AI_DEADLINE
    ai = assistant(monkeypatch, mode='auto')
    catalog = Mock(side_effect=lambda mode, **kwargs: ['one-free', 'two-free'] if mode == 'free' else ['working'])
    monkeypatch.setattr(provider, 'catalog', catalog)
    clock = [100.0]
    monkeypatch.setattr('ai_assistant.time.monotonic', lambda: clock[0])
    def run(*args, **kwargs):
        clock[0] += 2
        return {'ok': False, 'text': '', 'problem': 'timeout', 'seconds': 2}
    monkeypatch.setattr(ai, '_run_cli_request', run)
    token = _AI_DEADLINE.set(101.0)
    try:
        with pytest.raises(TimeoutError):
            ai._call_cli_provider('opencode', 'Вопрос', None)
    finally:
        _AI_DEADLINE.reset(token)
    assert [call.args[0] for call in catalog.call_args_list] == ['free']


def test_go_catalog_error_after_free_exhaustion_is_handled(monkeypatch):
    ai = assistant(monkeypatch, mode='auto')
    def catalog(mode, **kwargs):
        if mode == 'go':
            raise OSError('synthetic network outage')
        return ['one-free']
    monkeypatch.setattr(provider, 'catalog', catalog)
    monkeypatch.setattr(ai, '_run_cli_request', Mock(return_value={
        'ok': False, 'text': '', 'problem': 'quota', 'seconds': .1}))
    assert ai._call_cli_provider('opencode', 'Вопрос', None) is None


def test_combined_access_stops_on_go_account_quota(monkeypatch):
    ai = assistant(monkeypatch, mode='auto')
    monkeypatch.setattr(provider, 'catalog', lambda mode, **kwargs: ['one-free'] if mode == 'free' else ['one', 'two'])
    run = Mock(return_value={'ok': False, 'text': '', 'problem': 'quota', 'seconds': .1})
    monkeypatch.setattr(ai, '_run_cli_request', run)
    assert ai._ensure_cli_model('opencode') is None
    assert [call.args[1]['model'] for call in run.call_args_list] == ['opencode/one-free', 'opencode-go/one']


@pytest.mark.parametrize('problem', ['quota', 'daily_quota', 'weekly_quota', 'timeout',
                                     'model_unavailable', 'request_failed'])
def test_generation_failure_rotates_free_models_without_disabling_provider(monkeypatch, problem):
    ai = assistant(monkeypatch)
    monkeypatch.setattr(provider, 'catalog', lambda *args, **kwargs: ['one-free', 'two-free'])
    first = ai._discover_cli_models('opencode')[0]
    ai._remember_cli_model('opencode', first, {'ok': True, 'model': first['model'], 'seconds': .1})
    run = Mock(side_effect=[{'ok': False, 'text': '', 'problem': problem, 'seconds': .2},
                           {'ok': True, 'text': 'Ответ', 'model': 'opencode/two-free', 'seconds': .3}])
    monkeypatch.setattr(ai, '_run_cli_request', run)
    assert ai._call_cli_providers('Вопрос', 'system') == 'Ответ'
    assert [call.args[1]['model'] for call in run.call_args_list] == [
        'opencode/one-free', 'opencode/two-free']
    assert all(call.args[2:] == ('Вопрос', 'system') for call in run.call_args_list)
    assert 'opencode' not in ai._cli_dead
    assert 'opencode' not in ai._load_stats().get('_down', [])
    assert ai._ensure_cli_model('opencode')['model'] == 'opencode/two-free'
    assert [row['model'] for row in ai._discover_cli_models('opencode')] == ['opencode/two-free']
    monkeypatch.setattr('ai_assistant.time.time', lambda: first.get('checked_at', 0) + 10**12)
    assert len(ai._discover_cli_models('opencode')) == 2


def test_free_generation_exhausts_entire_catalog_without_repeating_models(monkeypatch):
    ai = assistant(monkeypatch)
    monkeypatch.setattr(provider, 'catalog', lambda *args, **kwargs: [
        'one-free', 'two-free', 'three-free', 'four-free'])
    first = ai._discover_cli_models('opencode')[0]
    ai._remember_cli_model('opencode', first, {'ok': True, 'model': first['model'], 'seconds': .1})
    run = Mock(return_value={'ok': False, 'text': '', 'problem': 'quota', 'seconds': .2})
    monkeypatch.setattr(ai, '_run_cli_request', run)
    assert ai._call_cli_provider('opencode', 'Вопрос', None) is None
    assert [call.args[1]['model'] for call in run.call_args_list] == [
        'opencode/one-free', 'opencode/two-free', 'opencode/three-free', 'opencode/four-free']


def test_startup_probe_can_reach_working_model_beyond_three(monkeypatch):
    ai = assistant(monkeypatch)
    monkeypatch.setattr(provider, 'catalog', lambda *args, **kwargs: [
        'one-free', 'two-free', 'three-free', 'four-free', 'five-free'])
    first = ai._discover_cli_models('opencode')[0]
    clock = [100.0]
    monkeypatch.setattr('ai_assistant.time.monotonic', lambda: clock[0])
    calls = []
    def run(_name, candidate, prompt, system_prompt, timeout=None):
        calls.append(candidate['model'])
        clock[0] += 19
        good = candidate['model'] == 'opencode/four-free'
        return {'ok': good, 'text': 'готов' if good else '', 'model': candidate['model'],
                'problem': '' if good else 'timeout', 'seconds': 19}
    monkeypatch.setattr(ai, '_run_cli_request', run)
    assert ai._ensure_cli_model('opencode')['model'] == 'opencode/four-free'
    assert calls == ['opencode/one-free', 'opencode/two-free', 'opencode/three-free', 'opencode/four-free']


@pytest.mark.parametrize('deadline', [None, float('inf')])
def test_opencode_catalog_budget_yields_to_other_provider(monkeypatch, deadline, caplog):
    ai = assistant(monkeypatch)
    ai.enabled = True
    ai.ai_order = lambda: ['opencode', 'compat:other']
    monkeypatch.setattr(provider, 'catalog', lambda *args, **kwargs:
                        ['one-free', 'two-free', 'three-free', 'four-free'])
    clock = [100.0]
    monkeypatch.setattr('ai_assistant.time.monotonic', lambda: clock[0])
    calls = []

    def run(_name, candidate, *args, **kwargs):
        calls.append(candidate['model'])
        timeout = ai._request_timeout(20)
        clock[0] += timeout
        return {'ok': False, 'text': '', 'model': candidate['model'],
                'problem': 'timeout', 'seconds': timeout}

    monkeypatch.setattr(ai, '_run_cli_request', run)
    def other(*args, **kwargs):
        calls.append('other')
        assert ai._request_timeout(45) == 30
        return 'готов'
    monkeypatch.setattr(ai, '_call_compat_providers', other)
    with caplog.at_level('INFO'):
        assert ai._call_llm('Вопрос', deadline=deadline) == 'готов'
    assert calls == ['opencode/one-free', 'opencode/two-free', 'other']
    assert clock[0] == 130
    assert 'следующий ИИ' in caplog.text
    assert 'opencode' not in getattr(ai, '_cli_dead', set())
    assert [row['model'] for row in ai._discover_cli_models('opencode')] == [
        'opencode/three-free', 'opencode/four-free']


def test_opencode_timeout_stops_owned_process_before_fallback(monkeypatch, tmp_path):
    import cli_models
    ai = assistant(monkeypatch)
    ai.enabled = True
    ai.ai_order = lambda: ['opencode', 'compat:other']
    monkeypatch.setattr(ai, '_attempt_budget', lambda _: .2)
    monkeypatch.setattr(provider, 'catalog', lambda *args, **kwargs: ['one-free'])
    processes = []
    popen = subprocess.Popen

    def tracked_process(*args, **kwargs):
        child = popen(*args, **kwargs)
        processes.append(child)
        return child

    monkeypatch.setattr(cli_models.subprocess, 'Popen', tracked_process)
    def request(_exe, model, prompt, system_prompt, timeout):
        cli_models.run_cli_process([sys.executable, '-c', 'import time; time.sleep(60)'],
                                  input=b'', cwd=str(tmp_path), timeout=timeout)
        pytest.fail('Sleeping process must time out')
    monkeypatch.setattr(provider, 'request', request)
    def other(*args, **kwargs):
        assert processes and all(child.poll() is not None for child in processes)
        return 'готов'
    monkeypatch.setattr(ai, '_call_compat_providers', other)
    assert ai._call_llm('Вопрос') == 'готов'


@pytest.mark.parametrize('stage', ['probe', 'generation'])
def test_go_auto_skips_global_models_and_caches_working_selection(monkeypatch, stage):
    ai = assistant(monkeypatch, mode='go')
    monkeypatch.setattr(provider, 'catalog', lambda *args, **kwargs: ['blocked1', 'blocked2', 'blocked3', 'working'])
    first = ai._discover_cli_models('opencode')[0]
    if stage == 'generation':
        ai._remember_cli_model('opencode', first, {'ok': True, 'model': first['model'], 'seconds': .1})
    calls = []
    def run(name, candidate, *args, **kwargs):
        calls.append(candidate['model'])
        good = candidate['model'] == 'opencode-go/working'
        return {'ok': good, 'text': 'готов' if good else '', 'model': candidate['model'],
                'problem': '' if good else 'global_regions', 'seconds': .1}
    monkeypatch.setattr(ai, '_run_cli_request', run)
    if stage == 'probe':
        assert ai._ensure_cli_model('opencode')['model'] == 'opencode-go/working'
    else:
        assert ai._call_cli_provider('opencode', 'Вопрос', None) == 'готов'
    assert calls == ['opencode-go/blocked1', 'opencode-go/blocked2', 'opencode-go/blocked3', 'opencode-go/working']
    assert [row['model'] for row in ai._discover_cli_models('opencode')] == ['opencode-go/working']
    assert ai._call_cli_provider('opencode', 'Вопрос', None) == 'готов'
    assert calls[-1] == 'opencode-go/working' and len(calls) == 5


def test_task_deadline_leaves_untried_models_available_for_next_request(monkeypatch):
    from ai_assistant import _AI_DEADLINE
    ai = assistant(monkeypatch)
    monkeypatch.setattr(provider, 'catalog', lambda *args, **kwargs: ['one-free', 'two-free', 'three-free'])
    first = ai._discover_cli_models('opencode')[0]
    ai._remember_cli_model('opencode', first, {'ok': True, 'model': first['model'], 'seconds': .1})
    clock = [100.0]
    monkeypatch.setattr('ai_assistant.time.monotonic', lambda: clock[0])
    def run(*args, **kwargs):
        clock[0] += 2
        return {'ok': False, 'text': '', 'problem': 'timeout', 'seconds': 2}
    fail = Mock(side_effect=run)
    monkeypatch.setattr(ai, '_run_cli_request', fail)
    token = _AI_DEADLINE.set(101.0)
    try:
        with pytest.raises(TimeoutError):
            ai._call_cli_provider('opencode', 'Вопрос', None)
    finally:
        _AI_DEADLINE.reset(token)
    fail.assert_called_once()
    assert [row['model'] for row in ai._discover_cli_models('opencode')] == [
        'opencode/two-free', 'opencode/three-free']
    assert not ai._cli_model_fresh('opencode', ai._load_stats()['_cli_models']['opencode'])
    assert 'opencode' not in ai._load_stats().get('_down', [])


def test_expired_request_budget_does_not_launch_cli(monkeypatch):
    from ai_assistant import _AI_DEADLINE
    ai = assistant(monkeypatch)
    run = Mock()
    monkeypatch.setattr(provider, 'request', run)
    token = _AI_DEADLINE.set(0)
    try:
        with pytest.raises(TimeoutError):
            ai._run_cli_request('opencode', {'model': 'opencode/one-free'}, 'test', None)
    finally:
        _AI_DEADLINE.reset(token)
    run.assert_not_called()


@pytest.mark.parametrize('mode,model,problem', [
    ('free', 'one-free', 'quota'), ('go', 'auto', 'quota'),
    ('go', 'one-free', 'global_regions'),
    ('free', 'auto', 'auth'), ('free', 'auto', 'tool_requested')])
def test_runtime_fallback_respects_manual_model_go_quota_and_tool_errors(monkeypatch, mode, model, problem):
    ai = assistant(monkeypatch, mode=mode, model=model)
    monkeypatch.setattr(provider, 'catalog', lambda *args, **kwargs: ['one-free', 'two-free'])
    first = ai._discover_cli_models('opencode')[0]
    ai._remember_cli_model('opencode', first, {'ok': True, 'model': first['model'], 'seconds': .1})
    run = Mock(return_value={'ok': False, 'text': '', 'problem': problem, 'seconds': .2})
    monkeypatch.setattr(ai, '_run_cli_request', run)
    assert ai._call_cli_provider('opencode', 'Вопрос', None) is None
    run.assert_called_once()


def test_startup_probe_deadline_releases_lock_without_caching_untried_models_as_failed(monkeypatch):
    from ai_assistant import _AI_DEADLINE
    ai = assistant(monkeypatch)
    ai.PROBE_DEADLINE = 1
    monkeypatch.setattr(provider, 'catalog', lambda *args, **kwargs: ['one-free', 'two-free'])
    clock = [100.0]
    monkeypatch.setattr('ai_assistant.time.monotonic', lambda: clock[0])
    def run(*args, **kwargs):
        clock[0] += 2
        return {'ok': False, 'text': '', 'problem': 'timeout', 'seconds': 2}
    fail = Mock(side_effect=run)
    monkeypatch.setattr(ai, '_run_cli_request', fail)
    assert ai.probe_providers()[0][1] is False
    fail.assert_called_once()
    assert _AI_DEADLINE.get() is None
    assert 'opencode' not in ai._load_stats().get('_cli_models', {})
    assert [row['model'] for row in ai._discover_cli_models('opencode')] == ['opencode/two-free']
    lock = ai._provider_lock('opencode')
    assert lock.acquire(blocking=False)
    lock.release()
    monkeypatch.setattr(ai, '_run_cli_request', lambda *args, **kwargs: {
        'ok': True, 'text': 'готов', 'problem': '', 'model': 'opencode/two-free', 'seconds': .1})
    assert ai._call_cli_providers('Вопрос', None) == 'готов'


def test_timeout_is_reported_and_missing_go_login_is_auth(monkeypatch):
    ai = assistant(monkeypatch)
    monkeypatch.setattr(provider, 'request', Mock(side_effect=subprocess.TimeoutExpired('synthetic', 1)))
    assert ai._run_cli_request('opencode', {'model': 'opencode/one-free'}, 'test', None)['problem'] == 'timeout'
    monkeypatch.setattr(provider, 'request', Mock(side_effect=ValueError('not connected')))
    assert ai._run_cli_request('opencode', {'model': 'opencode-go/one'}, 'test', None)['problem'] == 'auth'


def test_free_ranking_respects_existing_primary_and_disabled_provider(monkeypatch):
    ai = assistant(monkeypatch)
    ai.ai_config['cli_providers'] += ['codex']
    ai._ai_stats['opencode'] = {'avg': 20, 'success': 1}
    ai._ai_stats['codex'] = {'avg': 1, 'success': 1}
    assert ai.ai_order()[0] == 'opencode'
    ai.ai_config['primary_ai'] = 'codex'
    assert ai.ai_order()[0] == 'codex'
    ai.ai_config['cli_providers'] = []
    assert 'opencode' not in ai.steps()


def test_config_and_menu_preserve_primary_and_other_credentials(monkeypatch):
    cfg = {'resume_id': 'untouched', 'ai_config': {'primary_ai': 'Antigravity',
           'api_key': 'synthetic-private', 'cli_providers': ['claude', 'codex']}}
    before = copy.deepcopy(cfg)
    monkeypatch.setattr(config_manager, 'load_config', lambda: cfg)
    save = Mock(return_value=True)
    monkeypatch.setattr(config_manager, 'save_config', save)
    monkeypatch.setattr(provider, 'catalog', lambda mode: ['one-free', 'two-free'])
    answers = iter(['1', '3'])
    monkeypatch.setattr('builtins.input', lambda _: next(answers))
    config_manager.edit_opencode_provider()
    assert cfg['ai_config']['opencode'] == {'mode': 'free', 'model': 'two-free'}
    assert cfg['ai_config']['cli_providers'] == ['claude', 'codex', 'opencode']
    for key in ('primary_ai', 'api_key'):
        assert cfg['ai_config'][key] == before['ai_config'][key]
    assert cfg['resume_id'] == before['resume_id']
    save.assert_called_once_with(cfg)


def test_cancelling_model_selection_does_not_save_config(monkeypatch):
    cfg = {'ai_config': {'primary_ai': 'Antigravity'}}
    before = copy.deepcopy(cfg)
    monkeypatch.setattr(config_manager, 'load_config', lambda: cfg)
    save = Mock()
    monkeypatch.setattr(config_manager, 'save_config', save)
    monkeypatch.setattr(provider, 'catalog', lambda mode: ['one-free'])
    answers = iter(['1', ''])
    monkeypatch.setattr('builtins.input', lambda _: next(answers))
    config_manager.edit_opencode_provider()
    assert cfg == before
    save.assert_not_called()


def test_provider_menu_opens_opencode_settings(monkeypatch):
    import test as menu
    cfg = {'ai_config': {}}
    monkeypatch.setattr(menu, 'load_config', lambda: cfg)
    monkeypatch.setattr('ai_assistant.describe_ai_chain', lambda config: [])
    monkeypatch.setattr('builtins.input', lambda _: 'O')
    edit = Mock()
    monkeypatch.setattr(config_manager, 'edit_opencode_provider', edit)
    menu.choose_ai_provider()
    edit.assert_called_once_with()


def test_provider_menu_checks_real_chain_without_saving_settings(monkeypatch, capsys):
    import test as menu
    cfg = {'ai_config': {}}
    monkeypatch.setattr(menu, 'load_config', lambda: cfg)
    monkeypatch.setattr('ai_assistant.describe_ai_chain', lambda config: [])
    monkeypatch.setattr('builtins.input', lambda _: 'T')
    save = Mock()
    monkeypatch.setattr(menu, 'save_config', save)
    call = Mock(return_value='12')
    monkeypatch.setattr(AIAssistant, '_call_llm', call)
    menu.choose_ai_provider()
    assert 'Рабочая очередь вернула 12' in capsys.readouterr().out
    assert call.call_args.kwargs['validate']('12') is None
    assert call.call_args.kwargs['validate']('13')
    save.assert_not_called()


def test_provider_menu_opens_parallel_control_without_saving_settings(monkeypatch):
    import test as menu
    cfg = {'ai_config': {}}
    monkeypatch.setattr(menu, 'load_config', lambda: cfg)
    monkeypatch.setattr('ai_assistant.describe_ai_chain', lambda _config: [])
    monkeypatch.setattr('builtins.input', lambda _: 'W')
    control = Mock()
    save = Mock()
    monkeypatch.setattr('chat_workflow.check_parallel_chat_preparation', control)
    monkeypatch.setattr(menu, 'save_config', save)
    menu.choose_ai_provider()
    control.assert_called_once_with(cfg)
    save.assert_not_called()


@pytest.mark.parametrize('working', ['free', 'go', 'other'])
def test_complete_generation_queue_free_then_go_then_others(monkeypatch, working):
    ai = assistant(monkeypatch, mode='auto')
    ai.enabled = True
    ai.ai_config['primary_ai'] = 'opencode'
    monkeypatch.setattr(ai, 'steps', lambda: ['compat:other', 'opencode'])
    monkeypatch.setattr(provider, 'catalog', lambda mode, **kwargs:
                        ['one-free', 'two-free'] if mode == 'free' else ['blocked', 'working'])
    calls = []
    def run(name, candidate, prompt, *args, **kwargs):
        model = candidate['model']
        calls.append(model)
        good = ((working == 'free' and model == 'opencode/one-free')
                or (working == 'go' and model == 'opencode-go/working'))
        return {'ok': good, 'text': ('готов' if 'одним словом' in prompt else '12') if good else '',
                'model': model, 'seconds': .1, 'problem': '' if good else (
                    'quota' if model.startswith('opencode/') else 'global_regions')}
    monkeypatch.setattr(ai, '_run_cli_request', run)
    def other(*args, **kwargs):
        calls.append('other')
        return '12'
    monkeypatch.setattr(ai, '_call_compat_providers', other)
    assert ai._call_llm('7+5', validate=lambda text: None if text == '12' else 'invalid') == '12'
    if working == 'free':
        assert calls == ['opencode/one-free'] * 2
    else:
        assert calls[:4] == ['opencode/one-free', 'opencode/two-free',
                             'opencode-go/blocked', 'opencode-go/working']
        assert calls[4:] == (['opencode-go/working'] if working == 'go' else ['other'])


def test_combined_mode_menu_sets_primary_without_reading_go_credentials(monkeypatch):
    cfg = {'resume_id': 'unchanged', 'ai_config': {'primary_ai': 'Antigravity', 'api_key': 'synthetic-private'}}
    monkeypatch.setattr(config_manager, 'load_config', lambda: cfg)
    monkeypatch.setattr('builtins.input', lambda _: '3')
    monkeypatch.setattr(provider, 'go_key', Mock(side_effect=AssertionError('No key read before Free exhausts')))
    monkeypatch.setattr(provider, 'catalog', Mock(side_effect=AssertionError('No catalog read when configuring')))
    save = Mock(return_value=True)
    monkeypatch.setattr(config_manager, 'save_config', save)
    config_manager.edit_opencode_provider()
    assert cfg['ai_config']['opencode'] == {'mode': 'auto', 'model': 'auto'}
    assert cfg['ai_config']['primary_ai'] == 'opencode'
    assert cfg['ai_config']['api_key'] == 'synthetic-private'
    assert cfg['resume_id'] == 'unchanged'
    save.assert_called_once_with(cfg)


@pytest.mark.parametrize('mode', ['free', 'go', 'auto'])
def test_settings_cli_uses_isolated_profile_and_rejects_invalid_model(tmp_path, mode):
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, HH_DATA_DIR=str(tmp_path), HH_PROFILE_ID='default')
    command = [sys.executable, str(root / 'hh.py'), 'settings', '--opencode-mode', mode]
    result = subprocess.run(command, env=env, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr.decode('utf-8', errors='replace')
    path = tmp_path / 'hh_selenium_config.json'
    config = json.loads(path.read_text(encoding='utf-8'))
    assert config['ai_config']['opencode'] == {'mode': mode, 'model': 'auto'}
    if mode == 'auto':
        assert config['ai_config']['primary_ai'] == 'opencode'
    assert 'opencode' in config['ai_config']['cli_providers']
    before = path.read_bytes()
    result = subprocess.run(command + ['--opencode-model', '../invalid'], env=env,
                            capture_output=True, timeout=20)
    assert result.returncode == 1
    assert path.read_bytes() == before

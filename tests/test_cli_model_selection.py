import json
import io
import queue
import subprocess
from unittest.mock import Mock

import pytest

from ai_assistant import AIAssistant


def assistant(monkeypatch, providers=('codex', 'claude')):
    a = AIAssistant.__new__(AIAssistant)
    a.ai_config = {'cli_providers': list(providers), 'auto_explore': 0}
    a._ai_stats = {}
    a.find_cli = lambda name: 'synthetic-' + name
    monkeypatch.setattr(a, '_save_stats', Mock())
    return a


def candidates():
    return [
        {'model': 'gpt-future-luna', 'effort': 'low', 'reference_cost': 0.6},
        {'model': 'gpt-future-sol', 'effort': 'low', 'reference_cost': 12.0},
    ]


def reply(model, ok=True, seconds=0.7, problem=''):
    return {'text': 'готов' if ok else '', 'model': model, 'ok': ok,
            'seconds': seconds, 'problem': problem}


def test_codex_discovers_tests_and_remembers_model(monkeypatch):
    a = assistant(monkeypatch)
    discover = Mock(return_value=candidates())
    run = Mock(side_effect=lambda name, row, *args, **kwargs: reply(row['model']))
    monkeypatch.setattr(a, '_discover_cli_models', discover)
    monkeypatch.setattr(a, '_run_cli_request', run)
    row = a._ensure_cli_model('codex')
    assert row['model'] == 'gpt-future-luna'
    assert row['available'] and row['seconds'] == 0.7
    assert a._load_stats()['_cli_models']['codex'] == row
    assert a._ensure_cli_model('codex') == row
    assert discover.call_count == run.call_count == 1


def test_model_catalog_is_refreshed_after_ttl_and_cli_update(monkeypatch):
    a = assistant(monkeypatch)
    discover = Mock(return_value=candidates())
    monkeypatch.setattr(a, '_discover_cli_models', discover)
    monkeypatch.setattr(a, '_run_cli_request', lambda name, row, *args, **kwargs: reply(row['model']))
    a._ensure_cli_model('codex')
    a._ai_stats['_cli_models']['codex']['retry_at'] = 1
    a._ensure_cli_model('codex')
    a.find_cli = lambda name: 'new-synthetic-' + name
    a._ensure_cli_model('codex')
    assert discover.call_count == 3


def test_quota_does_not_trigger_expensive_model_fallback(monkeypatch):
    a = assistant(monkeypatch)
    monkeypatch.setattr(a, '_discover_cli_models', lambda name: candidates())
    run = Mock(return_value=reply('gpt-future-luna', False, problem='weekly_quota'))
    monkeypatch.setattr(a, '_run_cli_request', run)
    assert a._ensure_cli_model('codex') is None
    assert a._ensure_cli_model('codex') is None
    assert run.call_count == 1
    assert not a._ai_stats['_cli_models']['codex']['available']


def test_unavailable_model_tries_next_cheapest(monkeypatch):
    a = assistant(monkeypatch)
    monkeypatch.setattr(a, '_discover_cli_models', lambda name: candidates())
    run = Mock(side_effect=[reply('gpt-future-luna', False, problem='model_unavailable'),
                            reply('gpt-future-sol')])
    monkeypatch.setattr(a, '_run_cli_request', run)
    assert a._ensure_cli_model('codex')['model'] == 'gpt-future-sol'
    assert run.call_count == 2


@pytest.mark.parametrize('failures,selected', [
    ([], 'haiku'),
    (['model_unavailable'], 'sonnet'),
    (['timeout'], 'sonnet'),
    (['model_unavailable', 'model_unavailable'], 'opus'),
    (['weekly_quota'], None),
    (['auth'], None),
])
def test_claude_selects_first_available_economy_alias(monkeypatch, failures, selected):
    a = assistant(monkeypatch, providers=('claude',))
    attempts = []

    def run(name, row, *args, **kwargs):
        attempts.append(row['model'])
        if len(attempts) <= len(failures):
            return reply(row['model'], False, problem=failures[len(attempts) - 1])
        return reply('claude-' + row['model'] + '-future')

    monkeypatch.setattr(a, '_run_cli_request', run)
    row = a._ensure_cli_model('claude')
    expected = ['haiku', 'sonnet', 'opus'][:len(failures) + bool(selected)]
    assert attempts == expected
    if selected:
        assert row['requested_model'] == selected
        assert row['model'] == 'claude-' + selected + '-future'
        assert row['available'] and row['seconds'] == 0.7
    else:
        assert row is None
        assert a._ai_stats['_cli_models']['claude']['problem'] == failures[-1]
    assert a._ensure_cli_model('claude') == row
    assert attempts == expected
    if selected and selected != 'haiku':
        row['retry_at'] = 0
        cheaper = Mock(return_value=reply('claude-haiku-future'))
        monkeypatch.setattr(a, '_run_cli_request', cheaper)
        assert a._ensure_cli_model('claude')['requested_model'] == 'haiku'
        assert cheaper.call_args.args[1]['model'] == 'haiku'
        cheaper.assert_called_once()


def test_startup_checks_cli_even_without_compat_service(monkeypatch):
    a = assistant(monkeypatch)
    monkeypatch.setattr(a, '_discover_cli_models', lambda name: (
        candidates() if name == 'codex' else [{'model': 'haiku', 'effort': 'low'}]))
    monkeypatch.setattr(a, '_run_cli_request', lambda name, row, *args, **kwargs: reply(row['model']))
    report = a.probe_report()
    assert 'gpt-future-luna' in report and 'haiku' in report
    assert a._ai_stats['codex']['n'] == 1
    assert a.probe_providers() == []


def test_codex_command_overrides_global_model_and_effort(monkeypatch):
    a = assistant(monkeypatch)
    def run(cmd, **kwargs):
        with open(cmd[cmd.index('-o') + 1], 'w', encoding='utf-8') as f:
            f.write('готов')
        return subprocess.CompletedProcess(cmd, 0, b'', b'')
    runner = Mock(side_effect=run)
    monkeypatch.setattr('subprocess.run', runner)
    result = a._run_cli_request('codex', candidates()[0], 'test', None)
    cmd = runner.call_args.args[0]
    assert cmd[cmd.index('--model') + 1] == 'gpt-future-luna'
    assert 'model_reasoning_effort="low"' in cmd
    assert '--ephemeral' in cmd and 'read-only' in cmd
    assert result['ok'] and result['text'] == 'готов'


def test_claude_alias_tracks_actual_model_and_blocks_expensive_fallback(monkeypatch):
    a = assistant(monkeypatch)
    result = {'type': 'result', 'is_error': False, 'result': 'готов',
              'modelUsage': {'claude-haiku-future': {'inputTokens': 10}}}
    runner = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(result).encode(), b''))
    monkeypatch.setattr('subprocess.run', runner)
    row = {'model': 'haiku', 'effort': 'low'}
    output = a._run_cli_request('claude', row, 'test', None)
    cmd = runner.call_args.args[0]
    assert cmd[cmd.index('--model') + 1] == 'haiku'
    assert cmd[cmd.index('--fallback-model') + 1] == 'haiku'
    assert cmd[cmd.index('--tools') + 1] == ''
    assert '--safe-mode' in cmd
    assert '--bare' not in cmd
    assert output['ok'] and output['model'] == 'claude-haiku-future'


def test_failed_generation_invalidates_verified_model(monkeypatch):
    a = assistant(monkeypatch)
    monkeypatch.setattr(a, '_discover_cli_models', lambda name: candidates())
    run = Mock(side_effect=[reply('gpt-future-luna'),
                            reply('gpt-future-luna', False, problem='timeout')])
    monkeypatch.setattr(a, '_run_cli_request', run)
    assert a._call_cli_provider('codex', 'test', None) is None
    assert not a._ai_stats['_cli_models']['codex']['available']
    assert a._call_cli_provider('codex', 'test', None) is None
    assert run.call_count == 2


def test_cli_selection_cache_persists_and_is_reused(monkeypatch, tmp_path):
    a = assistant(monkeypatch)
    path = tmp_path / 'stats.json'
    a._stats_path = lambda: str(path)
    a._save_stats = AIAssistant._save_stats.__get__(a)
    monkeypatch.setattr(a, '_discover_cli_models', lambda name: candidates())
    monkeypatch.setattr(a, '_run_cli_request', lambda name, row, *args, **kwargs: reply(row['model']))
    a._ensure_cli_model('codex')
    b = assistant(monkeypatch)
    b._ai_stats = None
    b._stats_path = lambda: str(path)
    discover = Mock(side_effect=AssertionError('cached selection should be used'))
    monkeypatch.setattr(b, '_discover_cli_models', discover)
    assert b._ensure_cli_model('codex')['seconds'] == 0.7
    discover.assert_not_called()


def test_catalog_rank_uses_prices_not_model_name():
    from cli_models import rank_codex_models, parse_openai_prices
    html = '''<table><tr><th>Model</th><th>Input</th><th>Cached input</th><th>Output</th></tr>
    <tr><td>gpt-new-economy</td><td>$0.05</td><td>$0.01</td><td>$0.20</td></tr>
    <tr><td>gpt-6-luna</td><td>$0.10</td><td>$0.01</td><td>$0.50</td></tr></table>
    <table><tr><th>Model</th><th>Input</th><th>Output</th></tr>
    <tr><td>gpt-6-luna</td><td>$0.05</td><td>$0.25</td></tr></table>'''
    prices = parse_openai_prices(html)
    models = [{'model': name, 'supportedReasoningEfforts': [{'reasoningEffort': 'low'}]}
              for name in ('gpt-6-astra', 'gpt-6-luna', 'gpt-new-economy')]
    ranked = rank_codex_models(models, prices)
    assert ranked[0]['model'] == 'gpt-new-economy'
    assert prices['gpt-6-luna']['output'] == 0.5  # not the batch table


def test_unpriced_catalog_does_not_inherit_expensive_default():
    from cli_models import rank_codex_models
    rows = rank_codex_models([{'model': 'gpt-next-astra', 'isDefault': True},
                             {'model': 'gpt-next-luna'}], {})
    assert rows[0]['model'] == 'gpt-next-luna'
    assert all(row['model'] != 'gpt-next-astra' for row in rows)


def test_probe_failure_text_is_not_considered_a_real_answer(monkeypatch):
    a = assistant(monkeypatch)
    monkeypatch.setattr(a, '_discover_cli_models', lambda name: candidates())
    run = Mock(return_value=dict(reply('gpt-future-luna'), text='API is temporarily unavailable'))
    monkeypatch.setattr(a, '_run_cli_request', run)
    assert a._ensure_cli_model('codex') is None


def test_active_label_names_actual_cli_not_preferred_service(monkeypatch):
    a = assistant(monkeypatch)
    a.enabled = True
    a.ensure_model_resolved = lambda: None
    a.ai_order = lambda: ['compat:preferred', 'codex']
    a._using_cli = 'codex'
    a._ai_stats['_cli_models'] = {'codex': {'model': 'gpt-future-luna'}}
    assert a.active_model_label() == 'ChatGPT: gpt-future-luna'


def test_successful_requests_do_not_extend_catalog_selection_forever(monkeypatch):
    a = assistant(monkeypatch)
    monkeypatch.setattr(a, '_discover_cli_models', lambda name: candidates())
    monkeypatch.setattr(a, '_run_cli_request', lambda name, row, *args, **kwargs: reply(row['model']))
    first = a._ensure_cli_model('codex')
    assert a._call_cli_provider('codex', 'test', None)
    last = a._load_stats()['_cli_models']['codex']
    assert last['checked_at'] == first['checked_at']
    assert last['retry_at'] == first['retry_at']


def fake_catalog_process(messages):
    writes = []
    process = Mock()
    process.stdin.write.side_effect = lambda line: writes.append(json.loads(line))
    process.stdout = io.StringIO(''.join(json.dumps(message) + '\n' for message in messages))
    process.poll.return_value = None
    process.wait.return_value = 0
    return process, writes


def test_catalog_handshake_pagination_and_process_cleanup(monkeypatch):
    from cli_models import codex_catalog
    process, writes = fake_catalog_process([
        {'id': 1, 'result': {}}, {'method': 'unrelated/notification'},
        {'id': 2, 'result': {'data': [{'model': 'gpt-new-luna'}], 'nextCursor': 'next'}},
        {'id': 3, 'result': {'data': [{'model': 'gpt-other-mini'}], 'nextCursor': None}}])
    monkeypatch.setattr('subprocess.Popen', Mock(return_value=process))
    assert [row['model'] for row in codex_catalog('synthetic-cli')] == ['gpt-new-luna', 'gpt-other-mini']
    assert [row['method'] for row in writes] == ['initialize', 'initialized', 'model/list', 'model/list']
    assert writes[-1]['params']['cursor'] == 'next'
    process.terminate.assert_called_once()
    process.wait.assert_called_once()
    assert process.stdout.closed


def test_catalog_rpc_error_still_closes_process(monkeypatch):
    from cli_models import codex_catalog
    process, _ = fake_catalog_process([{'id': 1, 'error': {'message': 'synthetic error'}}])
    monkeypatch.setattr('subprocess.Popen', Mock(return_value=process))
    with pytest.raises(RuntimeError, match='request failed'):
        codex_catalog('synthetic-cli')
    process.terminate.assert_called_once()
    process.wait.assert_called_once()
    assert process.stdout.closed


def test_catalog_timeout_kills_and_reaps_stuck_process(monkeypatch):
    from cli_models import codex_catalog
    process, _ = fake_catalog_process([])
    process.wait.side_effect = [subprocess.TimeoutExpired('synthetic-cli', 5), 0]
    monkeypatch.setattr('subprocess.Popen', Mock(return_value=process))
    messages = Mock()
    messages.get.side_effect = queue.Empty
    monkeypatch.setattr('cli_models.queue.Queue', lambda: messages)
    with pytest.raises(TimeoutError):
        codex_catalog('synthetic-cli')
    process.kill.assert_called_once()
    assert process.wait.call_count == 2


def test_claude_does_not_accept_a_silent_expensive_model(monkeypatch):
    a = assistant(monkeypatch)
    payload = {'is_error': False, 'result': 'готов', 'modelUsage': {'claude-opus-future': {}}}
    monkeypatch.setattr('subprocess.run', Mock(return_value=subprocess.CompletedProcess(
        [], 0, json.dumps(payload).encode(), b'')))
    assert not a._run_cli_request('claude', {'model': 'haiku'}, 'test', None)['ok']


@pytest.mark.parametrize('provider', ['claude', 'codex', 'catalog'])
def test_locked_cli_directory_does_not_discard_response(monkeypatch, caplog, provider):
    import shutil
    import tempfile
    from pathlib import Path
    from cli_models import codex_catalog

    directories = []
    original_mkdtemp = tempfile.mkdtemp
    original_rmtree = shutil.rmtree

    def create(*args, **kwargs):
        path = original_mkdtemp(*args, **kwargs)
        directories.append(path)
        return path

    def locked(path, *args, **kwargs):
        if str(path) in directories:
            raise PermissionError('Synthetic Windows directory lock')
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(tempfile, 'mkdtemp', create)
    monkeypatch.setattr(shutil, 'rmtree', locked)
    try:
        if provider == 'catalog':
            process, _ = fake_catalog_process([
                {'id': 1, 'result': {}},
                {'id': 2, 'result': {'data': [{'model': 'gpt-future-luna'}]}}])
            monkeypatch.setattr('subprocess.Popen', Mock(return_value=process))
            assert codex_catalog('synthetic-cli') == [{'model': 'gpt-future-luna'}]
            process.terminate.assert_called_once()
        else:
            def run(cmd, **kwargs):
                if provider == 'codex':
                    Path(cmd[cmd.index('-o') + 1]).write_text('готов', encoding='utf-8')
                payload = {'result': 'готов', 'modelUsage': {'claude-haiku-future': {}}}
                return subprocess.CompletedProcess(cmd, 0, json.dumps(payload).encode(), b'')

            monkeypatch.setattr('subprocess.run', run)
            a = assistant(monkeypatch)
            row = {'model': 'haiku' if provider == 'claude' else 'gpt-future-luna'}
            result = a._run_cli_request(provider, row, 'test', None)
            assert result['ok'] and result['text'] == 'готов'
            assert result['problem'] == ''
        assert 'временн' in caplog.text.lower()
        assert 'продолжа' in caplog.text.lower()
    finally:
        for path in directories:
            original_rmtree(path)


@pytest.mark.parametrize('detail', [
    "The 'gpt-new-luna' model is not supported with a ChatGPT account",
    '{"code":"model_not_supported"}',
    "The model 'gpt-new-luna' is not available",
])
def test_model_access_errors_can_refresh_catalog(detail):
    from cli_models import cli_problem
    assert cli_problem(detail) == 'model_unavailable'

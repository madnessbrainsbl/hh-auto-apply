import threading
import time
from unittest.mock import Mock

from ai_assistant import AIAssistant


def assistant():
    ai = AIAssistant.__new__(AIAssistant)
    ai.enabled = True
    ai.ai_config = {'primary_ai': 'opencode', 'auto_explore': 0, 'cli_providers': []}
    ai._ai_stats = {}
    ai.steps = lambda: ['opencode', 'compat:ready']
    return ai


def test_failed_pinned_provider_is_not_retried_on_every_task():
    ai = assistant()
    ai._call_step = Mock(side_effect=lambda step, *_: None if step == 'opencode' else 'ответ')
    assert ai._call_llm('first') == 'ответ'
    assert ai._call_llm('second') == 'ответ'
    assert [call.args[0] for call in ai._call_step.call_args_list] == [
        'opencode', 'compat:ready', 'compat:ready']


def test_first_ready_returns_before_slow_probe_and_publishes_late_success():
    ai = assistant()
    ai.steps = lambda: ['compat:ready', 'compat:slow']
    release = threading.Event()
    published = threading.Event()
    original = ai._record

    def probe(step):
        if step.endswith('slow'):
            assert release.wait(3)
        return step, True, 0.01

    def record(step, *args):
        original(step, *args)
        if step.endswith('slow'):
            published.set()

    ai._probe_one = probe
    ai._record = record
    started = time.monotonic()
    try:
        result = ai.probe_providers(first_ready=True)
        assert time.monotonic() - started < 1
        assert any(step == 'compat:ready' and ok for step, ok, _ in result)
        assert 'compat:slow' not in ai._load_stats().get('_down', [])
        assert ai.probe_providers(first_ready=True) == []
    finally:
        release.set()
    assert published.wait(3)
    assert ai._load_stats()['compat:slow']['n'] == 1


def test_cloud_provider_not_classified_as_closed_local_app(monkeypatch):
    ai = assistant()
    ai._compat_providers = lambda: [{'name': 'Cloud', 'base_url': 'https://cloud.invalid/v1'}]
    connect = Mock(side_effect=OSError('Direct TCP is blocked; proxy is configured'))
    monkeypatch.setattr('socket.create_connection', connect)
    assert ai.closed_compat_providers() == []
    connect.assert_not_called()


def test_stats_are_shared_between_stages_without_stale_overwrite():
    first, second = assistant(), assistant()
    del first._ai_stats, second._ai_stats
    first._load_stats()
    second._load_stats()
    first._record('compat:one', True, 1)
    second._record('compat:two', True, 2)
    assert 'compat:two' in first._load_stats()
    assert 'compat:one' in second._load_stats()


def test_busy_ready_model_is_reused_after_slot_is_released():
    from concurrent.futures import ThreadPoolExecutor

    ai = assistant()
    ai.steps = lambda: ['compat:ready']
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def answer(*_args, **_kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(3)
        return 'ответ'

    ai._call_compat_providers = answer
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(ai._call_llm, 'first')
        assert entered.wait(2)
        second = pool.submit(ai._call_llm, 'second')
        try:
            assert not second.done()
        finally:
            release.set()
        assert first.result(timeout=3) == 'ответ'
        assert second.result(timeout=3) == 'ответ'
    assert len(calls) == 2
    assert ai._load_stats()['compat:ready']['n'] == 2
    assert not ai._load_stats().get('_down')


def test_foreground_reuses_verified_go_model_without_free_catalogue_scan():
    ai = assistant()
    ai.ai_config = {'cli_providers': ['opencode'], 'opencode': {'mode': 'auto'}}
    ai.steps = lambda: ['opencode']
    ai.find_cli = lambda _name: 'synthetic-cli'
    cached = {'model': 'opencode-go/verified', 'available': True, 'seconds': 1}
    ai._ai_stats['_cli_models'] = {'opencode': cached}
    ai._cli_model_fresh = lambda *_: True
    ai._discover_cli_models = Mock(side_effect=AssertionError('No discovery during foreground work'))
    ai._run_cli_request = Mock(return_value={'ok': True, 'text': 'ответ', 'seconds': 1})
    ai._remember_cli_model = Mock(return_value=cached)
    assert ai._call_llm('question') == 'ответ'
    ai._discover_cli_models.assert_not_called()
    ai._run_cli_request.assert_called_once()


def test_service_error_cools_down_but_quality_rejection_does_not_disable_model():
    ai = assistant()
    ai.steps = lambda: ['compat:broken', 'compat:ready']
    ai._call_step = Mock(side_effect=lambda step, *_: 'ответ' if step.endswith('ready') else 'synthetic service failure')
    import ai_assistant
    original = ai_assistant.service_message_problem
    from unittest.mock import patch
    with patch.object(ai_assistant, 'service_message_problem', side_effect=lambda text:
                      'service' if text == 'synthetic service failure' else original(text)):
        assert ai._call_llm('one') == 'ответ'
        assert ai._load_stats()['compat:broken']['retry_at'] > time.time()
    ai._ai_stats = {}
    ai._call_step = Mock(return_value='different technical answer')
    ai._call_llm('one', validate=lambda _text: 'Ответ не по теме')
    assert not ai._load_stats().get('compat:broken', {}).get('retry_at')


def test_named_primary_uses_ready_model_without_front_loading_untested_siblings():
    ai = assistant()
    ai.ai_config['primary_ai'] = 'Synthetic'
    ai.steps = lambda: ['compat:cold', 'compat:ready']
    ai._compat_providers = lambda: [{'name': 'Synthetic', 'models': ['cold', 'ready']}]
    ai._record('compat:ready', True, 1)
    assert ai.ai_order()[0] == 'compat:ready'


def test_newly_ready_free_model_retains_priority_over_active_paid_route():
    ai = assistant()
    ai.ai_config['primary_ai'] = 'auto'
    ai.steps = lambda: ['compat:paid', 'compat:one:free']
    ai._record('compat:paid', True, 1)
    ai._ai_stats['_active_step'] = 'compat:paid'
    ai._record('compat:one:free', True, 2)
    assert ai.ai_order()[0] == 'compat:one:free'


def test_menu_does_not_count_background_unchecked_models_as_working(monkeypatch):
    from ai_assistant import ai_menu_lines

    config = {'ai_config': {'cli_providers': [], 'openai_compatible': [
        {'name': 'Synthetic', 'base_url': 'https://synthetic.invalid/v1', 'models': ['ready', 'unchecked']}]}}
    monkeypatch.setattr(AIAssistant, '_compat_providers', lambda self: self.ai_config['openai_compatible'])
    monkeypatch.setattr(AIAssistant, 'closed_compat_providers', lambda self: [])
    monkeypatch.setattr(AIAssistant, '_load_stats', lambda self: {
        '_probe_version': self.PROBE_VERSION, 'compat:ready': {'ready_at': time.time()}})
    head, queue = ai_menu_lines(config)
    assert 'первым: Synthetic: ready' in head
    assert '1 из 2 модели отвечают' in queue


def test_new_stage_does_not_reprobe_healthy_cloud_models_when_local_service_is_closed():
    first, second = assistant(), assistant()
    del first._ai_stats, second._ai_stats
    providers = [
        {'name': 'Local', 'base_url': 'http://127.0.0.1:8080/v1', 'models': ['local']},
        {'name': 'Cloud', 'base_url': 'https://cloud.invalid/v1', 'models': ['cloud']}]
    for ai in (first, second):
        ai.steps = lambda: ['compat:local', 'compat:cloud']
        ai._compat_providers = lambda: providers
        ai.closed_compat_providers = lambda: ['Local']
        ai._probe_one = Mock(side_effect=lambda step: (step, True, .1))
    first.probe_report()
    assert first._probe_one.call_count == 1
    assert second.probe_providers() == []
    second._probe_one.assert_not_called()


def test_failed_catalogue_is_not_retried_by_every_menu_instance(monkeypatch):
    first, second = assistant(), assistant()
    del first._ai_stats, second._ai_stats
    provider = {'name': 'Local', 'base_url': 'http://127.0.0.1:8080/v1', 'api_key': 'synthetic'}
    get = Mock(side_effect=ConnectionError('offline'))
    monkeypatch.setattr('requests.get', get)
    assert first._discovered_models(provider) is None
    assert second._discovered_models(provider) is None
    assert get.call_count == 1


def test_live_control_uses_real_preparation_pipeline_with_isolated_history(monkeypatch, tmp_path):
    import copy
    import json
    from chat_workflow import check_parallel_chat_preparation
    from rejection_analyzer import RejectionAnalyzer

    ai = assistant()
    ai.steps = lambda: ['compat:a', 'compat:b', 'compat:c']
    ai._compat_providers = lambda: [{'base_url': 'https://synthetic.invalid/v1', 'models': ['a', 'b', 'c']}]
    together = threading.Barrier(3)

    def answer(*_args, **_kwargs):
        together.wait(timeout=3)
        return 'Проверяю права доступа и обработку некорректных входных данных в API.'

    ai._call_compat_providers = answer
    monkeypatch.setattr('ai_assistant.AIAssistant', lambda _cfg: ai)
    forbidden = Mock(side_effect=AssertionError('Control must not touch HH'))
    monkeypatch.setattr(RejectionAnalyzer, 'send_chat_reply', forbidden)
    monkeypatch.setattr(RejectionAnalyzer, '_init_driver', forbidden)
    config = {'candidate_profile': {'contacts': {'telegram': 'private-contact'}}, 'ai_config': {}}
    before = copy.deepcopy(config)
    report = check_parallel_chat_preparation(config)
    assert report['ok'] is True
    assert report['peak_workers'] == 3
    assert report['peak_provider_requests'] == 3
    assert report['hh_messages_sent'] == 0
    assert config == before
    assert len({row['worker'] for row in report['tasks']}) == 3
    assert {row['provider'] for row in report['requests']} == {'compat:a', 'compat:b', 'compat:c'}
    forbidden.assert_not_called()
    import rejection_analyzer
    from pathlib import Path
    files = list(Path(rejection_analyzer.SCRIPT_DIR).glob('.live_runs/*-parallel-ai.json'))
    assert len(files) == 1
    assert 'private-contact' not in files[0].read_text(encoding='utf-8')
    assert json.loads(files[0].read_text(encoding='utf-8')) == report
    assert not (Path(rejection_analyzer.SCRIPT_DIR) / 'chat_actions.json').exists()

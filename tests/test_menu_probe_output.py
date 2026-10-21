import io
import logging
import threading
from unittest.mock import Mock

import pytest

from ai_assistant import AIAssistant
from terminal_ui import hide_technical_from_console


@pytest.fixture
def outputs(tmp_path, monkeypatch):
    stream = io.StringIO()
    console = logging.StreamHandler(stream)
    hide_technical_from_console(console)
    path = tmp_path / 'probes.log'
    file_handler = logging.FileHandler(path, encoding='utf-8')
    logger = logging.getLogger('hh_test_menu_probes')
    monkeypatch.setattr(logger, 'handlers', [file_handler, console])
    monkeypatch.setattr(logger, 'propagate', False)
    monkeypatch.setattr(logger, 'level', logging.DEBUG)
    try:
        yield logger, stream, path
    finally:
        file_handler.close()
        console.close()


def probe_assistant(steps):
    ai = AIAssistant.__new__(AIAssistant)
    ai.enabled = True
    ai.ai_config = {'cli_providers': []}
    ai._ai_stats = {}
    ai.steps = lambda: steps
    return ai


def test_late_background_probes_leave_prompt_clean_but_keep_file_logs(outputs):
    logger, console, path = outputs
    ai = probe_assistant(['compat:ready', 'compat:slow'])
    release = threading.Event()

    def probe(step):
        if step == 'compat:slow':
            assert release.wait(3)
        logger.info('OpenCode: проверка %s', step)
        logger.warning('CLI: диагностическое предупреждение %s', step)
        return step, True, 0.01

    ai._probe_one = probe
    try:
        assert ai.probe_providers(first_ready=True)
        console.write('ОЖИДАНИЕ ВВОДА: Enter')
    finally:
        release.set()
        for thread in ai._runtime().get('probe_threads', []):
            thread.join(timeout=3)
            assert not thread.is_alive()
    assert console.getvalue() == 'ОЖИДАНИЕ ВВОДА: Enter'
    logged = path.read_text(encoding='utf-8')
    assert 'compat:ready' in logged and 'compat:slow' in logged
    assert 'диагностическое предупреждение' in logged
    assert ai._load_stats()['compat:slow']['n'] == 1
    logger.error('Ошибка рабочего цикла')
    assert 'Ошибка рабочего цикла' in console.getvalue()


def test_explicit_provider_check_remains_visible(outputs):
    logger, console, _ = outputs
    ai = probe_assistant(['compat:ready'])
    def probe(step):
        logger.info('Ручная проверка провайдера')
        return step, True, 0.01
    ai._probe_one = probe
    assert ai.probe_providers(first_ready=False)
    for thread in ai._runtime().get('probe_threads', []):
        thread.join(timeout=3)
    assert 'Ручная проверка провайдера' in console.getvalue()


def test_main_menu_explicitly_states_it_waits_for_enter(monkeypatch, capsys):
    import test as menu
    monkeypatch.setattr(menu.sys, 'argv', ['test.py'])
    monkeypatch.setattr(menu, 'get_active_resume', lambda: ('synthetic', 'Test'))
    monkeypatch.setattr(menu, 'get_active_preset', lambda: {'name': 'Test'})
    monkeypatch.setattr(menu, 'check_first_run_setup', lambda: [])
    monkeypatch.setattr(menu, 'search_direction_chosen', lambda _: True)
    monkeypatch.setattr(menu, 'load_config', lambda: {'ai_config': {'enabled': False}})
    monkeypatch.setattr(AIAssistant, 'probe_report', lambda *a, **kw: '')
    monkeypatch.setattr('ai_assistant.ai_menu_lines', lambda _: ('Test', 'Test'))
    launch = Mock(side_effect=AssertionError('Must not start a job while waiting'))
    monkeypatch.setattr(menu.subprocess, 'run', launch)
    def choose(prompt):
        output = capsys.readouterr().out
        assert 'ОЖИДАНИЕ ВВОДА' in output and 'Enter' in output
        assert 'hh_auto_apply.log' in output
        return '0'
    monkeypatch.setattr('builtins.input', choose)
    menu.main()
    launch.assert_not_called()

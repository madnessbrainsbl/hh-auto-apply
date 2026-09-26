# -*- coding: utf-8 -*-
"""Проверка ИИ при запуске: живые ответы, а не давняя статистика."""
from ai_assistant import AIAssistant, ai_menu_lines


def _assistant():
    a = AIAssistant.__new__(AIAssistant)
    a.enabled = True
    a.ai_config = {'auto_explore': 0}
    a._ai_stats = {}
    a.steps = lambda: ['compat:fast', 'compat:slow', 'compat:dead', 'gemini', 'claude', 'codex']
    answers = {'compat:fast': (True, 1.0), 'compat:slow': (True, 9.0), 'compat:dead': (False, 20.0)}
    a._probe_one = lambda step: (step, *answers[step])
    return a


def test_probe_orders_queue_by_live_answers():
    a = _assistant()
    # У «dead» хорошая история, но сейчас он не отвечает (Google отказывает по
    # стране) — он в конце очереди, а не вторым, как было 24.09.
    a._ai_stats = {'compat:dead': {'avg': 5.0, 'success': 0.95, 'n': 20}}
    results = a.probe_providers()
    assert len(results) == 3                    # только модели сервиса
    order = a.ai_order()
    assert order[0] == 'compat:fast'
    assert order[-1] == 'compat:dead'
    # Ожил по ходу прогона — снова по статистике.
    a._record('compat:dead', True, 5.0)
    assert a.ai_order()[0] == 'compat:dead'


def test_probe_is_skipped_when_recent_and_forced_on_demand():
    a = _assistant()
    assert a.probe_providers()
    assert a.probe_providers() == []            # только что проверяли
    assert a.probe_providers(force=True)        # по требованию — снова


def test_probe_report_line():
    a = _assistant()
    line = a.probe_report()
    assert line.startswith('Проверка ИИ: fast 1.0 с')
    assert 'dead — не ответил' in line


def test_menu_lines_group_service_models(monkeypatch):
    cfg = {'ai_config': {'auto_explore': 0, 'openai_compatible': [
        {'name': 'Antigravity', 'base_url': 'http://x/v1', 'models': ['m1', 'm2', 'm3']}]}}
    # conftest выключает сервисы; здесь нужен настоящий список.
    monkeypatch.setattr(AIAssistant, '_compat_providers', lambda self: self.ai_config['openai_compatible'])
    monkeypatch.setattr(AIAssistant, 'closed_compat_providers', lambda self: [])
    monkeypatch.setattr(AIAssistant, '_load_stats', lambda self: {})
    head, queue = ai_menu_lines(cfg)
    assert head.startswith('Авто, первым: Antigravity: m1')
    assert queue.startswith('очередь: Antigravity (3 модели)')
    assert queue.count('Antigravity') == 1

    # Живое состояние: m1 не ответил на проверке — первым m2, в очереди «2 из 3».
    monkeypatch.setattr(AIAssistant, '_load_stats', lambda self: {'_down': ['compat:m1']})
    head, queue = ai_menu_lines(cfg)
    assert head.startswith('Авто, первым: Antigravity: m2')
    assert 'Antigravity (2 из 3 модели отвечают)' in queue

    # Сервис закрыт — так и написано, первым его нет.
    monkeypatch.setattr(AIAssistant, 'closed_compat_providers', lambda self: ['Antigravity'])
    head, queue = ai_menu_lines(cfg)
    assert 'Antigravity' not in head
    assert 'Antigravity (3 модели) — не запущен' in queue


def test_closed_service_is_named_and_not_probed(monkeypatch):
    """Antigravity забыли включить — так и говорим, модели не опрашиваем."""
    a = AIAssistant.__new__(AIAssistant)
    a.enabled = True
    a.ai_config = {'auto_explore': 0, 'openai_compatible': [
        {'name': 'Antigravity', 'base_url': 'http://127.0.0.1:9/v1', 'models': ['m1', 'm2']}]}
    a._ai_stats = {}
    monkeypatch.setattr(AIAssistant, '_compat_providers', lambda self: self.ai_config['openai_compatible'])
    asked = []
    a._probe_one = lambda step: asked.append(step) or (step, True, 1.0)
    report = a.probe_report()
    assert report.startswith('Antigravity не запущен — похоже, его забыли включить. '
                             'Перехожу на доступный ИИ: Gemini')
    assert asked == []
    assert 'Antigravity' in a._compat_rest     # во время работы пропускается сразу

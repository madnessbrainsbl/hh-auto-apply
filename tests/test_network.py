# -*- coding: utf-8 -*-
"""Короткий обрыв сети не останавливает бота.

24.09: сеть пропала на секунды — все 15 разборов отказов провалились за доли
секунды, а прогон откликов замер навсегда на «Составляю письмо…».
"""
import terminal_ui
from unittest.mock import Mock

import pytest
import requests


def test_aborted_connection_has_a_specific_explanation():
    error = requests.ConnectionError('Connection aborted.', 'Remote end closed connection without response')
    assert 'соединение' in terminal_ui.explain_error(error)
    assert 'неизвестный' not in terminal_ui.explain_error(error)


@pytest.mark.parametrize('method, failures, succeeds, expected_calls', [
    ('GET', 1, True, 2), ('GET', 10, False, 3), ('POST', 1, False, 1),
])
def test_api_retries_transport_errors_only_for_reads(monkeypatch, method, failures, succeeds, expected_calls):
    import test as menu
    bot = menu.HHAutoApplicant.__new__(menu.HHAutoApplicant)
    bot.user_agent = 'synthetic test'
    bot.access_token = None
    response = Mock(status_code=200)
    bot.session = Mock()
    bot.session.request.side_effect = [requests.ConnectionError('Connection aborted.')] * failures + [response]
    monkeypatch.setattr(menu.time, 'sleep', lambda _seconds: None)
    if succeeds:
        assert bot.make_request(method, 'https://api.hh.ru/resumes/mine') is response
    else:
        with pytest.raises(requests.ConnectionError):
            bot.make_request(method, 'https://api.hh.ru/resumes/mine')
    assert bot.session.request.call_count == expected_calls


def test_wait_for_network_returns_when_connection_is_back(monkeypatch):
    states = iter([False, False, True])
    monkeypatch.setattr(terminal_ui, 'network_is_up', lambda *a, **k: next(states))
    monkeypatch.setattr('time.sleep', lambda s: None)
    assert terminal_ui.wait_for_network(60) is True


def test_wait_for_network_respects_stop(monkeypatch):
    monkeypatch.setattr(terminal_ui, 'network_is_up', lambda *a, **k: False)
    monkeypatch.setattr('time.sleep', lambda s: None)
    assert terminal_ui.wait_for_network(60, should_stop=lambda: True) is False


def test_network_errors_are_recognized():
    for text in ('unknown error: net::ERR_NAME_NOT_RESOLVED', 'Connection error.',
                 "HTTPSConnectionPool(host='api.hh.ru'): Max retries exceeded",
                 'NameResolutionError'):
        assert terminal_ui.is_network_error(text), text
    assert not terminal_ui.is_network_error('element click intercepted')


def test_ai_waits_for_network_and_retries_once(monkeypatch):
    """Все ИИ отказали, сети нет — ждём связь и повторяем запрос один раз."""
    from ai_assistant import AIAssistant
    states = iter([False, True])  # первая проверка: сети нет; потом вернулась
    monkeypatch.setattr(terminal_ui, 'network_is_up', lambda *a, **k: next(states, True))
    monkeypatch.setattr('time.sleep', lambda s: None)

    answers = iter([None, 'письмо после возвращения сети'])
    assistant = AIAssistant.__new__(AIAssistant)
    assistant.enabled = True
    assistant.ai_config = {'cli_providers': []}
    assistant._call_llm_api = lambda prompt, system_prompt=None: next(answers)

    assert assistant._call_llm('p') == 'письмо после возвращения сети'


@pytest.mark.parametrize('error', ['ERR_INTERNET_DISCONNECTED', 'ERR_SSL_PROTOCOL_ERROR'])
def test_apply_network_failure_is_reported_as_network(monkeypatch, error):
    """Сетевой сбой при отклике — отдельный исход: цикл дождётся сети и повторит."""
    import hh_selenium as m
    bot = m.HHSeleniumBot.__new__(m.HHSeleniumBot)
    bot.check_interactive_controls = lambda: None
    bot.stop_requested = False

    class Driver:
        def get(self, url):
            raise Exception('unknown error: net::' + error)
    bot.driver = Driver()
    bot.maybe_bump_resume = lambda: None
    success, message = bot.apply_to_vacancy('https://hh.ru/vacancy/1', 'Вакансия')
    assert success is False and message == m.NETWORK_MESSAGE

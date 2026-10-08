import builtins
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import rejection_analyzer as module


@pytest.fixture
def analyzer(tmp_path, monkeypatch):
    monkeypatch.setattr(module.sys, 'stdin', SimpleNamespace(isatty=lambda: True))
    instance = Mock()
    instance._user_closed = False
    instance.headless = True
    instance.is_driver_alive.return_value = True
    instance.close.side_effect = lambda: setattr(instance.is_driver_alive, 'return_value', False)
    instance.find_chat_message_input.return_value = None
    instance._chat_state_path.return_value = str(tmp_path / 'chat_actions.json')
    instance.messenger_summary = {
        'blocked': True, 'reason': 'Не найден заголовок',
        'recovery_hint': 'Проверьте название вакансии в открытом чате',
    }
    instance.run_chat_analysis.return_value = {'status': 'messenger_blocked',
                                              'messenger': instance.messenger_summary}
    return instance


def test_noninteractive_run_prints_steps_without_waiting(analyzer, monkeypatch, capsys):
    monkeypatch.setattr(module.sys, 'stdin', SimpleNamespace(isatty=lambda: False))
    ask = Mock(side_effect=AssertionError('Must not read unattended input'))
    monkeypatch.setattr(builtins, 'input', ask)
    result = module.run_chat_analysis_with_recovery(analyzer, limit=0, fetch_live=True)
    assert result['status'] == 'messenger_blocked'
    output = capsys.readouterr().out
    assert 'Что делать' in output and '--show-browser' in output
    assert 'chat_followups.md' in output
    analyzer.run_chat_analysis.assert_called_once_with(limit=0, fetch_live=True)
    ask.assert_not_called()


def test_stop_choice_does_not_retry_or_close_browser(analyzer, monkeypatch, capsys):
    monkeypatch.setattr(builtins, 'input', lambda _: '0')
    result = module.run_chat_analysis_with_recovery(analyzer)
    assert result['status'] == 'messenger_blocked'
    analyzer.run_chat_analysis.assert_called_once()
    analyzer.close.assert_not_called()
    assert '[1]' in capsys.readouterr().out


def test_visible_retry_waits_for_user_and_resumes_same_analyzer(analyzer, monkeypatch):
    analyzer.run_chat_analysis.side_effect = [analyzer.run_chat_analysis.return_value,
                                             {'status': 'no_chats_found'}]
    answers = iter(['1', ''])
    monkeypatch.setattr(builtins, 'input', lambda _: next(answers))
    result = module.run_chat_analysis_with_recovery(analyzer, auto_apply=True)
    assert result['status'] == 'no_chats_found'
    assert analyzer.headless is False
    assert analyzer._recovery_show_browser is True
    analyzer.close.assert_called_once()
    analyzer._init_driver.assert_called_once()
    analyzer.goto.assert_called_once_with('https://hh.ru/chat')
    assert analyzer.run_chat_analysis.call_count == 2


def test_headed_retry_preserves_browser_and_manual_draft(analyzer, monkeypatch):
    analyzer.headless = False
    analyzer.find_chat_message_input.return_value = Mock(text='Draft')
    analyzer.run_chat_analysis.side_effect = [analyzer.run_chat_analysis.return_value,
                                             {'status': 'no_chats_found'}]
    answers = iter(['1', ''])
    monkeypatch.setattr(builtins, 'input', lambda _: next(answers))
    module.run_chat_analysis_with_recovery(analyzer)
    analyzer.close.assert_not_called()
    analyzer._init_driver.assert_not_called()
    analyzer.goto.assert_not_called()


def test_headless_draft_is_not_lost_on_retry(analyzer, monkeypatch, capsys):
    analyzer.find_chat_message_input.return_value = Mock()
    analyzer.find_chat_message_input.return_value.get_attribute.return_value = 'Draft'
    answers = iter(['1', '0'])
    monkeypatch.setattr(builtins, 'input', lambda _: next(answers))
    module.run_chat_analysis_with_recovery(analyzer)
    analyzer.close.assert_not_called()
    analyzer.run_chat_analysis.assert_called_once()
    assert 'черновик' in capsys.readouterr().out


def test_report_choice_returns_to_recovery_menu(analyzer, tmp_path, monkeypatch):
    report = tmp_path / 'chat_followups.md'
    report.write_text('# Test', encoding='utf-8')
    opener = Mock()
    monkeypatch.setattr(module.os, 'startfile', opener, raising=False)
    answers = iter(['2', '0'])
    monkeypatch.setattr(builtins, 'input', lambda _: next(answers))
    module.run_chat_analysis_with_recovery(analyzer)
    opener.assert_called_once_with(str(report))
    analyzer.run_chat_analysis.assert_called_once()


@pytest.mark.parametrize('error', [EOFError, KeyboardInterrupt, OSError])
def test_closed_input_stops_safely(analyzer, monkeypatch, error):
    monkeypatch.setattr(builtins, 'input', Mock(side_effect=error))
    result = module.run_chat_analysis_with_recovery(analyzer)
    assert result['status'] == 'messenger_blocked'
    analyzer.run_chat_analysis.assert_called_once()


def test_user_closed_browser_does_not_offer_recovery(analyzer, monkeypatch):
    analyzer.run_chat_analysis.return_value = {'status': 'user_closed'}
    ask = Mock(side_effect=AssertionError('No prompt after explicit close'))
    monkeypatch.setattr(builtins, 'input', ask)
    assert module.run_chat_analysis_with_recovery(analyzer)['status'] == 'user_closed'
    ask.assert_not_called()

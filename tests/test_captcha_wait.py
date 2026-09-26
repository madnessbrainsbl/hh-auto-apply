"""Капчу решает человек: бот ждёт его, а не выбрасывает вакансию.

Браузер не запускаем — драйвер и страница подменены заглушками.
"""
from unittest.mock import MagicMock

import hh_selenium
from hh_selenium import HHSeleniumBot


def make_bot(headless=False, pages=()):
    bot = HHSeleniumBot.__new__(HHSeleniumBot)
    bot.headless = headless
    bot.driver = MagicMock()
    bot.stop_requested = False
    bot.check_interactive_controls = MagicMock(return_value=None)
    bot.get_visible_page_text = MagicMock(side_effect=list(pages))
    return bot


def no_sleep(monkeypatch):
    monkeypatch.setattr(hh_selenium.time, 'sleep', lambda s: None)


def test_headless_returns_false_and_hints_once(monkeypatch, caplog):
    bot = make_bot(headless=True)
    caplog.set_level('INFO')
    assert bot.wait_for_human_captcha() is False
    assert bot.wait_for_human_captcha() is False
    assert sum('видимым окном' in r.message for r in caplog.records) == 1
    bot.get_visible_page_text.assert_not_called()


def test_returns_true_when_captcha_gone(monkeypatch):
    no_sleep(monkeypatch)
    bot = make_bot(pages=['подтвердите, что вы не робот', 'капча', 'вакансия'])
    assert bot.wait_for_human_captcha() is True
    assert bot.get_visible_page_text.call_count == 3


def test_stop_request_aborts_wait(monkeypatch):
    no_sleep(monkeypatch)
    bot = make_bot(pages=['captcha'] * 10)
    bot.check_interactive_controls.side_effect = [None, 'stop']
    assert bot.wait_for_human_captcha() is False


def test_timeout_returns_false(monkeypatch):
    clock = iter(range(0, 1000, 100))
    monkeypatch.setattr(hh_selenium.time, 'monotonic', lambda: next(clock))
    no_sleep(monkeypatch)
    bot = make_bot(pages=['captcha'] * 10)
    assert bot.wait_for_human_captcha(timeout_seconds=180) is False


def make_loop_bot(apply_results, solved):
    bot = HHSeleniumBot.__new__(HHSeleniumBot)
    bot.config = {'skip_applied': True, 'skip_with_tests': True}
    bot.applied_today = 0
    bot.skipped = 0
    bot.errors = 0
    bot.stop_requested = False
    bot.applied_vacancies = {}
    bot.delay_between_vacancies = (0, 0)
    bot.check_interactive_controls = MagicMock(return_value=None)
    bot.employer_is_blocked = MagicMock(return_value=False)
    bot.should_skip_known_vacancy = MagicMock(return_value=False)
    bot.is_api_vacancy_suitable = MagicMock(return_value=(True, ''))
    bot.random_delay = MagicMock()
    bot.save_applied = MagicMock()
    bot.note_apply_success = MagicMock()
    bot.register_apply_failure = MagicMock(return_value=False)
    bot.apply_to_vacancy = MagicMock(side_effect=apply_results)
    bot.wait_for_human_captcha = MagicMock(return_value=solved)
    return bot


VACANCY = {'id': '42', 'name': 'Аналитик данных', 'employer': {'id': '1', 'name': 'Фирма'},
           'alternate_url': 'https://hh.ru/vacancy/42'}


def test_solved_captcha_retries_once_and_counts_success():
    bot = make_loop_bot([(False, 'Требуется капча'), (True, 'Отклик отправлен')], solved=True)
    assert bot.process_api_vacancies([VACANCY]) == 1
    assert bot.apply_to_vacancy.call_count == 2
    assert bot.errors == 0 and bot.skipped == 0
    bot.save_applied.assert_called_once_with('42', 'Аналитик данных', hh_selenium.STATUS_SENT)


def test_unsolved_captcha_is_skip_not_error_and_not_saved():
    bot = make_loop_bot([(False, 'Требуется капча')], solved=False)
    assert bot.process_api_vacancies([VACANCY]) == 0
    assert bot.apply_to_vacancy.call_count == 1
    assert bot.skipped == 1 and bot.errors == 0
    bot.save_applied.assert_not_called()


def test_captcha_again_after_retry_does_not_loop():
    bot = make_loop_bot([(False, 'Требуется капча'), (False, 'Требуется капча')], solved=True)
    bot.process_api_vacancies([VACANCY])
    assert bot.apply_to_vacancy.call_count == 2
    assert bot.wait_for_human_captcha.call_count == 1
    assert bot.skipped == 1 and bot.errors == 0
    bot.save_applied.assert_not_called()

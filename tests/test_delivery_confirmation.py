import os
from unittest.mock import Mock

import pytest

import hh_selenium
from hh_selenium import HHSeleniumBot, APPLY_NOT_CONFIRMED_MARKER


def confirmation_bot(monkeypatch, text='', states=('ready',)):
    bot = HHSeleniumBot.__new__(HHSeleniumBot)
    bot.driver = Mock()
    bot.driver.current_url = 'https://hh.ru/applicant/vacancy_response?vacancyId=123'
    bot.driver.find_elements.return_value = []
    bot.handle_warning_popups = Mock(return_value=False)
    bot.wait_for_response_state = Mock(side_effect=list(states))
    bot.get_response_blocker_message = Mock(return_value=None)
    bot.find_response_modal = Mock(return_value=None)
    bot.get_visible_page_text = lambda: text
    bot.application_ui_problem = Mock(side_effect=lambda code, detail: f'[{code}] {detail}')
    bot.submit_open_response_modal = Mock()
    bot.click_lowest_visible_apply_button = Mock()
    bot.build_success_message = lambda *_: 'confirmed'
    monkeypatch.setattr(hh_selenium.time, 'sleep', lambda _: None)
    return bot


@pytest.mark.parametrize('text', [
    'ваш отклик будет отправлен после заполнения формы',
    'вы откликнулись на похожую вакансию',
    'сопроводительное письмо: отклик отправлен',
])
def test_page_copy_does_not_confirm_delivery(monkeypatch, text):
    bot = confirmation_bot(monkeypatch, text, ['ready'] * 4)
    ok, message = bot.confirm_response_submission('', False, 0, modal_submitted=True)
    assert not ok and APPLY_NOT_CONFIRMED_MARKER in message
    bot.submit_open_response_modal.assert_not_called()
    bot.click_lowest_visible_apply_button.assert_not_called()


def test_confirmation_waits_once_per_attempt(monkeypatch):
    bot = confirmation_bot(monkeypatch, states=['ready'] * 4)
    bot.confirm_response_submission('', False, 0, modal_submitted=True)
    assert bot.wait_for_response_state.call_count == hh_selenium.APPLY_CONFIRM_ATTEMPTS


def test_generic_control_copy_is_not_success(monkeypatch):
    bot = confirmation_bot(monkeypatch)
    bot.is_response_limit_reached = lambda: False
    bot.get_visible_response_state = lambda: {'controls': [{'text': 'Ваш отклик будет отправлен'}]}
    bot.get_response_control_texts = lambda: []
    assert bot.detect_response_state() == 'unknown'


def test_open_response_form_does_not_inherit_background_success(monkeypatch):
    bot = confirmation_bot(monkeypatch)
    bot.is_response_limit_reached = lambda: False
    bot.get_visible_response_state = lambda: {
        'hasPendingForm': True,
        'controls': [{'text': 'Чат'}, {'text': 'Откликнуться другим резюме'}],
    }
    bot.get_response_control_texts = Mock(return_value=['Вы откликнулись'])
    assert bot.detect_response_state() == 'ready'
    bot.get_response_control_texts.assert_not_called()


@pytest.mark.parametrize('visible', [False, True])
def test_captcha_requires_visible_challenge_controls(monkeypatch, visible):
    bot = confirmation_bot(monkeypatch, 'подтвердите, что вы не робот')
    control = Mock()
    control.is_displayed.return_value = visible
    bot.driver.find_elements.return_value = [control]
    assert bot.page_has_captcha() is visible


def test_captcha_words_without_controls_are_not_a_challenge(monkeypatch):
    bot = confirmation_bot(monkeypatch, 'CAPTCHA: подтвердите, что вы не робот; защита веб-приложений')
    assert not bot.page_has_captcha()


@pytest.mark.parametrize('state', ['blocked', 'success'])
def test_captcha_after_dispatch_is_pending_not_retryable(monkeypatch, state):
    bot = confirmation_bot(monkeypatch, states=[state])
    bot.wait_for_human_captcha = Mock(return_value=False)
    bot.get_response_blocker_message.return_value = 'Требуется капча'
    ok, message = bot.confirm_response_submission('', False, 0, modal_submitted=True)
    assert not ok and APPLY_NOT_CONFIRMED_MARKER in message
    assert '[captcha_required]' in message
    bot.submit_open_response_modal.assert_not_called()
    bot.click_lowest_visible_apply_button.assert_not_called()
    bot.wait_for_human_captcha.assert_called_once_with(allow_browser_restart=False)


@pytest.mark.parametrize('final_state,confirmed', [('success', True), ('ready', False)])
def test_post_dispatch_captcha_rechecks_delivery_without_resending(monkeypatch, final_state, confirmed):
    bot = confirmation_bot(monkeypatch, states=['blocked', final_state])
    bot.wait_for_human_captcha = Mock(return_value=True)
    bot.get_response_blocker_message.side_effect = ['Требуется капча', None]
    ok, message = bot.confirm_response_submission('', True, 0, modal_submitted=True)
    assert ok is confirmed
    if not confirmed:
        assert APPLY_NOT_CONFIRMED_MARKER in message
    bot.wait_for_human_captcha.assert_called_once_with(allow_browser_restart=False)
    bot.submit_open_response_modal.assert_not_called()
    bot.click_lowest_visible_apply_button.assert_not_called()
    bot.driver.get.assert_not_called()
    bot.driver.quit.assert_not_called()
    assert bot.wait_for_response_state.call_args.kwargs == {'allow_warning_dispatch': False}


def test_persistent_captcha_is_not_solved_again_after_dispatch(monkeypatch):
    bot = confirmation_bot(monkeypatch, states=['blocked', 'blocked'])
    bot.wait_for_human_captcha = Mock(return_value=True)
    bot.get_response_blocker_message.return_value = 'Требуется капча'
    ok, message = bot.confirm_response_submission('', False, 0, modal_submitted=True)
    assert not ok and '[captcha_required]' in message
    bot.wait_for_human_captcha.assert_called_once_with(allow_browser_restart=False)


@pytest.mark.parametrize('blocker', ['Лимит откликов', 'Ошибка HH после клика'])
def test_solved_captcha_does_not_hide_actual_hh_blocker(monkeypatch, blocker):
    bot = confirmation_bot(monkeypatch, states=['blocked', 'blocked'])
    bot.wait_for_human_captcha = Mock(return_value=True)
    bot.get_response_blocker_message.side_effect = ['Требуется капча', blocker]
    ok, message = bot.confirm_response_submission('', False, 0, modal_submitted=True)
    assert not ok and blocker in message and '[captcha_required]' not in message
    bot.submit_open_response_modal.assert_not_called()


def test_captcha_from_modal_dispatch_uses_same_recovery(monkeypatch):
    bot = confirmation_bot(monkeypatch, states=['ready', 'success'])
    bot.find_response_modal.return_value = Mock()
    bot.submit_open_response_modal.return_value = (True, True, 0, 'Требуется капча')
    bot.wait_for_human_captcha = Mock(return_value=True)
    assert bot.confirm_response_submission('', False, 0) == (True, 'confirmed')
    bot.submit_open_response_modal.assert_called_once()
    bot.wait_for_human_captcha.assert_called_once_with(allow_browser_restart=False)
    bot.click_lowest_visible_apply_button.assert_not_called()


def test_wait_ends_on_visible_blocker_without_spending_timeout(monkeypatch):
    bot = confirmation_bot(monkeypatch)
    bot.handle_warning_popups.return_value = False
    bot.detect_response_state = Mock(return_value='ready')
    bot.get_response_blocker_message.return_value = 'Требуется капча'
    monkeypatch.setattr(hh_selenium.time, 'sleep', Mock())
    result = HHSeleniumBot.wait_for_response_state(bot)
    assert result == 'blocked'
    hh_selenium.time.sleep.assert_not_called()


def test_post_captcha_observation_never_dispatches_warning(monkeypatch):
    bot = confirmation_bot(monkeypatch)
    bot.detect_response_state = Mock(return_value='success')
    assert HHSeleniumBot.wait_for_response_state(bot, allow_warning_dispatch=False) == 'success'
    bot.handle_warning_popups.assert_not_called()


@pytest.mark.parametrize('error,dispatched', [
    (None, True),
    (RuntimeError('lost driver response'), True),
    (hh_selenium.ElementClickInterceptedException(), False),
    (hh_selenium.ElementNotInteractableException(), False),
    (hh_selenium.StaleElementReferenceException(), False),
])
def test_submit_command_never_falls_back_after_unknown_result(monkeypatch, error, dispatched):
    bot = confirmation_bot(monkeypatch)
    button = Mock()
    button.click.side_effect = error
    bot.is_disabled_element = lambda _: False
    bot.click_viewport_coordinates = Mock()
    assert bot.dispatch_response_button(button) is dispatched
    assert bot._response_submit_dispatched is dispatched
    button.click.assert_called_once()
    bot.click_viewport_coordinates.assert_not_called()


def test_post_dispatch_captcha_stops_without_claiming_hh_limit(monkeypatch):
    bot = confirmation_bot(monkeypatch)
    bot.stop_requested = False
    assert bot.register_apply_failure(APPLY_NOT_CONFIRMED_MARKER + ' [captcha_required]')
    assert bot.stop_requested
    assert not getattr(bot, 'response_limit_reached', False)


def test_cache_run_keeps_actual_captcha_stop_reason(monkeypatch, caplog):
    bot = confirmation_bot(monkeypatch)
    bot.stop_requested = False
    bot.register_apply_failure(APPLY_NOT_CONFIRMED_MARKER + ' [captcha_required]')
    bot.init_driver = lambda: True
    bot.check_login = lambda: True
    bot.select_primary_resume_on_startup = lambda: None
    bot.maybe_bump_resume = lambda: None
    bot.merge_resume_recommendations = lambda: None
    bot.close_driver = Mock()
    caplog.clear()
    with caplog.at_level('INFO'):
        bot.run_api_cache()
    assert 'капч' in caplog.text.lower()
    assert 'по запросу пользователя' not in caplog.text
    bot.close_driver.assert_called_once()


def test_user_stop_is_not_reported_as_captcha(monkeypatch, caplog):
    bot = confirmation_bot(monkeypatch)
    with caplog.at_level('INFO'):
        bot.log_stop_reason()
    assert 'по запросу пользователя' in caplog.text
    assert 'капч' not in caplog.text.lower()


def test_keyboard_dispatch_does_not_wait_for_modal_or_send_twice(monkeypatch):
    bot = confirmation_bot(monkeypatch)
    button = Mock()
    button.send_keys.side_effect = RuntimeError('lost driver response')
    bot.driver.find_elements.return_value = [button]
    bot.is_disabled_element = lambda _: False
    assert bot.submit_popup_by_keyboard()
    button.send_keys.assert_called_once()


def test_processing_saves_pending_captcha_and_stops_before_next_vacancy(monkeypatch):
    bot = confirmation_bot(monkeypatch)
    bot.config = {'skip_applied': False}
    bot.applied_vacancies = {}
    bot.applied_today = bot.errors = bot.skipped = 0
    bot.stop_requested = False
    bot.check_interactive_controls = lambda: None
    bot.local_application_limit_reached = lambda: False
    bot.employer_is_blocked = lambda *_: False
    bot.is_api_vacancy_suitable = lambda _: (True, '')
    bot.apply_to_vacancy = Mock(return_value=(False, APPLY_NOT_CONFIRMED_MARKER + ' [captcha_required]'))
    bot.wait_for_human_captcha = Mock()
    bot.note_apply_success = Mock()
    bot.save_applied = Mock()
    bot.random_delay = Mock()
    vacancies = [{'id': str(i), 'name': 'AppSec', 'employer': {},
                  'alternate_url': f'https://hh.ru/vacancy/{i}'} for i in (123, 124)]
    assert bot.process_api_vacancies(vacancies) == 0
    bot.save_applied.assert_called_once_with('123', 'AppSec', hh_selenium.STATUS_PENDING_CONFIRMATION)
    bot.apply_to_vacancy.assert_called_once()
    bot.wait_for_human_captcha.assert_not_called()
    bot.note_apply_success.assert_not_called()
    assert bot.applied_today == 0 and bot.stop_requested


def test_warning_confirmation_is_dispatched_only_once(monkeypatch):
    bot = confirmation_bot(monkeypatch, 'в другой стране')
    target = Mock()
    target.click.side_effect = RuntimeError('lost driver response')
    bot.driver.execute_script.return_value = target
    bot.is_disabled_element = lambda _: False
    assert HHSeleniumBot.handle_warning_popups(bot)
    assert not HHSeleniumBot.handle_warning_popups(bot)
    target.click.assert_called_once()
    bot.driver.execute_script.assert_called_once()


def test_warning_dispatch_in_wait_is_not_followed_by_submit(monkeypatch):
    bot = confirmation_bot(monkeypatch, states=['ready'] * 2)
    bot._response_submit_dispatched = True
    ok, message = bot.confirm_response_submission('', False, 0, modal_submitted=False)
    assert not ok and APPLY_NOT_CONFIRMED_MARKER in message
    bot.submit_open_response_modal.assert_not_called()
    bot.click_lowest_visible_apply_button.assert_not_called()


@pytest.mark.skipif(os.environ.get('HH_BROWSER_SMOKE') != '1', reason='Explicit local Chrome check')
def test_delivery_signals_in_local_chrome(tmp_path, monkeypatch):
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    page = tmp_path / 'response.html'
    page.write_text('''<html><meta charset="utf-8"><body>
      <div data-qa="vacancy-description">Ваш отклик. Подтвердите, что вы не робот. CAPTCHA.</div>
      <div data-qa="vacancy-serp__vacancy"><button>Отклик отправлен</button></div>
      <div data-qa="vacancy-response-popup" role="dialog">
        <button data-qa="vacancy-response-link-top" id="response">Откликнуться</button>
        <button data-qa="vacancy-response-submit-popup" id="submit">Откликнуться</button>
      </div>
      <div role="dialog" id="challenge" hidden><h2>Пройдите капчу</h2>
        Чтобы подтвердить, что вы не робот, введите текст с картинки:
        <input data-qa="account-captcha-input"></div>
      </body></html>''', encoding='utf-8')
    options = webdriver.ChromeOptions()
    options.add_argument('--headless=new')
    options.add_argument(f'--user-data-dir={tmp_path / "chrome"}')
    driver = webdriver.Chrome(options=options)
    try:
        driver.get(page.as_uri())
        bot = HHSeleniumBot.__new__(HHSeleniumBot)
        bot.driver = driver
        assert bot.detect_response_state() == 'ready'
        assert bot.get_response_blocker_message() is None
        driver.execute_script("document.getElementById('challenge').hidden=false")
        assert bot.get_response_blocker_message() == 'Требуется капча'
        bot.application_ui_problem('captcha_required', 'Synthetic visible challenge')
        import json
        from pathlib import Path
        report = next((Path(hh_selenium.SCRIPT_DIR) / '.apply_diagnostics').glob('*captcha_required.json'))
        nodes = json.loads(report.read_text(encoding='utf-8'))['dom']['nodes']
        assert any(n['qa'] == 'account-captcha-input' for n in nodes)
        assert not any(n['qa'] == 'vacancy-response-link-top' for n in nodes)
        driver.execute_script("document.getElementById('challenge').hidden=true")
        driver.execute_script("document.getElementById('response').textContent='Вы откликнулись'")
        assert bot.detect_response_state() == 'ready'
        driver.execute_script("document.getElementById('submit').hidden=true")
        assert bot.detect_response_state() == 'success'
        assert driver.find_element(By.ID, 'response').text == 'Вы откликнулись'
    finally:
        driver.quit()

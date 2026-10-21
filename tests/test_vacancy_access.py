from unittest.mock import MagicMock
import os

import pytest

import hh_selenium


def test_restricted_vacancy_does_not_relogin_stop_or_dispatch(monkeypatch):
    bot = hh_selenium.HHSeleniumBot.__new__(hh_selenium.HHSeleniumBot)
    bot.stop_requested = False
    bot.driver = MagicMock()
    bot.driver.current_url = 'https://hh.ru/vacancy/138611522'
    form = MagicMock(tag_name='form')
    form.is_displayed.return_value = True
    bot.driver.find_elements.return_value = [form]
    bot.driver.execute_script.return_value = True
    for name, result in [('check_interactive_controls', None), ('maybe_bump_resume', None),
                         ('random_delay', None), ('check_login', True)]:
        setattr(bot, name, MagicMock(return_value=result))
    bot.login = MagicMock()
    bot.save_applied = MagicMock()
    bot.dispatch_response_button = MagicMock()
    monkeypatch.setattr('terminal_ui.is_hh_logged_in', lambda _: True)
    wait = MagicMock()
    wait.until.side_effect = hh_selenium.TimeoutException()
    monkeypatch.setattr(hh_selenium, 'WebDriverWait', lambda *a, **kw: wait)
    for _ in range(3):
        sent, reason = bot.apply_to_vacancy('https://hh.ru/vacancy/138611522', 'Job')
        assert not sent and 'недоступна' in reason.lower()
        assert not bot.stop_requested
    bot.check_login.assert_not_called()
    bot.login.assert_not_called()
    bot.save_applied.assert_not_called()
    bot.dispatch_response_button.assert_not_called()


def test_access_notice_without_authenticated_account_is_not_classified_as_restricted(monkeypatch):
    bot = hh_selenium.HHSeleniumBot.__new__(hh_selenium.HHSeleniumBot)
    bot.driver = MagicMock()
    bot.driver.find_elements.return_value = [MagicMock(tag_name='form')]
    bot.driver.execute_script.return_value = True
    monkeypatch.setattr('terminal_ui.is_hh_logged_in', lambda _: False)
    assert bot.vacancy_access_restricted() is False


@pytest.mark.skipif(os.environ.get('HH_BROWSER_SMOKE') != '1', reason='Run explicitly with HH_BROWSER_SMOKE=1')
def test_real_browser_access_notice_excludes_hidden_and_vacancy_description(tmp_path, monkeypatch):
    from urllib.parse import quote
    from selenium import webdriver
    options = webdriver.ChromeOptions()
    options.add_argument('--headless=new')
    options.add_argument('--user-data-dir=' + str(tmp_path / 'chrome'))
    driver = webdriver.Chrome(options=options)
    monkeypatch.setattr('terminal_ui.is_hh_logged_in', lambda _: True)
    bot = hh_selenium.HHSeleniumBot.__new__(hh_selenium.HHSeleniumBot)
    bot.driver = driver
    try:
        for attrs, expected in [('', True), ('data-qa="vacancy-description"', False),
                                ('style="display:none"', False)]:
            html = ('<form data-qa="account-login-form"><input></form>'
                    '<p ' + attrs + '>Вам недоступна <span>эта вакансия.</span> Войдите как пользователь.</p>')
            driver.get('data:text/html;charset=utf-8,' + quote(html))
            assert bot.vacancy_access_restricted() is expected
    finally:
        driver.quit()

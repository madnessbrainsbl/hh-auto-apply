"""Вход не должен закрываться во время выбора профиля или ввода кода."""
from unittest.mock import Mock

import pytest
from selenium.common.exceptions import ElementClickInterceptedException, NoSuchWindowException

import terminal_ui
from hh_selenium import HHSeleniumBot
from rejection_analyzer import RejectionAnalyzer
from resume_updater import HHResumeUpdater


class LoginDriver:
    def __init__(self, url='https://hh.ru/account/login?role=applicant'):
        self.current_url = url
        self.links = []
        self.login_buttons = []
        self.applicants = []
        self.submit = Mock()
        self.get = Mock()

    def find_elements(self, _by, selector):
        if selector == '[data-qa="login"]':
            return self.login_buttons
        if 'account-type-card-APPLICANT' in selector:
            return self.applicants
        if selector == '[data-qa="submit-button"]':
            return [self.submit]
        return self.links

    def sign_in(self):
        self.current_url = 'https://krasnoyarsk.hh.ru/applicant/resumes'
        self.applicants = []
        link = Mock()
        link.get_attribute.side_effect = lambda attr: (
            'https://krasnoyarsk.hh.ru/applicant/resumes' if attr == 'href' else ''
        )
        self.links = [link]


@pytest.mark.parametrize('url', [
    'about:blank',
    'chrome-error://chromewebdata/',
    'https://hh.ru/',
    'https://hh.ru/account/login?role=applicant',
    'https://krasnoyarsk.hh.ru/account/signup',
    'https://example.com/applicant/resumes',
])
def test_missing_login_button_does_not_prove_login(url):
    assert terminal_ui.is_hh_logged_in(LoginDriver(url)) is False


def test_login_requires_account_controls():
    driver = LoginDriver()
    driver.sign_in()
    assert terminal_ui.is_hh_logged_in(driver) is True
    driver.login_buttons = [Mock()]
    assert terminal_ui.is_hh_logged_in(driver) is False


def test_login_waits_past_three_minutes_without_reloading(monkeypatch):
    driver = LoginDriver()
    clock = {'elapsed': 0}

    def advance(seconds):
        clock['elapsed'] += seconds
        if clock['elapsed'] >= 240:
            driver.sign_in()

    monkeypatch.setattr('time.sleep', advance)
    assert terminal_ui.wait_for_hh_login(driver) is True
    assert clock['elapsed'] == 240
    driver.get.assert_not_called()


def test_role_selection_opens_applicant_login_only_once(monkeypatch):
    driver = LoginDriver()
    applicant = Mock()
    applicant.is_selected.return_value = False
    driver.applicants = [applicant]
    driver.submit.click.side_effect = driver.sign_in
    monkeypatch.setattr('time.sleep', lambda _seconds: None)

    assert terminal_ui.wait_for_hh_login(driver) is True
    applicant.find_element.return_value.click.assert_called_once()
    driver.submit.click.assert_called_once()
    driver.get.assert_not_called()


def test_closed_window_ends_login_without_crash():
    driver = LoginDriver()
    driver.find_elements = Mock(side_effect=NoSuchWindowException())
    assert terminal_ui.wait_for_hh_login(driver) is False


def test_stop_ends_login_wait():
    assert terminal_ui.wait_for_hh_login(LoginDriver(), should_stop=lambda: True) is False


def test_changing_login_form_does_not_abort_wait(monkeypatch):
    driver = LoginDriver()
    driver.find_elements = Mock(side_effect=ElementClickInterceptedException())

    def finish_login(_seconds):
        del driver.find_elements
        driver.sign_in()

    monkeypatch.setattr('time.sleep', finish_login)
    assert terminal_ui.wait_for_hh_login(driver) is True


def test_headless_does_not_wait_for_impossible_manual_login(monkeypatch):
    driver = LoginDriver()
    wait = Mock()
    monkeypatch.setattr(terminal_ui, 'wait_for_hh_login', wait)
    assert terminal_ui.ensure_hh_login(driver, headless=True) is False
    wait.assert_not_called()


@pytest.mark.parametrize('cls', [RejectionAnalyzer, HHResumeUpdater])
def test_account_modules_wait_for_login_before_reading_pages(cls, monkeypatch):
    module = __import__(cls.__module__)
    component = cls.__new__(cls)
    component.driver = None
    component.headless = False
    driver = Mock()
    monkeypatch.setattr('selenium.webdriver.Chrome', Mock(return_value=driver))
    monkeypatch.setattr(terminal_ui, 'chrome_service', lambda _path: None)
    wait = Mock(return_value=True)
    monkeypatch.setattr(terminal_ui, 'ensure_hh_login', wait)
    monkeypatch.setattr(terminal_ui, 'ensure_russian_interface', Mock())

    assert component._init_driver() is True
    wait.assert_called_once_with(driver, False, log=module.logger)


def test_selenium_uses_shared_login_check_and_wait(monkeypatch):
    bot = HHSeleniumBot.__new__(HHSeleniumBot)
    bot.driver = LoginDriver('https://hh.ru/')
    assert bot.is_logged_in_current_page() is False
    wait = Mock(return_value=True)
    monkeypatch.setattr(terminal_ui, 'wait_for_hh_login', wait)
    monkeypatch.setattr(terminal_ui, 'ensure_russian_interface', Mock())
    assert bot.wait_for_login() is True
    assert wait.call_args.args == (bot.driver,)

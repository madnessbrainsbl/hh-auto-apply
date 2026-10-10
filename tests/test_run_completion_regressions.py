import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from selenium import webdriver
from selenium.webdriver.common.by import By

import test as menu
from hh_selenium import HHSeleniumBot


@pytest.fixture(scope='module')
def local_browser(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp('completion-browser')
    options = webdriver.ChromeOptions()
    options.add_argument('--headless=new')
    options.add_argument(f'--user-data-dir={tmp_path / "chrome"}')
    driver = webdriver.Chrome(options=options)
    try:
        yield driver
    finally:
        driver.quit()


def open_form(driver, tmp_path, html):
    page = tmp_path / 'form.html'
    page.write_text('<meta charset="utf-8">' + html, encoding='utf-8')
    driver.get(page.as_uri())
    bot = HHSeleniumBot.__new__(HHSeleniumBot)
    bot.driver = driver
    bot.config = {'question_answers': {}}
    bot.click_element_with_mouse = lambda element: element.click() or True
    return bot


def test_checkbox_batch_mismatch_asks_for_available_options(local_browser, tmp_path):
    bot = open_form(local_browser, tmp_path, '''
      <div id="q">Оцените уровень работы с Linux:
        <label><input type="checkbox" value="0">Нет опыта</label>
        <label><input type="checkbox" value="1">Уверенный: работаю в терминале</label>
        <label><input type="checkbox" value="2">Продвинутый: администрирую серверы</label>
      </div>''')
    block = local_browser.find_element(By.ID, 'q')
    bot._batch_answers = {' '.join(block.text.split()): 'Применял Linux для защиты инфраструктуры.'}
    bot.ai_assistant = SimpleNamespace(enabled=True, answer_question=Mock(return_value=1))
    answered, unresolved = bot.answer_single_question(block, block.text, {}, set())
    assert unresolved is None and answered == 1
    assert [e.get_attribute('value') for e in local_browser.find_elements(By.CSS_SELECTOR, ':checked')] == ['1']
    assert bot.ai_assistant.answer_question.call_args.kwargs['options'] == [
        'Нет опыта', 'Уверенный: работаю в терминале', 'Продвинутый: администрирую серверы']


@pytest.mark.parametrize('checked', [True, False])
def test_choice_rerender_requires_actual_checked_state(local_browser, tmp_path, checked):
    bot = open_form(local_browser, tmp_path, f'''
      <label><input id="choice" type="checkbox" value="yes"
       onchange="const replacement=this.cloneNode(); replacement.checked={str(checked).lower()};
         this.replaceWith(replacement)">Да</label>''')
    option = local_browser.find_element(By.ID, 'choice')
    assert bot.click_choice(option) is checked


def test_resume_selection_does_not_click_employment_format(local_browser, tmp_path, monkeypatch):
    monkeypatch.setattr('config_manager.get_active_resume', lambda: ('target', 'Application Security Engineer'))
    bot = open_form(local_browser, tmp_path, '''
      <div id="modal"><div data-qa="cell-text-content">На месте работодателя</div>
        <div data-qa="resume-selector"><span data-qa="cell-text-content">Application Security Engineer</span></div>
      </div>''')
    bot.click_element_with_mouse = Mock(return_value=True)
    assert bot.ensure_target_resume_selected(local_browser.find_element(By.ID, 'modal')) == (True, None)
    bot.click_element_with_mouse.assert_not_called()


def test_resume_switch_is_not_success_until_form_changes(local_browser, tmp_path, monkeypatch):
    monkeypatch.setattr('config_manager.get_active_resume', lambda: ('target', 'Application Security Engineer'))
    bot = open_form(local_browser, tmp_path, '''
      <div id="modal"><span data-qa="resume-title">Фотограф</span></div>
      <div data-qa="bottom-sheet-content"><label data-qa="cell">Application Security Engineer</label></div>''')
    ok, error = bot.ensure_target_resume_selected(local_browser.find_element(By.ID, 'modal'))
    assert not ok and error


def test_resume_switch_waits_for_selected_title(local_browser, tmp_path, monkeypatch):
    monkeypatch.setattr('config_manager.get_active_resume', lambda: ('target', 'Application Security Engineer'))
    bot = open_form(local_browser, tmp_path, '''
      <div id="modal"><button data-qa="resume-title" id="title"
       onclick="setTimeout(() => document.getElementById('options').hidden=false, 200)">Фотограф</button></div>
      <div id="options" data-qa="bottom-sheet-content" hidden>
        <label data-qa="cell" onclick="setTimeout(() => {
          document.getElementById('title').textContent='Application Security Engineer';
          document.getElementById('options').hidden=true; }, 200)">Application Security Engineer</label>
      </div>''')
    assert bot.ensure_target_resume_selected(local_browser.find_element(By.ID, 'modal')) == (True, None)
    assert local_browser.find_element(By.ID, 'title').text == 'Application Security Engineer'


def test_resume_selection_waits_for_initial_title(local_browser, tmp_path, monkeypatch):
    monkeypatch.setattr('config_manager.get_active_resume', lambda: ('target', 'Application Security Engineer'))
    bot = open_form(local_browser, tmp_path, '''
      <div id="modal"><div data-qa="cell-text-content">На месте работодателя</div></div>
      <script>setTimeout(() => {
        document.getElementById('modal').innerHTML +=
          '<span data-qa="resume-title">Application Security Engineer</span>';
      }, 400);</script>''')
    bot.click_element_with_mouse = Mock(return_value=True)
    assert bot.ensure_target_resume_selected(local_browser.find_element(By.ID, 'modal')) == (True, None)
    bot.click_element_with_mouse.assert_not_called()


def test_response_modal_prefers_response_form_over_unrelated_popup(local_browser, tmp_path):
    bot = open_form(local_browser, tmp_path, '''
      <div class="promo-popup" id="promo">Другие вакансии</div>
      <div data-qa="vacancy-response-popup" id="response">Отклик на вакансию</div>''')
    assert bot.find_response_modal().get_attribute('id') == 'response'


def test_questionnaire_submit_button_is_not_a_response_modal(local_browser, tmp_path):
    bot = open_form(local_browser, tmp_path, '''
      <h1>Отклик на вакансию</h1><form>
        <label>Расскажите о технических задачах<textarea></textarea></label>
        <button data-qa="vacancy-response-submit-popup">Откликнуться</button>
      </form>''')
    bot.apply_via_response_page = Mock(return_value=(True, True, 1, None))
    bot.ensure_target_resume_selected = Mock()
    bot.click_lowest_visible_apply_button = Mock()

    assert bot.find_response_modal() is None
    assert bot.submit_open_response_modal('Test letter', False) == (True, True, 1, None)
    bot.apply_via_response_page.assert_called_once_with('Test letter', False)
    bot.ensure_target_resume_selected.assert_not_called()
    bot.click_lowest_visible_apply_button.assert_not_called()


def test_resume_selection_recovers_replaced_form(local_browser, tmp_path, monkeypatch):
    monkeypatch.setattr('config_manager.get_active_resume', lambda: ('target', 'Application Security Engineer'))
    bot = open_form(local_browser, tmp_path, '''
      <div id="modal" data-qa="vacancy-response-popup"></div>
      <script>setTimeout(() => {
        const form = document.createElement('div');
        form.setAttribute('data-qa', 'vacancy-response-popup');
        form.innerHTML = '<span data-qa="resume-title">Application Security Engineer</span>';
        document.getElementById('modal').replaceWith(form);
      }, 300);</script>''')
    assert bot.ensure_target_resume_selected(local_browser.find_element(By.ID, 'modal')) == (True, None)


def test_application_ui_diagnostic_is_structural_and_bounded(local_browser, tmp_path, monkeypatch):
    import hh_selenium
    monkeypatch.setattr(hh_selenium, 'SCRIPT_DIR', str(tmp_path))
    bot = open_form(local_browser, tmp_path, '''
      <div data-qa="vacancy-response-popup"><input value="PRIVATE_VALUE">
        <textarea>PRIVATE_LETTER</textarea><button data-qa="vacancy-response-submit-popup">Отправить</button>
      </div>''')
    bot.last_application_meta = {'url': 'https://hh.ru/vacancy/123?token=PRIVATE_TOKEN',
                                 'cover_letter': 'PRIVATE_LETTER'}
    for _ in range(4):
        message = bot.application_ui_problem('resume_not_confirmed', 'Не прочитано резюме')
        assert 'resume_not_confirmed' in message and '.json' in message
    files = list((tmp_path / '.apply_diagnostics').glob('*.json'))
    assert len(files) == 4
    assert len(list((tmp_path / '.apply_diagnostics').glob('*.png'))) == 3
    for path in files:
        text = path.read_text(encoding='utf-8')
        assert 'PRIVATE_' not in text
        row = json.loads(text)
        assert row['vacancy_id'] == '123' and row['code'] == 'resume_not_confirmed'
        assert row['dom']['nodes']
    assert bot._application_ui_errors == {'resume_not_confirmed': 4}


def test_application_ui_diagnostic_failure_does_not_hide_problem(tmp_path, monkeypatch):
    import hh_selenium
    monkeypatch.setattr(hh_selenium, 'SCRIPT_DIR', str(tmp_path))
    bot = HHSeleniumBot.__new__(HHSeleniumBot)
    bot.driver = Mock()
    bot.driver.execute_script.side_effect = RuntimeError('PRIVATE_EXCEPTION')
    message = bot.application_ui_problem('apply_button_missing', 'Кнопка недоступна')
    assert 'apply_button_missing' in message and 'PRIVATE_EXCEPTION' not in message
    assert 'не удалось сохранить' in message


def test_unread_resume_guard_saves_diagnostic_without_clicking(local_browser, tmp_path, monkeypatch):
    import hh_selenium
    monkeypatch.setattr(hh_selenium, 'SCRIPT_DIR', str(tmp_path))
    monkeypatch.setattr('config_manager.get_active_resume', lambda: ('target', 'Application Security Engineer'))
    bot = open_form(local_browser, tmp_path, '''
      <div data-qa="vacancy-response-popup"><div data-qa="cell-text-content">Гибрид</div></div>''')
    bot.click_element_with_mouse = Mock(return_value=True)
    ok, message = bot.ensure_target_resume_selected(bot.find_response_modal())
    assert not ok and '[resume_not_confirmed]' in message
    assert 'не означает, что резюме не выбрано' in message
    assert list((tmp_path / '.apply_diagnostics').glob('*.json'))
    bot.click_element_with_mouse.assert_not_called()


def test_submitted_modal_is_observed_not_submitted_again(monkeypatch):
    bot = HHSeleniumBot.__new__(HHSeleniumBot)
    bot.handle_warning_popups = lambda: False
    bot.wait_for_response_state = lambda: 'unknown'
    bot.get_response_blocker_message = lambda: None
    bot.find_response_modal = lambda: Mock()
    bot.submit_open_response_modal = Mock(return_value=(True, True, 0, None))
    bot.get_visible_page_text = lambda: ''
    monkeypatch.setattr('hh_selenium.time.sleep', lambda _: None)
    assert not bot.confirm_response_submission('test letter', True, 0, modal_submitted=True)[0]
    bot.submit_open_response_modal.assert_not_called()


def test_recent_count_excludes_future_dates():
    future = (datetime.now() + timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')
    assert menu.count_recent_timestamps([future], 24) == 0


def test_menu_count_includes_browser_only_confirmed_sends(tmp_path):
    bot = menu.HHAutoApplicant.__new__(menu.HHAutoApplicant)
    bot.applied_vacancies_file = str(tmp_path / 'shared.json')
    bot.selenium_applied_vacancies_file = str(tmp_path / 'browser.json')
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    (tmp_path / 'shared.json').write_text(json.dumps({'1': now}), encoding='utf-8')
    (tmp_path / 'browser.json').write_text(json.dumps({
        '1': {'date': now, 'status': 'sent'},
        '2': {'date': now, 'status': 'sent'},
        '3': {'date': now, 'status': 'already_applied'},
    }), encoding='utf-8')
    bot.load_applied_vacancies()
    assert bot.applied_today == 2


def test_unknown_submission_is_saved_without_counting_or_repeating(tmp_path):
    bot = HHSeleniumBot.__new__(HHSeleniumBot)
    bot.config = {'skip_applied': False}
    bot.applied_file = str(tmp_path / 'browser.json')
    bot.shared_applied_file = str(tmp_path / 'shared.json')
    bot.api_cache_file = ''
    bot.applied_vacancies = {}
    bot.applied_today = bot.skipped = bot.errors = 0
    bot.check_interactive_controls = lambda: None
    bot.stop_requested = False
    bot.employer_is_blocked = lambda *_: False
    bot.is_api_vacancy_suitable = lambda _: (True, '')
    bot.random_delay = lambda _: None
    bot.delay_between_vacancies = (0, 0)
    bot.register_apply_failure = lambda _: False
    bot.apply_to_vacancy = Mock(return_value=(False, 'Статус отклика не изменился (unknown).'))
    vacancy = {'id': '42', 'name': 'AppSec', 'alternate_url': 'https://hh.ru/vacancy/42', 'employer': {}}
    bot.process_api_vacancies([vacancy, vacancy])
    bot.apply_to_vacancy.assert_called_once()
    assert bot.applied_vacancies['42']['status'] == 'pending_confirmation'
    assert bot.count_sent_today() == 0
    assert not (tmp_path / 'shared.json').exists()


def test_run_summary_separates_new_sends_from_rolling_history(capsys):
    bot = HHSeleniumBot.__new__(HHSeleniumBot)
    bot.sent_this_run = 18
    bot.applied_today = 215
    bot.count_sent_today = lambda: 197
    bot.skipped = 27
    bot.errors = 1
    bot._application_ui_errors = {'resume_not_confirmed': 1}
    bot.print_run_summary()
    output = capsys.readouterr().out
    assert '[resume_not_confirmed]' in output and '.apply_diagnostics' in output
    assert 'за этот запуск: 18' in output
    assert 'за последние 24 часа: 197' in output


@pytest.mark.parametrize('title', [
    'Менеджер по работе с вендорами (Информационная безопасность)',
    'Vendor manager (Security)',
])
def test_vendor_commercial_roles_are_excluded(title):
    bot = HHSeleniumBot.__new__(HHSeleniumBot)
    bot.config = {'keywords_include': [], 'keywords_exclude': []}
    assert not menu.validate_apply_title(title)[0]
    assert not bot.validate_security_title(title)[0]


def test_followup_report_explains_pending_statuses(tmp_path, caplog):
    from rejection_analyzer import RejectionAnalyzer
    bot = RejectionAnalyzer.__new__(RejectionAnalyzer)
    bot._chat_state_path = lambda: str(tmp_path / 'actions.json')
    actions = {str(i): {'status': status} for i, status in enumerate([
        'deferred', 'pending_confirmation', 'external_interview_required', 'sent'])}
    bot._load_chat_actions = lambda: actions
    with caplog.at_level('INFO'):
        bot._write_chat_followups()
    report = (tmp_path / 'chat_followups.md').read_text(encoding='utf-8')
    assert 'Отложено: 1' in report
    assert 'Отправка не подтверждена: 1' in report
    assert 'Внешнее интервью: 1' in report
    assert 'не отправлять повторно' in report
    assert actions['1']['status'] == 'pending_confirmation'

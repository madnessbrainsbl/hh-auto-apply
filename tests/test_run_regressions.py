import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier
from unittest.mock import Mock

import pytest

from ai_assistant import AIAssistant
from hh_selenium import HHSeleniumBot
from terminal_ui import explain_error


def questionnaire_bot(error=False):
    bot = HHSeleniumBot.__new__(HHSeleniumBot)
    bot.driver = Mock()
    bot.get_visible_page_text = lambda: 'ответьте на вопросы работодателя'
    bot.is_response_limit_reached = lambda: False
    bot.page_says_archived = lambda: False
    if error:
        field_error = Mock()
        field_error.text = 'Ответьте на вопросы работодателя'
        bot.driver.find_elements.return_value = [field_error]
    else:
        bot.driver.find_elements.return_value = []
    return bot


def test_questionnaire_heading_is_not_a_submission_error():
    assert questionnaire_bot().get_response_blocker_message() is None
    assert questionnaire_bot(error=True).get_response_blocker_message() == 'Не заполнены вопросы работодателя'


def test_filled_questionnaire_can_confirm_with_heading_present(monkeypatch):
    bot = questionnaire_bot()
    bot.handle_warning_popups = lambda: False
    bot.wait_for_response_state = Mock(side_effect=['ready', 'success'])
    bot.find_response_modal = lambda: None
    bot.build_success_message = lambda *_args: 'sent'
    monkeypatch.setattr('hh_selenium.time.sleep', lambda _seconds: None)
    assert bot.confirm_response_submission('', False, 2, modal_submitted=True) == (True, 'sent')


def test_gemini_access_denial_is_not_repeated_for_each_letter(caplog):
    assistant = AIAssistant.__new__(AIAssistant)
    assistant.enabled = True
    assistant._gemini_client = Mock()
    assistant._gemini_client.generate_content.side_effect = RuntimeError('403 permission denied')
    assistant._openai_client = None
    assistant.ensure_model_resolved = lambda: None
    assistant.gemini_request_options = lambda: {}
    assistant._call_backup_provider = Mock(return_value=None)
    for _ in range(2):
        assert assistant._call_llm_api('generic test prompt') is None
    assert assistant._gemini_client.generate_content.call_count == 1
    assert assistant._call_backup_provider.call_count == 2
    assert 'hh.ru' not in caplog.text


def test_generic_http_error_is_not_misattributed_to_hh():
    assert 'hh.ru' not in explain_error(RuntimeError('403 forbidden'))


def modal_questionnaire_bot():
    bot = questionnaire_bot()
    modal = Mock()
    modal.text = 'Ответьте на вопросы работодателя'
    modal.find_elements.return_value = []
    bot.find_response_modal = lambda **_kwargs: modal
    bot.ensure_target_resume_selected = lambda _modal: (True, None)
    bot.is_cover_letter_required = lambda: False
    bot.unanswered_questions = []
    bot.click_response_submit_button = Mock(return_value=True)
    return bot, modal


def test_late_questionnaire_fields_are_retried(monkeypatch):
    bot, modal = modal_questionnaire_bot()
    bot.answer_employer_questions = Mock(side_effect=[0, 2])
    monkeypatch.setattr('hh_selenium.time.sleep', lambda _seconds: None)
    submitted, _, answered, error = bot.submit_open_response_modal('', True)
    assert submitted and answered == 2 and error is None
    assert bot.answer_employer_questions.call_count == 2
    bot.click_response_submit_button.assert_called_once_with(modal)


def test_unreadable_questionnaire_uses_full_response_page(monkeypatch):
    bot, _ = modal_questionnaire_bot()
    bot.answer_employer_questions = Mock(return_value=0)
    bot.apply_via_response_page = Mock(return_value=(False, True, 0, 'unreadable form'))
    monkeypatch.setattr('hh_selenium.time.sleep', lambda _seconds: None)
    assert bot.submit_open_response_modal('', True) == (False, True, 0, 'unreadable form')
    bot.apply_via_response_page.assert_called_once_with('', True)
    bot.click_response_submit_button.assert_not_called()


def test_actual_question_validation_error_is_not_cleared():
    bot, modal = modal_questionnaire_bot()
    field_error = Mock()
    field_error.text = 'Ответьте на вопросы работодателя'
    bot.driver.find_elements.return_value = [field_error]
    modal.find_elements.return_value = [field_error]
    bot.answer_employer_questions = Mock(return_value=2)
    submitted, _, _, error = bot.submit_open_response_modal('', True)
    assert not submitted and error == 'Не заполнены вопросы работодателя'
    bot.click_response_submit_button.assert_not_called()


@pytest.mark.parametrize('contents', ['{broken', '[]'])
def test_corrupt_shared_history_does_not_erase_local_count(tmp_path, contents):
    bot = HHSeleniumBot.__new__(HHSeleniumBot)
    bot.applied_vacancies = {'1': {'date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'status': 'sent'}}
    history = tmp_path / 'history.json'
    history.write_text(contents, encoding='utf-8')
    bot.shared_applied_file = str(history)
    assert bot.count_sent_today() == 1
    assert history.read_text(encoding='utf-8') == contents


def test_rolling_count_merges_histories_without_duplicates(tmp_path):
    now = datetime.now()
    stamp = now.strftime('%Y-%m-%d %H:%M:%S')
    old = (now - timedelta(hours=25)).strftime('%Y-%m-%d %H:%M:%S')
    bot = HHSeleniumBot.__new__(HHSeleniumBot)
    bot.applied_vacancies = {
        '1': {'date': stamp, 'status': 'sent'},
        '4': {'date': stamp, 'status': 'skipped_test'},
    }
    history = tmp_path / 'applied_vacancies.json'
    history.write_text(json.dumps({'1': stamp, '2': stamp, '3': old, '5': 'bad date'}), encoding='utf-8')
    bot.shared_applied_file = str(history)
    assert bot.count_sent_today() == 2


def test_parallel_gemini_denials_are_not_repeated():
    assistant = AIAssistant.__new__(AIAssistant)
    assistant.enabled = True
    assistant._gemini_client = Mock()
    def denied(*_args, **_kwargs):
        time.sleep(0.05)
        raise RuntimeError('403 permission denied')
    assistant._gemini_client.generate_content.side_effect = denied
    assistant._openai_client = None
    assistant.ensure_model_resolved = lambda: None
    assistant.gemini_request_options = lambda: {}
    assistant._call_backup_provider = Mock(return_value=None)
    barrier = Barrier(3)
    def call(_):
        barrier.wait(timeout=3)
        return assistant._call_step('gemini', 'synthetic prompt', None)
    with ThreadPoolExecutor(max_workers=3) as pool:
        assert list(pool.map(call, range(3))) == [None, None, None]
    assert assistant._gemini_client.generate_content.call_count == 1


def test_parallel_cli_denials_are_not_repeated(monkeypatch, tmp_path):
    import subprocess
    assistant = AIAssistant.__new__(AIAssistant)
    assistant.ai_config = {'cli_providers': ['claude']}
    assistant.find_cli = lambda _name: 'synthetic-cli'
    def failed(*_args, **_kwargs):
        time.sleep(0.05)
        return subprocess.CompletedProcess([], 1, b'', b'not logged in')
    runner = Mock(side_effect=failed)
    monkeypatch.setattr('subprocess.run', runner)
    barrier = Barrier(3)
    def call(_):
        barrier.wait(timeout=3)
        return assistant._call_step('claude', 'synthetic prompt', None)
    with ThreadPoolExecutor(max_workers=3) as pool:
        assert list(pool.map(call, range(3))) == [None, None, None]
    assert runner.call_count == 1


def test_deferred_analysis_does_not_call_failed_ai_twice_or_write_skills():
    assistant = AIAssistant.__new__(AIAssistant)
    assistant.enabled = True
    assistant.candidate_profile = {'skills': []}
    assistant.db = Mock()
    assistant._call_llm = Mock(return_value=None)
    assistant.generate_cover_letter = Mock(return_value='fallback')
    assistant._heuristic_cover_letter = Mock(return_value='synthetic template')
    result = assistant.analyze_chat_rejection('AppSec', 'Synthetic employer', 'AppSec Python')
    assert result['heuristic']
    assistant._call_llm.assert_called_once()
    assistant.generate_cover_letter.assert_not_called()
    assistant.db.record_adaptive_skills.assert_not_called()


def test_browser_continues_past_local_200_until_hh_blocks():
    bot = HHSeleniumBot.__new__(HHSeleniumBot)
    bot.config = {'max_applications': 200, 'skip_applied': True}
    bot.applied_today = 200
    bot.applied_vacancies = {}
    bot.errors = bot.skipped = 0
    bot.response_limit_reached = False
    bot.is_api_vacancy_suitable = lambda _vacancy: (True, '')
    bot.apply_to_vacancy = Mock(return_value=(False, 'Лимит откликов'))
    bot.save_applied = Mock()
    vacancy = {'id': '123', 'name': 'AppSec', 'alternate_url': 'https://hh.ru/vacancy/123', 'employer': {}}
    bot.process_api_vacancies([vacancy])
    bot.apply_to_vacancy.assert_called_once()
    assert bot.response_limit_reached
    bot.save_applied.assert_not_called()


def test_explicit_local_stop_is_respected():
    bot = HHSeleniumBot.__new__(HHSeleniumBot)
    bot.config = {'max_applications': 20, 'stop_at_local_limit': True}
    bot.applied_today = 20
    bot.apply_to_vacancy = Mock()
    bot.process_api_vacancies([{'id': '123'}])
    bot.apply_to_vacancy.assert_not_called()


def test_template_applications_continue_until_server_limit():
    assistant = AIAssistant.__new__(AIAssistant)
    assistant.enabled = True
    assistant.config = {}
    assistant.candidate_profile = {'specialization': 'AppSec', 'skills': ['Python']}
    assistant.db = None
    assistant._call_llm = Mock(return_value=None)
    bot = HHSeleniumBot.__new__(HHSeleniumBot)
    bot.config = {'max_applications': 200, 'skip_applied': True}
    bot.applied_today = 200
    bot.applied_vacancies = {}
    bot.errors = bot.skipped = 0
    bot.response_limit_reached = False
    bot.is_api_vacancy_suitable = lambda _vacancy: (True, '')
    letters = []
    def apply(_url, _name):
        letter = assistant.generate_cover_letter('AppSec', 'Synthetic employer', 'Python')
        assert letter and assistant.last_letter_source == 'template'
        letters.append(letter)
        return (True, 'sent') if len(letters) < 3 else (False, 'Лимит откликов')
    bot.apply_to_vacancy = apply
    bot.save_applied = Mock()
    bot.maybe_email_employer = lambda *_args: None
    bot.delay_between_vacancies = (0, 0)
    bot.random_delay = lambda _delay: None
    vacancies = [{'id': str(i), 'name': 'AppSec', 'alternate_url': f'https://hh.ru/vacancy/{i}',
                  'employer': {}} for i in range(1, 5)]
    assert bot.process_api_vacancies(vacancies) == 2
    assert bot.applied_today == 202 and bot.response_limit_reached
    assert len(letters) == 3 and bot.save_applied.call_count == 2


def test_search_does_not_stop_on_cumulative_errors_or_local_200():
    bot = HHSeleniumBot.__new__(HHSeleniumBot)
    bot.config = {'max_applications': 200, 'max_search_pages': 3}
    bot.applied_today = 205
    bot.response_limit_reached = bot.throttled_stop = bot.stop_requested = False
    bot.errors = 12
    bot.driver = Mock()
    bot.delay_between_actions = (0, 0)
    bot.random_delay = lambda _delay: None
    pages = []
    def process():
        pages.append(1)
        bot.last_search_page_count = 1 if len(pages) == 1 else 0
        return 0
    bot.process_search_page = process
    bot.process_search_url('https://hh.ru/search/vacancy?text=appsec')
    assert len(pages) == 2


def test_parallel_stats_updates_are_serialized():
    from threading import Lock
    assistant = AIAssistant.__new__(AIAssistant)
    assistant._ai_stats = {}
    active = 0
    collided = []
    guard = Lock()
    def save():
        nonlocal active
        with guard:
            active += 1
            if active > 1:
                collided.append(True)
        time.sleep(0.01)
        with guard:
            active -= 1
    assistant._save_stats = save
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda _: assistant._record('gemini', False, 1), range(12)))
    assert collided == []
    assert assistant._ai_stats['gemini']['n'] == 12

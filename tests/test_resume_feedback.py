"""Resume feedback loop with temporary data only; no HH requests or messages."""
import json
import os
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from ai_assistant import AIAssistant, clean_public_text
from db_manager import DatabaseManager
from rejection_analyzer import RejectionAnalyzer
from resume_updater import (HHResumeUpdater, load_resume_revision, resume_revision_path,
                            save_resume_revision)


PROFILE = {'specialization': 'AppSec', 'skills': ['Python', 'Linux', 'Burp Suite'],
           'about': 'Проверяю веб-приложения и API, анализирую доступы.',
           'experience_highlights': [{'company': 'Old Employer', 'what': 'Проверяю API с Burp Suite.'}]}
OLD = 'Проверяю безопасность веб-приложений и API. Анализирую контроль доступа, настройки сервисов и результаты сканирования.'
NEW = 'AppSec: проверяю веб-приложения и API с Burp Suite. Анализирую контроль доступа и конфигурации Linux; оформляю воспроизводимые сценарии для устранения уязвимостей.'
ANALYSIS = {'about_me_recommendation': NEW, 'cover_letter_critique': 'Письмо слишком общее',
            'vacancy_title': 'AppSec', 'company_name': 'Test', 'missing_skills': []}


def ai():
    assistant = AIAssistant({'resume_id': 'test-resume', 'resume_title': 'AppSec',
                            'candidate_profile': PROFILE, 'ai_config': {'enabled': True}})
    assistant._call_llm = MagicMock(return_value=json.dumps(
        {'about': NEW, 'reason': 'Конкретные технические задачи',
         'selection_guidance': ['Основная роль AppSec, а не разработка на другом языке']}))
    return assistant


@pytest.mark.parametrize('about, valid', [(NEW, True), ('Работаю с Terraform. ' + NEW, False),
                                         ('Провёл 800 аудитов. ' + NEW, False), ('', False)])
def test_generation_uses_candidate_facts_not_rejection_requirements(about, valid):
    assistant = ai()
    assistant._call_llm.return_value = json.dumps({'about': about, 'selection_guidance': []})
    result = assistant.improve_resume_about(OLD, [ANALYSIS], 'AppSec')
    assert bool(result) is valid
    prompt, system = assistant._call_llm.call_args.args
    assert 'candidate_facts' in prompt and 'не переориентируй' in system
    validator = assistant._call_llm.call_args.kwargs['validate']
    assert (validator(assistant._call_llm.return_value) is None) is valid


def test_samples_do_not_generate_resume_changes():
    assistant = ai()
    assert assistant.improve_resume_about(OLD, [dict(ANALYSIS, is_sample=True)], 'AppSec') is None
    assistant._call_llm.assert_not_called()


def editor(monkeypatch, saves=True):
    updater = HHResumeUpdater(resume_id='test-resume', headless=True)
    state = {'saved': OLD, 'draft': OLD, 'clicks': 0}
    field = MagicMock()
    field.is_displayed.return_value = True
    field.get_attribute.side_effect = lambda key: state['draft']

    def keys(*args):
        state['draft'] = '' if len(args) == 2 else state['draft'] + args[0]

    field.send_keys.side_effect = keys
    driver = updater.driver = MagicMock()

    def navigate(url):
        driver.current_url = url
        state['draft'] = state['saved']

    driver.get.side_effect = navigate
    driver.find_elements.return_value = [field]
    driver.find_element.return_value = field
    updater.is_driver_alive = lambda: True

    def click(button):
        state['clicks'] += 1
        if saves:
            state['saved'] = state['draft']
        return True

    updater.real_click = click
    monkeypatch.setattr('resume_updater.time.sleep', lambda _: None)
    return updater, state


def test_about_save_backup_and_readback(monkeypatch):
    updater, state = editor(monkeypatch)
    ok, msg = updater.replace_about_section(NEW, OLD, {'profile': PROFILE, 'selection_guidance': ['AppSec']})
    assert ok and updater._about_changed
    row = load_resume_revision('test-resume')
    assert row['status'] == 'verified' and row['before'] == OLD and row['after'] == NEW
    with open(row['backup'], encoding='utf-8') as f:
        assert json.load(f)['before'] == OLD
    assert state['clicks'] == 1
    assert updater.replace_about_section(NEW, NEW, {'profile': PROFILE})[0]
    assert not updater._about_changed and state['clicks'] == 1


def test_unconfirmed_save_is_pending_and_never_retried(monkeypatch):
    updater, state = editor(monkeypatch, saves=False)
    assert not updater.replace_about_section(NEW, OLD, {'profile': PROFILE})[0]
    assert load_resume_revision('test-resume')['status'] == 'pending'
    assert not updater.replace_about_section(NEW, OLD, {'profile': PROFILE})[0]
    assert state['clicks'] == 1
    state['saved'] = state['draft'] = NEW
    assert updater.replace_about_section(NEW, NEW, {'profile': PROFILE})[0]
    assert state['clicks'] == 1 and load_resume_revision('test-resume')['status'] == 'verified'


def test_concurrent_user_edit_is_not_overwritten(monkeypatch):
    updater, state = editor(monkeypatch)
    state['saved'] = 'Моя свежая правка'
    assert not updater.replace_about_section(NEW, OLD, {'profile': PROFILE})[0]
    assert state['clicks'] == 0 and not os.path.exists(resume_revision_path('test-resume'))


def test_manual_about_draft_is_preserved_without_navigation(monkeypatch):
    updater, state = editor(monkeypatch)
    assert updater.read_about_section() == OLD
    updater.driver.get.reset_mock()
    state['draft'] = 'Мой несохранённый черновик'
    assert not updater.replace_about_section(NEW, OLD, {'profile': PROFILE})[0]
    updater.driver.get.assert_not_called()
    assert state['draft'] == 'Мой несохранённый черновик' and state['clicks'] == 0


def test_backup_failure_never_saves(monkeypatch):
    updater, state = editor(monkeypatch)
    monkeypatch.setattr('resume_updater.save_resume_revision', MagicMock(side_effect=OSError('Disk full')))
    assert not updater.replace_about_section(NEW, OLD, {'profile': PROFILE})[0]
    assert state['clicks'] == 0 and state['draft'] == OLD


def test_unread_editor_is_not_empty_about(monkeypatch):
    updater, state = editor(monkeypatch)
    updater.driver.get.side_effect = OSError('Browser closed')
    assert updater.read_about_section() is None
    assert not updater.replace_about_section(NEW, OLD, {'profile': PROFILE})[0]
    assert state['clicks'] == 0


def test_about_load_deadline_does_not_discard_ready_form(monkeypatch):
    from selenium.common.exceptions import TimeoutException
    updater, state = editor(monkeypatch)
    updater.driver.current_url = 'https://hh.ru/resume/edit/test-resume/about'
    updater.driver.get.side_effect = TimeoutException('load never completes')
    assert updater.read_about_section() == OLD
    updater.driver.set_page_load_timeout.assert_called_with(30)
    assert state['clicks'] == 0


def test_regional_editor_read_save_and_manual_draft(monkeypatch):
    updater, state = editor(monkeypatch)
    original_get = updater.driver.get.side_effect

    def regional_get(url):
        original_get(url)
        updater.driver.current_url = url.replace('https://hh.ru/', 'https://krasnoyarsk.hh.ru/') + '?from=resume#about'

    updater.driver.get.side_effect = regional_get
    assert updater.read_about_section() == OLD
    assert updater.replace_about_section(NEW, OLD, {'profile': PROFILE})[0]
    assert state['saved'] == NEW and state['clicks'] == 1
    updater.driver.get.reset_mock()
    state['draft'] = 'Manual draft'
    assert not updater.replace_about_section(OLD, NEW, {'profile': PROFILE})[0]
    updater.driver.get.assert_not_called()
    assert state['draft'] == 'Manual draft' and state['clicks'] == 1


@pytest.mark.parametrize('url', [
    'https://hh.ru.evil.test/resume/edit/test-resume/about',
    'https://evilhh.ru/resume/edit/test-resume/about',
    'https://hh.ru/resume/edit/another-resume/about',
    'http://hh.ru/resume/edit/test-resume/about',
    'https://hh.ru:8443/resume/edit/test-resume/about',
    'https://krasnoyarsk.hh.ru/login',
])
def test_regional_editor_rejects_wrong_origin_or_resume(monkeypatch, url):
    updater, _ = editor(monkeypatch)
    updater.driver.get.side_effect = lambda _: setattr(updater.driver, 'current_url', url)
    updater.driver.execute_script.return_value = {}
    assert updater.read_about_section() is None


def test_about_wrong_page_is_diagnosed_without_saving(monkeypatch, tmp_path):
    updater, state = editor(monkeypatch)
    updater.driver.get.side_effect = lambda url: setattr(updater.driver, 'current_url', 'https://hh.ru/login')
    updater.driver.execute_script.return_value = {'path': '/login', 'fields': [], 'editLinks': []}
    monkeypatch.setattr('resume_updater.SCRIPT_DIR', str(tmp_path))
    assert updater.read_about_section() is None
    assert list((tmp_path / '.resume_revisions').glob('*-editor.json'))
    updater.driver.save_screenshot.assert_called_once()
    assert state['clicks'] == 0


def test_about_is_updated_without_missing_skills(monkeypatch, tmp_path):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'resume_id': 'test-resume', 'resume_title': 'AppSec',
                                  'candidate_profile': PROFILE, 'ai_config': {'enabled': False}}), encoding='utf-8')
    analyzer = RejectionAnalyzer(config_file=str(config), headless=True)
    analyzer.ai_assistant = ai()
    updater, state = editor(monkeypatch)
    updater.close = MagicMock()
    monkeypatch.setattr('resume_updater.HHResumeUpdater', lambda **kwargs: updater)
    result = analyzer._modernize_resume([], auto=True, analyses=[ANALYSIS])
    assert result['skills_added_count'] == 0 and result['about_changed']
    assert state['saved'] == NEW
    assert analyzer.ai_assistant.resume_feedback['status'] == 'verified'
    # Тяжёлый ИИ-фильтр сам не включается (06.10 он отсеивал почти все ИБ-вакансии).
    assert 'ai_filter' not in analyzer.ai_assistant.config


def test_analysis_dispatches_text_plan_even_without_tags(monkeypatch, tmp_path):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'ai_config': {'enabled': False}}), encoding='utf-8')
    analyzer = RejectionAnalyzer(config_file=str(config), headless=True)
    analyzer._load_cached_chats = lambda **kwargs: [dict(vacancy_title='AppSec', company_name='X',
                                                       employer_messages=['Отказ'], description='')]
    analyzer.seen_rejection_keys = lambda: set()
    analyzer.ai_assistant.analyze_chat_rejection = lambda **kwargs: dict(ANALYSIS)
    analyzer._modernize_resume = MagicMock(return_value={'about_changed': True})
    result = analyzer.run_chat_analysis(fetch_live=False, use_mock_if_empty=False, auto_apply=True)
    assert result['resume_update']['about_changed']
    assert analyzer._modernize_resume.call_args.kwargs['analyses'][0]['missing_skills'] == []


def test_saved_refusals_can_improve_resume_with_no_unread_chats(monkeypatch, tmp_path):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'resume_id': 'test-resume', 'candidate_profile': PROFILE,
                                  'ai_config': {'enabled': False}}), encoding='utf-8')
    analyzer = RejectionAnalyzer(config_file=str(config), headless=True)
    analyzer.db.record_rejection_analysis('1', 'Role', 'Test', remediation_advice=['Слишком общее письмо', NEW, ''])
    analyzer._load_cached_chats = lambda **kwargs: []
    analyzer._modernize_resume = MagicMock(return_value={'about_changed': True})
    result = analyzer.run_chat_analysis(fetch_live=False, use_mock_if_empty=False, auto_apply=True)
    assert result['total_analyzed'] == 0 and result['resume_update']['about_changed']
    assert analyzer._modernize_resume.call_args.kwargs['analyses'][0]['about_me_recommendation'] == NEW


def test_sample_skills_never_reach_resume_dispatch(tmp_path):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'ai_config': {'enabled': False}}), encoding='utf-8')
    analyzer = RejectionAnalyzer(config_file=str(config), headless=True)
    analyzer._modernize_resume = MagicMock()
    analyzer.apply_resume_feedback({'top_missing_skills': [('Python', 1)]},
                                   [dict(ANALYSIS, is_sample=True, missing_skills=['Python'])], True)
    analyzer._modernize_resume.assert_not_called()


def test_full_cycle_stops_if_resume_save_is_unconfirmed(monkeypatch):
    import test as entrypoint
    analyzer = MagicMock(config={}, _user_closed=False, messenger_summary={})
    monkeypatch.setattr('rejection_analyzer.RejectionAnalyzer', lambda: analyzer)
    monkeypatch.setattr('rejection_analyzer.run_chat_analysis_with_recovery',
                        lambda *args, **kwargs: {'status': 'resume_update_blocked',
                                                'resume_update': {'blocked': True, 'reason': 'save unconfirmed'}})
    monkeypatch.setattr(entrypoint.sys, 'argv', ['test.py'])
    monkeypatch.setattr(entrypoint.time, 'sleep', lambda _: None)
    applicant = entrypoint.HHAutoApplicant.__new__(entrypoint.HHAutoApplicant)
    applicant.sync_cache_with_applied = MagicMock()
    applicant.run_auto_applications({})
    applicant.sync_cache_with_applied.assert_not_called()


def test_reports_distinguish_proposal_from_saved_text(tmp_path):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'ai_config': {'enabled': False}}), encoding='utf-8')
    analyzer = RejectionAnalyzer(config_file=str(config), headless=True)
    summary = {'total_analyzed': 1, 'resume_update': {'about_changed': True, 'about_message': 'Перечитано на HH'}}
    path = tmp_path / 'report.md'
    analyzer._generate_chat_markdown_report(summary, str(path))
    assert 'Фактически применённые правки' in path.read_text(encoding='utf-8')
    assert 'Перечитано на HH' in path.read_text(encoding='utf-8')


@pytest.mark.parametrize('status, enabled', [('verified', True), ('pending', False)])
def test_feedback_loads_only_verified_target_and_filters_actual_description(status, enabled):
    save_resume_revision({'resume_id': 'test-resume', 'status': status, 'before': OLD, 'after': NEW,
                          'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                          'selection_guidance': ['Основная роль AppSec']})
    assistant = AIAssistant({'resume_id': 'test-resume', 'resume_title': 'AppSec', 'auto_ai_filter_after_resume_edit': True,
                             'candidate_profile': PROFILE, 'ai_config': {'enabled': True}})
    assistant._call_llm = MagicMock()
    assert bool(assistant.resume_feedback) is enabled
    if enabled:
        assistant._call_llm.return_value = '{"suitable":false,"reason":"Обязателен другой профиль"}'
        assert not assistant.filter_vacancy('Role', 'Обязательные требования', ['Python'])[0]
        payload = json.loads(assistant._call_llm.call_args.args[0])
        assert payload['rejection_lessons'] == ['Основная роль AppSec']
        assert payload['vacancy']['description'] == 'Обязательные требования'
    other = AIAssistant({'resume_id': 'another', 'ai_config': {'enabled': False}})
    assert not other.resume_feedback and 'ai_filter' not in other.config


def test_disabled_ai_does_not_enable_feedback_filter():
    save_resume_revision({'resume_id': 'test-resume', 'status': 'verified', 'before': OLD, 'after': NEW,
                          'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')})
    assistant = AIAssistant({'resume_id': 'test-resume', 'ai_config': {'enabled': False}})
    assert assistant.resume_feedback and 'ai_filter' not in assistant.config


def test_missing_profile_never_uses_example_facts():
    assistant = AIAssistant({'ai_config': {'enabled': True}})
    assistant._call_llm = MagicMock(return_value=json.dumps({'about': OLD, 'selection_guidance': []}))
    assert assistant.improve_resume_about(OLD, [ANALYSIS], 'AppSec')
    payload = json.loads(assistant._call_llm.call_args.args[0])
    assert payload['candidate_facts'] == OLD


def test_outcomes_exclude_old_other_resume_and_unknown_attribution(tmp_path):
    db = DatabaseManager(str(tmp_path / 'db.sqlite'))
    since = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    for vid, rid, status in [('1', 'r', 'invited'), ('2', 'r', 'discarded'), ('3', 'r', 'sent'),
                             ('4', 'other', 'discarded'), ('5', '', 'discarded'), ('6', 'r', 'discarded')]:
        assert db.record_application(vid, 'Role', status=status, resume_id=rid)
    with db._get_connection() as conn:
        conn.execute('UPDATE applications SET applied_at=? WHERE vacancy_id=?',
                     ((datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S'), '6'))
    assert db.get_resume_outcomes('r', since) == {
        'sent': 3, 'invited': 1, 'discarded': 1, 'pending': 1, 'rejection_rate': 50.0}
    db.update_application_status('3', 'invited')
    assert db.get_resume_outcomes('r', since)['invited'] == 2
    assert db.get_resume_outcomes('unseen', since)['rejection_rate'] is None
    db.record_application('1', 'Role', resume_id='other', status='invited')
    with db._get_connection() as conn:
        assert conn.execute('SELECT resume_id FROM applications WHERE vacancy_id="1"').fetchone()[0] == 'r'


def test_hyphenated_tenure_is_removed_before_resume_write():
    assert '6' not in clean_public_text('AppSec-инженер с 6‑летним опытом проверки API.', PROFILE)


@pytest.mark.parametrize('preview, expected', [('Role\nСобеседование', 'invited'),
                                               ('Role\nОтклик на вакансию', None)])
def test_chat_outcomes_use_hh_status_not_question_text(preview, expected):
    analyzer = RejectionAnalyzer.__new__(RejectionAnalyzer)
    analyzer.config = {'chat_autoreply': {'enabled': False}}
    analyzer.db = MagicMock()
    analyzer._load_chat_actions = lambda: {}
    chat = {'vacancy_url': 'https://hh.ru/vacancy/123',
            'messages': [{'text': 'Здравствуйте! Спасибо за отклик.', 'isOut': False}]}
    analyzer._handle_chat(chat, preview)
    if expected:
        analyzer.db.update_application_status.assert_called_once_with('123', expected)
    else:
        analyzer.db.update_application_status.assert_not_called()


@pytest.mark.skipif(os.environ.get('HH_BROWSER_SMOKE') != '1', reason='Isolated Chrome opt-in')
@pytest.mark.parametrize('save_works', [True, False])
def test_real_chrome_about_multiline_and_persistence(tmp_path, monkeypatch, save_works):
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    page = tmp_path / 'resume.html'
    page.write_text('''<!doctype html><meta charset="utf-8">
        <textarea data-qa="resume-editor-about"></textarea>
        <button data-qa="resume-partial-edit-save">Save</button>
        <script>const field=document.querySelector('textarea');
        field.value=sessionStorage.getItem('about')||%s;
        document.querySelector('button').onclick=()=>{
            if (%s) sessionStorage.setItem('about',field.value);
        };</script>''' % (json.dumps(OLD), str(save_works).lower()), encoding='utf-8')
    options = webdriver.ChromeOptions()
    options.add_argument('--headless=new')
    options.add_argument('--user-data-dir=' + str(tmp_path / 'chrome'))
    driver = webdriver.Chrome(options=options)
    try:
        updater = HHResumeUpdater(resume_id='test-resume', driver=driver, headless=True)
        def read_fixture():
            driver.get(page.as_uri())
            return driver.find_element(By.CSS_SELECTOR, 'textarea').get_attribute('value')
        updater.read_about_section = read_fixture
        text = NEW + '\n\nПроверяю API и оформляю технические отчёты.'
        ok, _ = updater.replace_about_section(text, OLD, {'profile': PROFILE})
        assert ok is save_works
        assert read_fixture() == (text if save_works else OLD)
        assert load_resume_revision('test-resume')['status'] == ('verified' if save_works else 'pending')
    finally:
        driver.quit()


def test_feedback_filter_only_when_user_allowed_it():
    save_resume_revision({'resume_id': 'test-resume', 'status': 'verified', 'before': OLD, 'after': NEW,
                          'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')})
    on = AIAssistant({'resume_id': 'test-resume', 'auto_ai_filter_after_resume_edit': True,
                      'ai_config': {'enabled': True}})
    assert on.config['ai_filter']['mode'] == 'heavy'

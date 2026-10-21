import copy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from selenium import webdriver
from selenium.webdriver.common.by import By

import config_manager
from db_manager import DatabaseManager
from hh_selenium import HHSeleniumBot
from question_memory import QuestionMemory, question_signature


CONFIG = {'resume_id': 'resume-a', 'candidate_profile': {'skills': ['Python', 'Linux']},
          'question_answers': {}}
QUESTION = 'Какой формат работы рассматриваете?'
OPTIONS = ['Офис', 'Гибрид', 'Удалённо']


@pytest.fixture
def memory(tmp_path):
    return QuestionMemory(DatabaseManager(str(tmp_path / 'answers.db')), copy.deepcopy(CONFIG))


def test_only_user_confirmation_enables_persistent_reuse(memory):
    key = memory.stage(QUESTION, 'single', OPTIONS, ['Офис'], 'https://hh.ru/vacancy/1')
    assert memory.find(QUESTION, 'single', OPTIONS) is None
    memory.confirm(key, ['Удалённо'])
    memory.stage(QUESTION, 'single', OPTIONS, ['Офис'], 'https://hh.ru/vacancy/2')
    reopened = QuestionMemory(DatabaseManager(memory.db.db_path), copy.deepcopy(CONFIG))
    assert reopened.find(QUESTION, 'single', OPTIONS) == ['Удалённо']
    assert reopened.records()[0]['source_url'] == 'https://hh.ru/vacancy/1'


def test_matching_keeps_negation_type_and_option_set_but_allows_reordering(memory):
    text = QUESTION + '\n' + '\n'.join(OPTIONS)
    key = memory.stage(text, 'single', OPTIONS, ['Удалённо'])
    memory.confirm(key)
    reversed_options = list(reversed(OPTIONS))
    assert memory.find(QUESTION.upper() + '\n' + '\n'.join(reversed_options),
                       'single', reversed_options) == ['Удалённо']
    assert memory.find(QUESTION, 'single', OPTIONS + ['Другое']) is None
    assert memory.find(QUESTION, 'multi', OPTIONS) is None
    assert memory.find('Какой формат работы НЕ рассматриваете?', 'single', OPTIONS) is None


@pytest.mark.parametrize('change', ['resume', 'profile', 'answers', 'policy', 'account', 'env_resume'])
def test_memory_is_scoped_to_candidate_and_account(memory, monkeypatch, change):
    key = memory.stage(QUESTION, 'single', OPTIONS, ['Удалённо'])
    memory.confirm(key)
    cfg = copy.deepcopy(CONFIG)
    if change == 'resume':
        cfg['resume_id'] = 'resume-b'
    elif change == 'profile':
        cfg['candidate_profile']['skills'].append('Rust')
    elif change == 'answers':
        cfg['question_answers']['формат'] = 'Офис'
    elif change == 'policy':
        cfg['answer_policy'] = {'yes_to_conditions': False}
    elif change == 'account':
        monkeypatch.setattr('app_paths.PROFILE_ID', 'second')
    else:
        monkeypatch.setenv('HH_RESUME_ID', 'resume-b')
    assert QuestionMemory(memory.db, cfg).find(QUESTION, 'single', OPTIONS) is None


def test_forgotten_answer_is_not_resuggested_and_payload_is_removed(memory):
    key = memory.stage(QUESTION, 'single', OPTIONS, ['Удалённо'])
    memory.confirm(key)
    memory.forget(key)
    memory.stage(QUESTION, 'single', OPTIONS, ['Офис'])
    assert memory.find(QUESTION, 'single', OPTIONS) is None
    assert memory.records() == []
    with memory.db._get_connection() as conn:
        assert conn.execute('SELECT payload FROM questionnaire_answers').fetchone()[0] == '{}'


@pytest.mark.parametrize('question', ['Ваши зарплатные ожидания?', 'Сколько лет вы работали?',
                                     'Почему хотите работать у нас?', 'Что знаете о нашей компании?'])
def test_restricted_or_employer_specific_questions_are_not_learned(memory, question):
    with pytest.raises(ValueError):
        memory.stage(question, 'text', [], ['Готов обсудить на собеседовании.'])


@pytest.mark.parametrize('kind,options,answers', [
    ('single', OPTIONS, ['Неизвестный вариант']), ('single', OPTIONS, ['Офис', 'Гибрид']),
    ('multi', OPTIONS, ['Офис', 'Офис']), ('text', [], ['']),
    ('text', [], ['Как ИИ-ассистент, я не могу помочь.']),
    ('single', ['Да', 'да'], ['Да']),
])
def test_invalid_or_service_answers_cannot_be_confirmed(memory, kind, options, answers):
    with pytest.raises(ValueError):
        memory.stage(QUESTION, kind, options, answers)


def test_confirmation_validates_edited_answer_again(memory):
    key = memory.stage('Расскажите об опыте Python', 'text', [], ['Проверял API на Python.'])
    with pytest.raises(ValueError):
        memory.confirm(key, ['Мой опыт более 6 лет.'])
    assert memory.records()[0]['status'] == 'pending'


def test_menu_can_edit_confirm_and_forget_without_saving_config(memory, monkeypatch, capsys):
    key = memory.stage(QUESTION, 'single', OPTIONS, ['Офис'])
    monkeypatch.setattr(config_manager, 'load_config', lambda: memory.config)
    monkeypatch.setattr('db_manager.DatabaseManager', lambda: memory.db)
    save = Mock()
    monkeypatch.setattr(config_manager, 'save_config', save)
    inputs = iter(['1', 'e', '3', '0'])
    monkeypatch.setattr('builtins.input', lambda _: next(inputs))
    config_manager.edit_question_memory()
    assert memory.find(QUESTION, 'single', OPTIONS) == ['Удалённо']
    assert 'Подтверждено' in capsys.readouterr().out
    inputs = iter(['1', '-', '0'])
    config_manager.edit_question_memory()
    assert memory.records() == []
    save.assert_not_called()


def test_settings_q_opens_answers_instead_of_exiting(monkeypatch):
    import test as menu
    monkeypatch.setattr('builtins.input', lambda _: 'q')
    assert menu.resolve_menu_choice('n') == 'question_answers'
    assert menu.resolve_menu_choice('q') == 'q'


def test_main_dispatches_settings_q_to_answer_editor(monkeypatch):
    import test as menu
    monkeypatch.setattr(menu.sys, 'argv', ['test.py'])
    monkeypatch.setattr(menu, 'get_active_resume', lambda: ('resume-a', 'Test'))
    monkeypatch.setattr(menu, 'get_active_preset', lambda: {'name': 'Test'})
    monkeypatch.setattr(menu, 'check_first_run_setup', lambda: [])
    monkeypatch.setattr(menu, 'search_direction_chosen', lambda _: True)
    monkeypatch.setattr(menu, 'load_config', lambda: {'ai_config': {'enabled': False}})
    monkeypatch.setattr('ai_assistant.AIAssistant.probe_report', lambda _: '')
    monkeypatch.setattr('ai_assistant.ai_menu_lines', lambda _: ('Test', 'Test'))
    editor = Mock()
    monkeypatch.setattr(config_manager, 'edit_question_answers', editor)
    inputs = iter(['n', 'q', '0'])
    monkeypatch.setattr('builtins.input', lambda _: next(inputs))
    menu.main()
    editor.assert_called_once_with()


def test_menu_confirms_the_displayed_answer_during_concurrent_draft_update(memory, monkeypatch):
    memory.stage(QUESTION, 'single', OPTIONS, ['Офис'])
    monkeypatch.setattr(config_manager, 'load_config', lambda: memory.config)
    monkeypatch.setattr('db_manager.DatabaseManager', lambda: memory.db)
    inputs = iter(['1', 'c', '0'])
    def respond(prompt):
        choice = next(inputs)
        if choice == 'c':
            memory.stage(QUESTION, 'single', OPTIONS, ['Гибрид'])
        return choice
    monkeypatch.setattr('builtins.input', respond)
    config_manager.edit_question_memory()
    assert memory.find(QUESTION, 'single', OPTIONS) == ['Офис']


@pytest.fixture(scope='module')
def browser(tmp_path_factory):
    root = tmp_path_factory.mktemp('question-memory-browser')
    options = webdriver.ChromeOptions()
    options.add_argument('--headless=new')
    options.add_argument(f'--user-data-dir={root / "chrome"}')
    driver = webdriver.Chrome(options=options)
    try:
        yield driver
    finally:
        driver.quit()


def open_form(browser, tmp_path, html, config):
    page = tmp_path / 'questionnaire.html'
    page.write_text('<meta charset="utf-8">' + html, encoding='utf-8')
    browser.get(page.as_uri())
    bot = HHSeleniumBot.__new__(HHSeleniumBot)
    bot.driver, bot.config, bot.db = browser, copy.deepcopy(config), DatabaseManager()
    bot.click_element_with_mouse = lambda element: element.click() or True
    return bot


def form(reverse=False):
    options = ['Python', 'Go', 'Rust']
    choices = ['Да', 'Нет']
    modes = OPTIONS[:]
    if reverse:
        options.reverse()
        choices.reverse()
        modes.reverse()
    return '''<div data-qa="vacancy-response-popup-form-question">Расскажите об опыте проверки API
       <textarea id="text"></textarea></div>''' + (
        '<div data-qa="vacancy-response-popup-form-question">Применяли ли вы Python для тестов API?' +
        ''.join(f'<label><input type="radio" name="experience" value="{s}">{s}</label>' for s in choices) + '</div>' +
        '<div data-qa="vacancy-response-popup-form-question">Какие языки используете для проверки API?' +
        ''.join(f'<label><input type="checkbox" value="{s}" {"checked" if reverse and s == "Go" else ""}>{s}</label>'
                for s in options) + '</div>' +
        '<div data-qa="vacancy-response-popup-form-question">Какой формат работы рассматриваете?' +
        '<select><option value=""></option>' + ''.join(f'<option>{s}</option>' for s in modes) + '</select></div>')


def test_browser_collect_review_restart_and_reuse_without_ai(browser, tmp_path, monkeypatch):
    bot = open_form(browser, tmp_path, form(), CONFIG)
    def generate(items):
        answers = ['Проверял REST API с помощью Python и pytest.', 'Да', 'Python | Rust', 'Удалённо']
        return {item['text']: answer for item, answer in zip(items, answers)}
    bot.ai_assistant = SimpleNamespace(enabled=True, answer_questions_batch=Mock(side_effect=generate))
    assert bot.answer_employer_questions() == 5
    memory = bot.question_memory()
    assert len(memory.records()) == 4
    assert all(row['status'] == 'pending' for row in memory.records())
    monkeypatch.setattr(config_manager, 'load_config', lambda: copy.deepcopy(CONFIG))
    for row in memory.records():
        index = next(i for i, item in enumerate(memory.records(), 1) if item['signature'] == row['signature'])
        inputs = iter([str(index), 'c', '0'])
        monkeypatch.setattr('builtins.input', lambda _: next(inputs))
        config_manager.edit_question_memory()
    assert all(row['status'] == 'confirmed' for row in memory.records())

    restarted = open_form(browser, tmp_path, form(reverse=True), CONFIG)
    ai = restarted.ai_assistant = SimpleNamespace(enabled=True, answer_questions_batch=Mock(
        side_effect=AssertionError('Confirmed answers must bypass AI')))
    assert restarted.answer_employer_questions() == 5
    ai.answer_questions_batch.assert_not_called()
    assert not restarted.unanswered_questions
    assert browser.find_element(By.ID, 'text').get_attribute('value').startswith('Проверял REST API')
    assert [e.get_attribute('value') for e in browser.find_elements(By.CSS_SELECTOR, 'input[type=radio]:checked')] == ['Да']
    assert {e.get_attribute('value') for e in browser.find_elements(By.CSS_SELECTOR, 'input[type=checkbox]:checked')} == {'Python', 'Rust'}
    assert browser.find_element(By.CSS_SELECTOR, 'select').get_attribute('value') == 'Удалённо'
    ai.enabled = False
    restarted = open_form(browser, tmp_path, form(), CONFIG)
    restarted.ai_assistant = ai
    assert restarted.answer_employer_questions() == 5
    ai.answer_questions_batch.assert_not_called()


def test_duplicate_questions_are_not_reused_or_staged(browser, tmp_path):
    html = ''.join('<div data-qa="vacancy-response-popup-form-question">Расскажите об опыте проверки API'
                   '<textarea></textarea></div>' for _ in range(2))
    bot = open_form(browser, tmp_path, html, CONFIG)
    key = bot.question_memory().stage('Расскажите об опыте проверки API', 'text', [], ['Проверял API.'])
    bot.question_memory().confirm(key)
    bot.ai_assistant = None
    bot.answer_employer_questions()
    assert len(bot.unanswered_questions) == 2
    assert not any(e.get_attribute('value') for e in browser.find_elements(By.CSS_SELECTOR, 'textarea'))


def test_failed_readback_does_not_fall_back_to_another_answer(browser, tmp_path, monkeypatch):
    bot = open_form(browser, tmp_path, form(), CONFIG)
    question = 'Расскажите об опыте проверки API'
    key = bot.question_memory().stage(question, 'text', [], ['Проверял API.'])
    bot.question_memory().confirm(key)
    block = browser.find_element(By.CSS_SELECTOR, '[data-qa="vacancy-response-popup-form-question"]')
    monkeypatch.setattr(bot, 'set_text_input_value', lambda *_: False)
    generate = Mock(side_effect=AssertionError('No second answer after failed memory fill'))
    bot.ai_assistant = SimpleNamespace(enabled=True, answer_question=generate)
    count, problem = bot.answer_single_question(block, block.text, {}, set())
    assert count == 0 and 'не удалось' in problem
    generate.assert_not_called()

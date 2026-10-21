import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from unittest.mock import MagicMock
from hh_selenium import HHSeleniumBot


@pytest.fixture
def bot(monkeypatch):
    # Создаем бота без запуска драйвера. Целевое резюме задаём явно: раньше
    # тесты брали его из настроек автора и на новой машине падали.
    import config_manager
    monkeypatch.setattr(config_manager, 'get_active_resume',
                        lambda: ('test-id', 'Application Security Engineer / AppSec-инженер'))
    bot = HHSeleniumBot(headless=True)
    bot.driver = MagicMock()
    bot.page_has_captcha = MagicMock(return_value=False)
    return bot


def test_detect_response_state_success_in_page_text(bot):
    bot.get_visible_response_state = MagicMock(return_value={'controls': [{'text': 'отклик отправлен'}]})
    bot.is_response_limit_reached = MagicMock(return_value=False)
    state = bot.detect_response_state()
    assert state == 'success'


def test_detect_response_state_vy_otkliknulis(bot):
    bot.get_visible_response_state = MagicMock(return_value={'controls': []})
    bot.get_response_control_texts = MagicMock(return_value=['вы откликнулись'])
    bot.is_response_limit_reached = MagicMock(return_value=False)
    state = bot.detect_response_state()
    assert state == 'success'


def test_open_cover_letter_in_modal_clicks_toggle(bot):
    modal = MagicMock()
    toggle_button = MagicMock()
    toggle_button.is_displayed.return_value = True
    toggle_button.text = "Добавить сопроводительное"

    # Сначала поле ввода отсутствует, после клика появляется
    call_count = {"count": 0}
    def mock_find_fields():
        if call_count["count"] == 0:
            call_count["count"] += 1
            return []
        return [MagicMock()]

    bot.find_cover_letter_fields = mock_find_fields
    bot.click_element_with_mouse = MagicMock(return_value=True)
    modal.find_elements.return_value = [toggle_button]

    opened = bot.open_cover_letter_in_modal(modal)
    assert opened is True
    bot.click_element_with_mouse.assert_called_once_with(toggle_button)


def test_ensure_target_resume_already_selected(bot):
    modal = MagicMock()
    title_elem = MagicMock()
    title_elem.is_displayed.return_value = True
    title_elem.text = "Application Security Engineer / AppSec-инженер"
    modal.find_elements.return_value = [title_elem]

    ok, err = bot.ensure_target_resume_selected(modal)
    assert ok is True
    assert err is None


def test_ensure_target_resume_switches_from_photographer(bot):
    modal = MagicMock()
    # Элемент текущего резюме в модалке
    bad_title = MagicMock()
    bad_title.is_displayed.return_value = True
    bad_title.text = "Фотограф видеограф 1 000 $"
    modal.find_elements.return_value = [bad_title]

    # Элементы в выпадающем списке выбора резюме
    target_option = MagicMock()
    target_option.is_displayed.return_value = True
    target_option.text = "Application Security Engineer / AppSec-инженер"

    def click(element):
        if element is target_option:
            bad_title.text = target_option.text
        return True
    bot.click_element_with_mouse = MagicMock(side_effect=click)
    bot.find_response_modal = lambda **_kwargs: modal
    bot.driver.find_elements.return_value = [target_option]

    ok, err = bot.ensure_target_resume_selected(modal)
    assert ok is True
    assert err is None
    # Должен был кликнуть дважды: сначала открыть список, потом выбрать целевое резюме
    assert bot.click_element_with_mouse.call_count == 2


def test_ensure_target_resume_blocks_photographer_if_not_switched(bot):
    modal = MagicMock()
    bad_title = MagicMock()
    bad_title.is_displayed.return_value = True
    bad_title.text = "Фотограф видеограф"
    modal.find_elements.return_value = [bad_title]

    bot.click_element_with_mouse = MagicMock(return_value=True)
    bot.driver.find_elements.return_value = []  # Опций нет

    ok, err = bot.ensure_target_resume_selected(modal)
    assert ok is False
    assert "активно резюме" in err.lower() or "фотограф" in err.lower()


def test_apply_to_vacancy_letter_sent_initialized(bot):
    bot.page_has_captcha = MagicMock(return_value=False)
    bot.driver.get = MagicMock()
    bot.detect_response_state = MagicMock(return_value='ready')
    bot.get_vacancy_page_company = MagicMock(return_value="Test Corp")
    bot.get_vacancy_page_description = MagicMock(return_value="DevSecOps engineer needed")
    bot.get_vacancy_page_skills = MagicMock(return_value=["Python", "AppSec"])
    bot.is_response_limit_reached = MagicMock(return_value=False)
    bot.handle_warning_popups = MagicMock(return_value=False)
    bot.click_element_with_mouse = MagicMock(return_value=True)

    apply_btn = MagicMock()
    apply_btn.text = "Откликнуться"
    apply_btn.is_displayed.return_value = True
    bot.driver.find_elements.return_value = [apply_btn]
    bot.driver.find_elements.side_effect = lambda _by, selector: [] if 'account-login-form' in selector else [apply_btn]

    # submit_open_response_modal returns submitted=True, letter_sent=True, answered=0, blocker=None
    bot.submit_open_response_modal = MagicMock(return_value=(True, True, 0, None))
    bot.confirm_response_submission = MagicMock(return_value=(True, "С письмо"))

    success, message = bot.apply_to_vacancy("https://hh.ru/vacancy/123456", "AppSec Engineer")
    assert success is True
    assert message == "С письмо"
    bot.submit_open_response_modal.assert_called_once()
    # Ensure letter_sent argument was passed as False initially
    call_args = bot.submit_open_response_modal.call_args[0]
    assert call_args[1] is False


def test_application_login_gate_recovers_before_letter_or_send(bot):
    bot.driver.current_url = 'https://hh.ru/vacancy/123'
    bot.driver.get = MagicMock()
    bot.maybe_bump_resume = MagicMock()
    bot.random_delay = MagicMock()
    form = MagicMock()
    form.tag_name = 'form'
    bot.driver.find_elements.side_effect = [[form], []]
    bot.check_login = MagicMock(return_value=True)
    bot.detect_response_state = MagicMock(return_value='already')
    bot.ai_assistant.generate_cover_letter = MagicMock()
    bot.submit_open_response_modal = MagicMock()
    assert bot.apply_to_vacancy('https://hh.ru/vacancy/123', 'AppSec') == (False, 'Уже откликнулись')
    bot.check_login.assert_called_once()
    assert bot.driver.get.call_count == 2
    bot.ai_assistant.generate_cover_letter.assert_not_called()
    bot.submit_open_response_modal.assert_not_called()


def test_regional_login_gate_recovers_on_same_canonical_vacancy(bot):
    bot.driver.current_url = 'https://hh.ru/vacancy/123'
    bot.maybe_bump_resume = MagicMock()
    bot.random_delay = MagicMock()
    form = MagicMock(tag_name='form')
    bot.driver.find_elements.side_effect = [[form], []]
    bot.check_login = MagicMock(return_value=True)
    bot.detect_response_state = MagicMock(return_value='already')
    bot.submit_open_response_modal = MagicMock()
    original = 'https://spb.hh.ru/vacancy/123?from=login'
    assert bot.apply_to_vacancy(original, 'AppSec') == (False, 'Уже откликнулись')
    assert bot.driver.get.call_args_list[-1].args == ('https://hh.ru/vacancy/123',)
    bot.submit_open_response_modal.assert_not_called()


def test_login_recovery_waits_for_transient_form_to_disappear(bot, monkeypatch):
    import hh_selenium
    bot.maybe_bump_resume = MagicMock()
    bot.random_delay = MagicMock()
    form = MagicMock(tag_name='form')
    bot.driver.find_elements.side_effect = [[form], [form], [form], []]
    bot.driver.current_url = 'https://hh.ru/vacancy/123'
    bot.check_login = MagicMock(return_value=True)
    bot.detect_response_state = MagicMock(return_value='already')
    monkeypatch.setattr(hh_selenium.time, 'sleep', lambda _: None)
    assert bot.apply_to_vacancy('https://spb.hh.ru/vacancy/123', 'AppSec') == (False, 'Уже откликнулись')
    assert bot.driver.find_elements.call_count == 4


def test_login_recovery_never_sends_to_redirected_different_vacancy(bot):
    bot.maybe_bump_resume = MagicMock()
    bot.random_delay = MagicMock()
    form = MagicMock(tag_name='form')
    bot.driver.find_elements.side_effect = [[form], []]
    bot.check_login = MagicMock(return_value=True)
    bot.driver.current_url = 'https://hh.ru/vacancy/999'
    bot.application_ui_problem = MagicMock(return_value='wrong vacancy')
    bot.submit_open_response_modal = MagicMock()
    assert bot.apply_to_vacancy('https://spb.hh.ru/vacancy/123', 'AppSec') == (False, 'wrong vacancy')
    bot.submit_open_response_modal.assert_not_called()


def test_unrecoverable_login_gate_stops_without_sending(bot):
    bot.maybe_bump_resume = MagicMock()
    bot.random_delay = MagicMock()
    form = MagicMock()
    form.tag_name = 'form'
    bot.driver.find_elements.return_value = [form]
    bot.check_login = MagicMock(return_value=False)
    bot.login = MagicMock(return_value=False)
    bot.submit_open_response_modal = MagicMock()
    ok, reason = bot.apply_to_vacancy('https://hh.ru/vacancy/123', 'AppSec')
    assert not ok and 'вход' in reason.lower()
    assert bot.stop_requested
    bot.submit_open_response_modal.assert_not_called()


@pytest.mark.parametrize('failure', ['tls', 'hh_before_confirm', 'hh_after_confirm', 'hh_before_submit',
                                     'network_after_dispatch', 'driver_after_dispatch', 'captcha_after_dispatch'])
def test_post_navigation_failure_is_not_retried_as_network(bot, failure):
    from hh_selenium import APPLY_NOT_CONFIRMED_MARKER, NETWORK_MESSAGE
    bot.maybe_bump_resume = MagicMock()
    bot.random_delay = MagicMock()
    bot.driver.find_elements.side_effect = lambda _by, selector: [] if 'account-login-form' in selector else [MagicMock()]
    bot.detect_response_state = MagicMock(return_value='ready')
    bot.page_has_captcha = MagicMock(return_value=False)
    bot.page_says_archived = MagicMock(return_value=False)
    bot.get_vacancy_page_company = MagicMock(return_value='Synthetic')
    bot.get_vacancy_page_description = MagicMock(return_value='AppSec')
    bot.get_vacancy_page_skills = MagicMock(return_value=[])
    bot.config = {'ai_config': {'enabled': False}, 'skip_keywords': []}
    bot.click_element_with_mouse = MagicMock(return_value=True)
    bot.find_response_modal = MagicMock(return_value=True)
    bot.is_response_limit_reached = MagicMock(return_value=False)
    bot.handle_warning_popups = MagicMock(return_value=False)
    bot.application_ui_problem = MagicMock(return_value='diagnostic')
    bot.submit_open_response_modal = MagicMock(return_value=(failure != 'hh_before_submit', False, 0,
        'Ошибка HH после клика' if failure in ('hh_before_confirm', 'hh_before_submit') else None))
    bot.confirm_response_submission = MagicMock(return_value=(False, 'Ошибка HH после клика'))
    if failure == 'captcha_after_dispatch':
        bot.submit_open_response_modal.return_value = (True, False, 0, 'Требуется капча')
        bot.confirm_post_dispatch_captcha = MagicMock(return_value=(False, APPLY_NOT_CONFIRMED_MARKER))
    if failure == 'tls':
        bot.detect_response_state.side_effect = RuntimeError('net::ERR_SSL_PROTOCOL_ERROR')
    elif failure in ('network_after_dispatch', 'driver_after_dispatch'):
        bot.confirm_response_submission.side_effect = RuntimeError(
            'net::ERR_CONNECTION_RESET' if failure == 'network_after_dispatch' else 'invalid session id')
    ok, message = bot.apply_to_vacancy('https://hh.ru/vacancy/123', 'AppSec')
    assert not ok and message != NETWORK_MESSAGE
    assert (APPLY_NOT_CONFIRMED_MARKER in message) == (failure != 'hh_before_submit')
    if failure == 'captcha_after_dispatch':
        bot.confirm_post_dispatch_captcha.assert_called_once_with(False, 0)
        bot.submit_open_response_modal.assert_called_once()
        bot.confirm_response_submission.assert_not_called()
    elif failure not in ('tls', 'network_after_dispatch', 'driver_after_dispatch'):
        bot.application_ui_problem.assert_called_once_with('hh_response_error', 'Ошибка HH после клика')





def test_letter_field_reopened_after_form_rerender():
    """Поле письма пропало после перерисовки — бот раскрывает его и заполняет.

    Прогон 23.09: поле «уже открыто», ввод падает на stale element, поле
    сворачивается, и отклик уходит без письма.
    """
    import hh_selenium as m
    bot = m.HHSeleniumBot.__new__(m.HHSeleniumBot)
    state = {'finds': 0, 'reopened': 0}

    class Field:
        pass

    def find_fields():
        state['finds'] += 1
        # 1-й поиск: поле есть (но ввод в него упадёт), 2-й: пропало,
        # после раскрытия: снова есть.
        if state['finds'] == 1:
            return [Field()]
        return [Field()] if state['reopened'] else []

    def set_value(field, text):
        return state['finds'] > 1  # первая попытка — «протухшее» поле

    bot.find_cover_letter_fields = find_fields
    bot.set_text_input_value = set_value
    bot.find_response_modal = lambda wait_seconds=0: None
    bot.open_cover_letter_in_modal = lambda modal=None: state.__setitem__('reopened', 1)

    assert bot.fill_cover_letter('Здравствуйте!', wait_seconds=2) is True
    assert state['reopened'] == 1



def test_foreign_archived_card_does_not_block_response():
    """Пометка «Вакансия в архиве» у чужой карточки внизу — не повод отказаться.

    23.09: бот считал архивными живые вакансии, потому что искал фразу по
    тексту всей страницы, а внизу подгружалась архивная карточка другой
    вакансии. Отклики Beeline, iSpring, Sumitec срывались.
    """
    import hh_selenium as m
    bot = m.HHSeleniumBot.__new__(m.HHSeleniumBot)
    bot.is_response_limit_reached = lambda: False
    bot.get_visible_page_text = lambda: 'откликнуться ... вакансия в архиве ведущий специалист по экономике'
    bot.page_says_archived = lambda: False
    bot.page_has_captcha = lambda: False
    assert bot.get_response_blocker_message() is None

    bot.page_says_archived = lambda: True
    assert bot.get_response_blocker_message() == 'Вакансия в архиве'


def test_hidden_resume_recognized_before_typing_letter(monkeypatch):
    """«Поменяйте видимость резюме» распознаётся сразу, письмо не печатается.

    23.09 две серии по 6-7 откликов: hh временно прятал резюме, а бот 45 с
    тыкался в кнопку и писал «кнопка недоступна».
    """
    import hh_selenium as m
    from unittest.mock import MagicMock
    bot = m.HHSeleniumBot.__new__(m.HHSeleniumBot)
    modal = MagicMock()
    modal.text = ('Отклик на вакансию. Чтобы откликнуться на эту вакансию, поменяйте '
                  'видимость резюме на «Видно всем работодателям, зарегистрированным на hh.ru»')
    bot.find_response_modal = lambda wait_seconds=0: modal
    bot.fill_cover_letter = MagicMock()

    result = bot.submit_open_response_modal('Здравствуйте!', False)
    assert result[3] == m.HIDDEN_RESUME_MESSAGE
    bot.fill_cover_letter.assert_not_called()

    # Пауза после серии ждёт и сбрасывает счётчик.
    monkeypatch.setattr(m, 'HIDDEN_RESUME_PAUSE_SECONDS', 0)
    bot.check_interactive_controls = lambda: None
    bot.stop_requested = False
    bot.hidden_resume_streak = 2
    bot.wait_out_hidden_resume()
    assert bot.hidden_resume_streak == 0



def test_title_filter_knows_ib_and_whole_word_short_tokens():
    """Фильтр знает «ИБ» и смежные названия; короткие слова — только целым словом.

    23.09 «вакансии для резюме» теряли «Инженер по ИБ», «Методолог ИБ»,
    «технических средств защиты». «иб» при этом не должно ловить «гибрид».
    """
    import hh_selenium as m
    bot = m.HHSeleniumBot.__new__(m.HHSeleniumBot)
    bot.config = {'keywords_include': list(m.STRICT_TITLE_INCLUDE_KEYWORDS),
                  'keywords_exclude': ['менеджер'],
                  'allow_technical_fallback': False}
    ok = lambda t: bot.validate_security_title(t)[0]
    assert ok('Ведущий инженер по ИБ (DLP/KSC/KEDR/KSMG)')
    assert ok('Методолог ИБ (SGRC и внутренний контроль)')
    assert ok('Главный специалист отдела технических средств защиты')
    assert ok('Специалист по безопасной разработке')
    assert ok('Kubernetes Security Engineer')
    assert ok('SOC-аналитик L1')
    assert not ok('Бухгалтер (гибрид)')
    assert not ok('Associate product designer')


def test_title_filter_by_meaning():
    """ИБ по смыслу названия: «безопасн»/«защит»+ИТ-контекст, но не охрана труда и релейная защита."""
    import hh_selenium as m
    bot = m.HHSeleniumBot.__new__(m.HHSeleniumBot)
    bot.config = {'keywords_include': list(m.STRICT_TITLE_INCLUDE_KEYWORDS),
                  'keywords_exclude': [], 'allow_technical_fallback': False}
    ok = lambda t: bot.validate_security_title(t)[0]
    for title in ('Инженер защиты от сетевых атак', 'Специалист по сетевой безопасности',
                  'Главный специалист по иформационной безопасности',
                  'Специалист технической защиты персональных данных',
                  'Ведущий инженер (информационная безопасность)'):
        assert ok(title), title
    for title in ('Инженер релейной защиты и автоматики',
                  'Специалист по охране труда и промышленной безопасности',
                  'Инженер по пожарной безопасности', 'Агроном по защите растений',
                  'Специалист службы экономической безопасности'):
        assert not ok(title), title


def test_all_answered_questionnaire_is_submitted_despite_hint():
    """Все вопросы отвечены, а на странице «ответьте на вопрос» — всё равно отправляем.

    24.09 бот заполнял все поля анкеты и сдавался до нажатия «Откликнуться».
    """
    import hh_selenium as m
    from unittest.mock import MagicMock
    bot = m.HHSeleniumBot.__new__(m.HHSeleniumBot)
    modal = MagicMock()
    modal.text = 'Отклик на вакансию. Ответьте на вопросы работодателя.'
    bot.find_response_modal = lambda wait_seconds=0: modal
    bot.ensure_target_resume_selected = lambda modal: (True, None)
    bot.open_cover_letter_in_modal = lambda modal=None: False
    bot.fill_cover_letter = lambda *a, **k: False
    bot.letter_skip_reason = ''

    def answer(container=None):
        bot.unanswered_questions = []
        return 2
    bot.answer_employer_questions = answer
    bot.is_cover_letter_required = lambda: False
    bot.get_response_blocker_message = lambda: 'Не заполнены вопросы работодателя'
    bot.get_visible_page_text = lambda: 'ответьте на вопросы работодателя'
    bot.page_has_captcha = lambda: False
    bot.click_response_submit_button = MagicMock(return_value=True)
    bot.get_modal_validation_error = lambda modal: None

    submitted, _, answered, blocker = bot.submit_open_response_modal('Письмо', False)
    assert submitted is True and blocker is None and answered == 2
    bot.click_response_submit_button.assert_called_once()


def _chat_driver(chat_text):
    """Подставной браузер: страница вакансии с кнопкой «Чат», чат в iframe с полем ввода."""
    state = {'value': '', 'sent': [], 'chat': chat_text}
    field = MagicMock()
    field.is_displayed.return_value = True
    field.get_attribute.side_effect = lambda name: state['value'] if name == 'value' else None

    def type_text(*keys):
        from selenium.webdriver.common.keys import Keys
        if keys == (Keys.ENTER,):
            state['sent'].append(state['value'])
            state['chat'] += ' ' + state['value']
            state['value'] = ''
        else:
            state['value'] += ''.join(k for k in keys if isinstance(k, str) and len(k) > 1 or k.isprintable())
    field.send_keys.side_effect = type_text
    field.clear.side_effect = lambda: state.update(value='')

    visible = MagicMock()
    visible.is_displayed.return_value = True
    driver = MagicMock()
    driver.current_url = 'https://hh.ru/vacancy/1'

    def find_elements(by, selector):
        if 'text-input' in selector:
            return [field]
        return [visible]
    driver.find_elements.side_effect = find_elements
    body = MagicMock()
    type(body).text = property(lambda self: state['chat'])
    driver.find_element.return_value = body
    return driver, state


def test_letter_goes_to_chat_when_no_attach_button(bot, monkeypatch):
    """29.09 Кион и МТС Банк: кнопки «Приложить сопроводительное» нет — письмо уходит в чат одним сообщением."""
    import hh_selenium
    driver, state = _chat_driver('Отклик на вакансию Без сопроводительного письма')
    bot.driver = driver
    bot.click_element_with_mouse = MagicMock(return_value=True)
    monkeypatch.setattr(hh_selenium.time, 'sleep', lambda s: None)
    monkeypatch.setattr('selenium.webdriver.common.action_chains.ActionChains.perform', lambda self: None)
    letter = 'Здравствуйте! Откликаюсь на позицию DevOps.\n\nОпыт с Kubernetes и GitLab CI.'
    assert bot.send_letter_to_chat(letter, 'https://hh.ru/vacancy/1') is True
    assert len(state['sent']) == 1                     # одно сообщение, а не по строкам
    assert 'Kubernetes' in state['sent'][0]
    driver.switch_to.default_content.assert_called()   # из iframe вышли


def test_letter_not_repeated_if_already_in_chat(bot, monkeypatch):
    import hh_selenium
    letter = 'Здравствуйте! Откликаюсь на позицию DevOps.'
    driver, state = _chat_driver('Отклик на вакансию ' + letter)
    bot.driver = driver
    bot.click_element_with_mouse = MagicMock(return_value=True)
    monkeypatch.setattr(hh_selenium.time, 'sleep', lambda s: None)
    assert bot.send_letter_to_chat(letter, 'https://hh.ru/vacancy/1') is True
    assert state['sent'] == []


def test_captcha_in_background_opens_window_and_delay_comes_from_settings():
    """07.10: в фоне капча — перезапуск браузера с окном на той же странице; пауза 20–60 с из настроек."""
    from unittest.mock import MagicMock
    from hh_selenium import HHSeleniumBot
    bot = HHSeleniumBot.__new__(HHSeleniumBot)
    bot.headless = True
    bot.pause_before_close = True
    bot.driver = MagicMock(current_url='https://hh.ru/vacancy/1')
    old_driver = bot.driver
    bot.close_driver = MagicMock()
    new_driver = MagicMock()

    def init():
        bot.driver = new_driver
        return True
    bot.init_driver = init
    assert bot.show_browser_for_captcha() is True
    assert bot.headless is False and bot.pause_before_close is True
    bot.close_driver.assert_called_once()
    new_driver.get.assert_called_once_with('https://hh.ru/vacancy/1')
    assert old_driver is not new_driver

    bot.delay_between_vacancies = (1, 2)
    bot.config = {'apply_delay_seconds': [20, 60]}
    assert bot.apply_delay() == (20.0, 60.0)
    bot.config = {'apply_delay_seconds': 'abc'}
    assert bot.apply_delay() == (1, 2)


def test_letters_are_prepared_ahead_and_taken_by_vacancy_id(monkeypatch):
    """14.10: письма следующих вакансий пишутся в фоне, отклик забирает готовое по id."""
    from unittest.mock import MagicMock
    import hh_selenium
    from hh_selenium import HHSeleniumBot
    monkeypatch.setattr(hh_selenium, 'fetch_vacancy_text', lambda vid, timeout=15: f'описание {vid}')
    bot = HHSeleniumBot.__new__(HHSeleniumBot)
    bot.config = {'letter_prefetch': 2, 'ai_config': {'enabled': True}}
    bot.applied_vacancies = {'3': {'status': 'sent'}}
    bot.ai_assistant = MagicMock()
    bot.ai_assistant.generate_cover_letter.side_effect = lambda **kw: 'письмо: ' + kw['vacancy_description']
    vacancies = [{'id': i, 'name': f'V{i}', 'employer': {'name': 'E'}} for i in (1, 2, 3, 4)]
    bot.prefetch_letters(vacancies, 1)            # следующие 2: id 2 и 3 (3 уже откликались)
    assert set(bot._prefetched) == {'2'}
    job = bot.take_prefetched_letter('https://hh.ru/vacancy/2')
    assert job.result(timeout=5) == 'письмо: описание 2'
    assert bot.take_prefetched_letter('https://hh.ru/vacancy/2') is None
    bot.config['letter_prefetch'] = 0
    bot._prefetched.clear()
    bot.prefetch_letters(vacancies, 0)
    assert bot._prefetched == {}


def test_login_form_on_one_vacancy_skips_and_three_in_row_stop():
    """14.10: форма входа на одной вакансии при живой сессии — пропуск, а не стоп всего прогона."""
    import hh_selenium
    bot = hh_selenium.HHSeleniumBot.__new__(hh_selenium.HHSeleniumBot)
    bot.stop_requested = False
    bot.driver = MagicMock()
    form = MagicMock(tag_name='form')
    form.is_displayed.return_value = True
    bot.driver.find_elements.return_value = [form]
    for name, value in [('check_interactive_controls', None), ('maybe_bump_resume', None),
                        ('random_delay', None), ('check_login', True)]:
        setattr(bot, name, MagicMock(return_value=value))
    for _ in range(2):
        assert bot.apply_to_vacancy('https://hh.ru/vacancy/1', 'x') == (False, hh_selenium.LOGIN_FORM_SKIP_MESSAGE)
        assert not bot.stop_requested
    success, message = bot.apply_to_vacancy('https://hh.ru/vacancy/1', 'x')
    assert not success and 'снова требует вход' in message and bot.stop_requested

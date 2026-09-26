import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from unittest.mock import MagicMock, patch
from hh_selenium import HHSeleniumBot


@pytest.fixture
def bot(monkeypatch):
    # Создаем бота без запуска драйвера. Целевое резюме задаём явно: раньше
    # тесты брали его из настроек автора и на новой машине падали.
    import config_manager
    monkeypatch.setattr(config_manager, 'get_active_resume',
                        lambda: ('test-id', 'Backend Engineer / Backend-инженер'))
    bot = HHSeleniumBot(headless=True)
    bot.driver = MagicMock()
    return bot


@pytest.fixture
def disallowed():
    """Резюме другой профессии на том же аккаунте, с которого откликаться нельзя."""
    with patch("config_manager.load_config", return_value={"disallowed_resume_keywords": ["дизайнер"]}):
        yield


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
    title_elem.text = "Backend Engineer / Backend-инженер"
    modal.find_elements.return_value = [title_elem]

    ok, err = bot.ensure_target_resume_selected(modal)
    assert ok is True
    assert err is None


def test_ensure_target_resume_switches_from_disallowed(bot, disallowed):
    modal = MagicMock()
    # Элемент текущего резюме в модалке
    bad_title = MagicMock()
    bad_title.is_displayed.return_value = True
    bad_title.text = "Дизайнер интерьеров"
    modal.find_elements.return_value = [bad_title]

    # Элементы в выпадающем списке выбора резюме
    target_option = MagicMock()
    target_option.is_displayed.return_value = True
    target_option.text = "Backend Engineer / Backend-инженер"

    bot.click_element_with_mouse = MagicMock(return_value=True)
    bot.driver.find_elements.return_value = [target_option]

    ok, err = bot.ensure_target_resume_selected(modal)
    assert ok is True
    assert err is None
    # Должен был кликнуть дважды: сначала открыть список, потом выбрать целевое резюме
    assert bot.click_element_with_mouse.call_count == 2


def test_ensure_target_resume_blocks_disallowed_if_not_switched(bot, disallowed):
    modal = MagicMock()
    bad_title = MagicMock()
    bad_title.is_displayed.return_value = True
    bad_title.text = "Дизайнер интерьеров"
    modal.find_elements.return_value = [bad_title]

    bot.click_element_with_mouse = MagicMock(return_value=True)
    bot.driver.find_elements.return_value = []  # Опций нет

    ok, err = bot.ensure_target_resume_selected(modal)
    assert ok is False
    assert "активно нецелевое резюме" in err.lower()


def test_apply_to_vacancy_letter_sent_initialized(bot):
    bot.page_has_captcha = MagicMock(return_value=False)
    bot.driver.get = MagicMock()
    bot.detect_response_state = MagicMock(return_value='ready')
    bot.get_vacancy_page_company = MagicMock(return_value="Test Corp")
    bot.get_vacancy_page_description = MagicMock(return_value="DevOps engineer needed")
    bot.get_vacancy_page_skills = MagicMock(return_value=["Python", "Backend"])
    bot.is_response_limit_reached = MagicMock(return_value=False)
    bot.handle_warning_popups = MagicMock(return_value=False)
    bot.click_element_with_mouse = MagicMock(return_value=True)

    apply_btn = MagicMock()
    apply_btn.text = "Откликнуться"
    apply_btn.is_displayed.return_value = True
    bot.driver.find_elements.return_value = [apply_btn]

    # submit_open_response_modal returns submitted=True, letter_sent=True, answered=0, blocker=None
    bot.submit_open_response_modal = MagicMock(return_value=(True, True, 0, None))
    bot.confirm_response_submission = MagicMock(return_value=(True, "С письмо"))

    success, message = bot.apply_to_vacancy("https://hh.ru/vacancy/123456", "Backend Engineer")
    assert success is True
    assert message == "С письмо"
    bot.submit_open_response_modal.assert_called_once()
    # Ensure letter_sent argument was passed as False initially
    call_args = bot.submit_open_response_modal.call_args[0]
    assert call_args[1] is False





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



def test_title_filter_whole_word_short_tokens():
    """Короткие слова-исключения ловятся только целым словом: «hr» не внутри «Chrome»."""
    import hh_selenium as m
    bot = m.HHSeleniumBot.__new__(m.HHSeleniumBot)
    bot.config = {'keywords_include': ['python', 'django'], 'keywords_exclude': ['hr', 'менеджер']}
    ok = lambda t: bot.validate_title(t)[0]
    assert ok('Python-разработчик (Chrome Extensions)')
    assert ok('Django Developer (гибрид)')
    assert not ok('HR Python')
    assert not ok('Менеджер проектов Python')
    assert not ok('Бухгалтер (гибрид)')


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
    bot.click_response_submit_button = MagicMock(return_value=True)
    bot.get_modal_validation_error = lambda modal: None

    submitted, _, answered, blocker = bot.submit_open_response_modal('Письмо', False)
    assert submitted is True and blocker is None and answered == 2
    bot.click_response_submit_button.assert_called_once()

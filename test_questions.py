# -*- coding: utf-8 -*-
"""Проверки отклика на вакансии с тестом/анкетой работодателя.

Запуск: python test_questions.py

Ветки отправки и заполнения анкеты проверяются на подставном драйвере,
без реальных вакансий и переписки.
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hh_selenium import HHSeleniumBot, QUESTIONS_SKIP_MARKER


def _bot(config=None):
    """Бот без браузера и без __init__ — нужны только методы."""
    b = HHSeleniumBot.__new__(HHSeleniumBot)
    b.config = config or {}
    b.unanswered_questions = []
    b.letter_skip_reason = ''
    b.ai_assistant = None
    return b


def test_custom_answer_from_config():
    """Ответ из конфига подбирается по ключевому слову в вопросе."""
    b = _bot({'answer_policy': {'yes_to_conditions': False}, 'question_answers': {
        'опыт работы': 'Применял SIEM для мониторинга.',
        'готовы к переезду': 'Нет',
    }})
    assert b.get_answer_for_question('Какой у вас опыт работы в ИБ?') == 'Применял SIEM для мониторинга.'
    assert b.get_answer_for_question('Готовы к переезду в Москву?') == 'Нет'
    # регистр вопроса роли не играет
    assert b.get_answer_for_question('ОПЫТ РАБОТЫ?') == 'Применял SIEM для мониторинга.'


def test_unknown_question_is_not_guessed():
    """На незнакомый вопрос ответа нет — вакансия пропускается, а не заполняется мусором.

    Отправить работодателю выдуманный ответ хуже, чем не откликнуться: это
    попадёт в его анкету и останется там навсегда.
    """
    b = _bot({'question_answers': {'опыт работы': '6 лет'}})
    assert b.get_answer_for_question('Назовите девичью фамилию вашей матери') is None
    assert b.get_answer_for_question('') is None
    assert b.get_answer_for_question(None) is None


def test_generic_ai_answer_rejected():
    """Дежурная отписка ИИ («не могу ответить») ответом не считается."""
    class DummyAI:
        def __init__(self, text): self.text = text
        def answer_question(self, q, question_type=None): return self.text

    b = _bot()
    b.ai_assistant = DummyAI('Извините, я не могу ответить на этот вопрос')
    assert b.get_answer_for_question('Сколько будет 2+2?') is None

    b.ai_assistant = DummyAI('Да, имею опыт работы с Kubernetes более 3 лет')
    assert b.get_answer_for_question('Есть опыт с Kubernetes?') is not None


def test_ai_exception_does_not_break_apply():
    """Падение ИИ не роняет отклик — просто нет ответа."""
    class BrokenAI:
        def answer_question(self, q, question_type=None):
            raise RuntimeError('quota exceeded')

    b = _bot()
    b.ai_assistant = BrokenAI()
    assert b.get_answer_for_question('Любой вопрос?') is None


def test_unanswered_questions_reported_to_user():
    """Пропуск по неотвеченным вопросам объясняется человеческим языком."""
    b = _bot()
    b.unanswered_questions = ['Сколько лет опыта с SIEM Positive Technologies?']
    msg = b.describe_unanswered_questions()
    assert QUESTIONS_SKIP_MARKER in msg
    assert 'SIEM' in msg, f'вопрос не показан пользователю: {msg}'

    b.unanswered_questions = ['Вопрос 1', 'Вопрос 2', 'Вопрос 3']
    msg3 = b.describe_unanswered_questions()
    assert 'Вопрос 1' in msg3
    # остальные схлопнуты в счётчик, а не вывалены простынёй
    assert '2' in msg3 or '3' in msg3


def test_success_message_two_variants():
    """Итог отклика различает «с письмом» и «без письма», во втором — причина."""
    b = _bot()
    assert 'С сопроводительным письмом' in b.build_success_message(True, 0)

    b.letter_skip_reason = 'отклик шёл через страницу-анкету, поля письма в ней не было'
    msg = b.build_success_message(False, 0)
    assert 'БЕЗ письма' in msg
    assert 'страницу-анкету' in msg, f'причина потеряна: {msg}'

    # число отвеченных вопросов попадает в итог
    with_q = b.build_success_message(True, 3)
    assert '3' in with_q


def test_success_message_never_silent_about_reason():
    """Даже если причину не выставили, в итоге не пустота."""
    b = _bot()
    b.letter_skip_reason = ''
    msg = b.build_success_message(False, 0)
    assert 'БЕЗ письма' in msg
    assert msg.rstrip().endswith(('определена', 'определена.')) or len(msg) > len('Отклик отправлен БЕЗ письма: ')


def test_questionnaire_page_branch():
    """Вакансия с анкетой открывается отдельной страницей, без модалки.

    Ветка обязана: ответить на вопросы, отметить причину отсутствия письма
    и нажать кнопку отклика на самой странице.
    """
    b = _bot()
    calls = {'answered': 0, 'clicked': False}

    b.find_response_modal = lambda wait_seconds=2: None
    b.ensure_target_resume_selected = lambda modal: (True, None)

    def fake_answer(container=None):
        calls['answered'] = 2
        b.unanswered_questions = []
        return 2
    b.answer_employer_questions = fake_answer

    def fake_click():
        calls['clicked'] = True
        return True
    b.click_lowest_visible_apply_button = fake_click

    ok, letter_sent, answered, err = b.submit_open_response_modal('текст письма', False)

    assert ok is True, f'отклик через анкету не прошёл: {err}'
    assert answered == 2
    assert calls['clicked'], 'кнопка отклика на странице-анкете не нажата'
    assert b.letter_skip_reason, 'не объяснено, почему отклик ушёл без письма'
    assert 'анкет' in b.letter_skip_reason.lower()


def test_questionnaire_blocks_apply_when_unanswered():
    """Есть неотвеченный вопрос — отклик не отправляется.

    Отправить анкету с пустым обязательным полем значит засветиться у
    работодателя с недозаполненной формой.
    """
    b = _bot()
    b.find_response_modal = lambda wait_seconds=2: None
    clicked = {'yes': False}

    def fake_answer(container=None):
        b.unanswered_questions = ['Сколько лет вы работали с КИИ?']
        return 0
    b.answer_employer_questions = fake_answer

    def fake_click():
        clicked['yes'] = True
        return True
    b.click_lowest_visible_apply_button = fake_click

    ok, letter_sent, answered, err = b.submit_open_response_modal('текст', False)

    assert ok is False, 'отклик ушёл с неотвеченным вопросом'
    assert not clicked['yes'], 'кнопка нажата, хотя вопрос без ответа'
    assert err and 'КИИ' in err, f'пользователю не показано, что именно осталось без ответа: {err}'


def test_neutral_fallback_when_configured():
    """Решение пользователя: на неизвестный вопрос отвечаем нейтрально.

    Без neutral_answer в конфиге поведение прежнее — вакансия пропускается.
    """
    b = _bot({'question_answers': {}})
    assert b.get_answer_for_question('Какой ваш любимый цвет?') is None

    b = _bot({'question_answers': {},
              'neutral_answer': 'Готов обсудить этот вопрос на собеседовании'})
    ans = b.get_answer_for_question('Какой ваш любимый цвет?')
    assert ans == 'Готов обсудить этот вопрос на собеседовании'
    # На вопрос о зарплате действует нейтральная политика условий, даже при готовом ответе.
    b.config['question_answers'] = {'зарплат': 'Готов обсудить, какая вилка у позиции?'}
    assert b.get_answer_for_question('Ваши зарплатные ожидания?') == 'Готов обсудить условия на следующем этапе.'
    b.config['question_answers'] = {'зарплат': 'от 180 000 руб.'}
    assert not any(ch.isdigit() for ch in b.get_answer_for_question('Ваши зарплатные ожидания?'))


def test_big_questionnaire_costs_one_ai_request():
    """Анкета на 50 вопросов уходит модели одним запросом, а не пятьюдесятью.

    У Gemini free tier 20 запросов в сутки на модель. Поштучный опрос выжигает
    квоту на первой же такой вакансии, и остаток анкеты получает один дежурный
    ответ — работодатель видит полсотни одинаковых фраз.
    """
    class FakeBlock:
        def __init__(self, t): self.text = t
        def find_elements(self, by, sel): return []

    class FakeAI:
        enabled = True
        def __init__(self): self.batch_calls = 0; self.single_calls = 0
        def answer_questions_batch(self, qs):
            self.batch_calls += 1
            return {q['text']: f'ответ {i}' for i, q in enumerate(qs)}
        def answer_question(self, *a, **k):
            self.single_calls += 1
            return 'поштучный ответ'

    b = _bot({'question_answers': {}})
    b.ai_assistant = FakeAI()
    blocks = [FakeBlock(f'Открытый вопрос номер {i}') for i in range(50)]

    b.prefetch_batch_answers(blocks, {})
    assert b.ai_assistant.batch_calls == 1, 'анкета должна уходить одним запросом'

    for i in range(50):
        assert b.get_answer_for_question(f'Открытый вопрос номер {i}')
    assert b.ai_assistant.single_calls == 0, (
        f'модель дёрнули поштучно {b.ai_assistant.single_calls} раз')


def test_multi_checkbox_marks_everything_model_named():
    """«Отметьте всё, чем владеете» — ставятся все названные галочки, и только они."""
    class El:
        def __init__(self, label): self.label = label; self.id = id(self); self.sel = False
        def is_selected(self): return self.sel
        def is_displayed(self): return True
        def get_attribute(self, key):
            return {'id': str(self.id), 'type': 'checkbox', 'value': self.label}.get(key, '')

    class Block:
        def __init__(self, text, boxes): self.text = text; self._c = [El(x) for x in boxes]
        def find_elements(self, by, sel):
            return self._c if 'checkbox' in sel else []

    b = _bot({'question_answers': {}})
    b.driver = SimpleNamespace(execute_script=lambda *args: None)
    b.get_option_label = lambda el: el.label
    clicked = []
    def click(el):
        el.sel = True; clicked.append(el.label); return True
    b.click_element_with_mouse = click

    q = 'Отметьте инструменты, с которыми работали'
    blk = Block(q, ['Burp Suite', 'Nmap', '1С Бухгалтерия', 'Metasploit', 'Photoshop'])
    b._batch_answers = {q: 'Burp Suite | Nmap | Metasploit'}

    answered, unresolved = b.answer_single_question(blk, q, {}, set())
    assert unresolved is None, f'вопрос остался без ответа: {unresolved}'
    assert answered == 3, f'отмечено {answered} галочек вместо 3'
    assert sorted(clicked) == ['Burp Suite', 'Metasploit', 'Nmap'], clicked


if __name__ == '__main__':
    failed = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith('test_'):
            continue
        try:
            fn()
            print('[OK]', name)
        except AssertionError as e:
            failed += 1
            print('[X] ', name, '->', e)
        except Exception as e:
            failed += 1
            print('[X] ', name, '-> неожиданная ошибка:', type(e).__name__, e)
    print('Провалено проверок:', failed) if failed else print('Все проверки пройдены.')
    sys.exit(1 if failed else 0)

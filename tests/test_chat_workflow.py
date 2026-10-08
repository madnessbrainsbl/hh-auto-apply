import inspect
import json
import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from rejection_analyzer import RejectionAnalyzer


@pytest.fixture
def bot(tmp_path, monkeypatch):
    import chat_workflow
    monkeypatch.setattr(chat_workflow.time, 'sleep', lambda _: None)
    analyzer = RejectionAnalyzer.__new__(RejectionAnalyzer)
    analyzer.config = {'chat_autoreply': {'enabled': True, 'max_per_run': 0}}
    analyzer.ai_assistant = SimpleNamespace(enabled=False, candidate_profile={})
    analyzer.FOLLOWUP_WAIT_SECONDS = 0   # ожидание ответа работодателя проверяется отдельным тестом
    analyzer.headless = True
    analyzer.driver = SimpleNamespace(current_url='https://hh.ru/chat', window_handles=['main'])
    analyzer._user_closed = False
    analyzer.is_driver_alive = lambda: True
    analyzer._chat_state_path = lambda: str(tmp_path / 'chat_actions.json')
    analyzer.find_chat_message_input = Mock(return_value=object())
    analyzer.send_chat_reply = Mock(return_value=True)
    analyzer._current_chat_matches = Mock(return_value=True)
    return analyzer


def snapshot(message='К сожалению, сейчас не готовы пригласить вас на следующий этап.'):
    return {
        'identity': 'chat:42', 'vacancy_title': 'AppSec', 'company_name': 'Test',
        'chat_url': 'https://hh.ru/chat/42', 'vacancy_url': 'https://hh.ru/vacancy/123',
        'messages': [{'text': message, 'isOut': False, 'id': 'm1'}],
    }


def recruiter_snapshot(question='Готовы ли вы оформить допуск по третьей форме?', options=('Да', 'Нет')):
    chat = snapshot(question)
    chat['messages'][0]['isBot'] = True
    chat['choice_options'] = [{'index': i, 'label': label, 'disabled': False}
                              for i, label in enumerate(options)]
    return chat


def test_recruiter_choices_use_buttons_not_generated_messages(bot):
    chat = recruiter_snapshot()
    bot.ai_assistant = SimpleNamespace(enabled=False, answer_question=Mock(return_value=0))
    bot.compose_chat_reply = Mock(return_value='Длинный ответ вместо выбора')
    bot._click_chat_choice = Mock(return_value=True)
    bot._read_open_chat = Mock(return_value=recruiter_snapshot('Ваши ответы отправлены работодателю.', ()))
    bot.find_chat_message_input.return_value = None
    bot._handle_chat(chat)
    bot._click_chat_choice.assert_called_once()
    assert bot._click_chat_choice.call_args.args[1]['label'] == 'Да'
    bot.compose_chat_reply.assert_not_called()
    bot.send_chat_reply.assert_not_called()
    assert next(iter(bot._load_chat_actions().values()))['status'] == 'sent'


def test_recruiter_advances_through_multiple_questions_with_same_answer(bot):
    bot.ai_assistant = SimpleNamespace(answer_question=Mock(return_value=0))
    bot.compose_chat_reply = Mock()
    bot._click_chat_choice = Mock(return_value=True)
    bot._read_open_chat = Mock(side_effect=[recruiter_snapshot('Готовы работать удалённо?'),
                                           recruiter_snapshot('Ваши ответы переданы работодателю.', ())])
    chat = recruiter_snapshot()
    chat['messages'].insert(0, {'text': 'Да', 'isOut': True})
    bot._handle_chat(chat)
    assert bot._click_chat_choice.call_count == 2
    assert len(bot._load_chat_actions()) == 2
    assert all(a['status'] == 'sent' for a in bot._load_chat_actions().values())
    bot.compose_chat_reply.assert_not_called()
    bot.send_chat_reply.assert_not_called()


def test_unconfirmed_recruiter_click_is_not_repeated_or_replaced_with_text(bot):
    bot.ai_assistant = SimpleNamespace(answer_question=Mock(return_value=0))
    bot.compose_chat_reply = Mock()
    bot._click_chat_choice = Mock(return_value=False)
    chat = recruiter_snapshot()
    bot._handle_chat(chat)
    bot._handle_chat(chat)
    bot._click_chat_choice.assert_called_once()
    assert next(iter(bot._load_chat_actions().values()))['status'] == 'pending_confirmation'
    bot.compose_chat_reply.assert_not_called()
    bot.send_chat_reply.assert_not_called()


@pytest.mark.parametrize('index', [None, 'Да', 10, True])
def test_invalid_recruiter_option_never_falls_back_to_text(bot, index):
    bot.ai_assistant = SimpleNamespace(answer_question=Mock(return_value=index))
    bot.compose_chat_reply = Mock()
    bot._click_chat_choice = Mock()
    bot._handle_chat(recruiter_snapshot())
    bot._click_chat_choice.assert_not_called()
    bot.compose_chat_reply.assert_not_called()
    bot.send_chat_reply.assert_not_called()


def test_human_chat_suggestions_are_not_recruiter_options(bot):
    chat = recruiter_snapshot('Как применяли Python?', ('Здравствуйте!', 'Есть профильный опыт'))
    chat['messages'][-1]['isBot'] = False
    bot.compose_chat_reply = Mock(return_value='Применял Python для проверки API.')
    bot._click_chat_choice = Mock()
    bot._handle_chat(chat)
    bot._click_chat_choice.assert_not_called()
    bot.send_chat_reply.assert_called_once()


@pytest.mark.parametrize('date', ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс', 'Пт.', 'Fri', 'пт, 12:30'])
def test_card_fallback_does_not_treat_weekday_as_employer(date):
    from chat_workflow import chat_card_names
    names = chat_card_names({'text': f'Сетевой инженер\n{date}\nАгрохолдинг Просторы\nОтклик на вакансию'})
    assert names == {'vacancy_title': 'Сетевой инженер', 'company_name': 'Агрохолдинг Просторы'}


def test_rendered_emphasis_confirms_reply_without_losing_numbers():
    from chat_workflow import confirms_reply
    reply = '1. Есть **опыт Python**.\n2. Зарплата **200 000 рублей**.'
    message = {'isOut': True, 'raw_text': '1. Есть опыт Python.\n2. Зарплата 200 000 рублей.'}
    assert confirms_reply(message, reply)
    assert not confirms_reply(dict(message, raw_text=message['raw_text'].replace('200 000', '100 000')), reply)
    assert not confirms_reply(dict(message, pending=True), reply)
    assert not confirms_reply(dict(message, failed=True), reply)
    assert not confirms_reply(dict(message, isOut=False), reply)
    assert not confirms_reply({'isOut': True, 'raw_text': 'Портфолио'}, '[Портфолио](https://example.org/private)')


def test_chat_history_retries_temporary_windows_lock(bot, monkeypatch):
    import chat_workflow
    replace = chat_workflow.os.replace
    calls = []

    def temporarily_locked(source, target):
        calls.append(target)
        if len(calls) < 3:
            raise PermissionError(13, 'File is temporarily locked')
        return replace(source, target)

    monkeypatch.setattr(chat_workflow.os, 'replace', temporarily_locked)
    assert bot._save_chat_action('reply', {'kind': 'answer', 'status': 'pending_confirmation'})
    assert len(calls) == 3
    assert not getattr(bot, '_chat_actions_blocked', False)
    with open(bot._chat_state_path(), encoding='utf-8') as stream:
        assert json.load(stream)['actions']['reply']['status'] == 'pending_confirmation'


def test_permanent_chat_history_failure_preserves_history_and_blocks_send(bot, monkeypatch):
    import chat_workflow
    assert bot._save_chat_action('old', {'kind': 'answer', 'status': 'sent'})
    monkeypatch.setattr(chat_workflow.os, 'replace', Mock(side_effect=PermissionError(13, 'Access denied')))
    assert not bot._save_chat_action('new', {'kind': 'answer', 'status': 'pending_confirmation'})
    assert bot._chat_actions_blocked is True
    assert not bot._send_chat_action('answer', snapshot(), 'Ответ работодателю')
    bot.send_chat_reply.assert_not_called()
    with open(bot._chat_state_path(), encoding='utf-8') as stream:
        assert list(json.load(stream)['actions']) == ['old']


def test_scan_stops_if_chat_history_cannot_be_saved(bot):
    bot._chat_actions_blocked = True
    bot._set_unread_filter = Mock()
    bot._scroll_chat_list = Mock(return_value={'moved': False})
    bot._read_chat_cards = Mock(return_value=[])
    bot._capture_chat_diagnostics = Mock()
    bot.process_unread_messenger_chats()
    assert bot.messenger_summary['blocked'] is True
    assert bot.messenger_summary['complete'] is False
    bot._read_chat_cards.assert_not_called()
    bot.send_chat_reply.assert_not_called()


def test_scan_has_no_implicit_50_or_60_cap():
    assert inspect.signature(RejectionAnalyzer.process_unread_messenger_chats).parameters['max_chats'].default == 0
    assert inspect.signature(RejectionAnalyzer.drain_unread_dialogs).parameters['max_dialogs'].default == 0


def test_all_65_unread_chats_visited_including_invites_and_questions(bot):
    cards = [{'idx': i, 'key': f'chat:{i}', 'text': f'Role {i}\nCompany\nСобеседование'} for i in range(65)]
    bot._set_unread_filter = Mock(return_value=True)
    bot._scroll_chat_list = Mock(return_value={'moved': False})
    bot._read_chat_cards = Mock(return_value=cards)
    bot._open_chat_card = Mock(side_effect=lambda c: dict(snapshot('Есть ли опыт?'), identity=c['key']))
    bot._handle_chat = Mock(return_value=None)
    bot._back_to_chat_list = Mock(return_value=True)
    bot._mark_chat_read = Mock()
    bot.process_unread_messenger_chats()
    assert bot._handle_chat.call_count == 65
    assert bot.messenger_summary['viewed'] == 65
    assert bot.messenger_summary['complete'] is True
    assert all(call.args == (True,) for call in bot._set_unread_filter.call_args_list)


def test_missing_unread_filter_blocks_before_opening_read_chats(bot):
    bot._set_unread_filter = Mock(return_value=False)
    bot._read_chat_cards = Mock()
    bot._open_chat_card = Mock()
    bot._capture_chat_diagnostics = Mock()
    bot.process_unread_messenger_chats()
    assert bot.messenger_summary['blocked'] is True
    assert 'Только непрочитанные' in bot.messenger_summary['reason']
    bot._read_chat_cards.assert_not_called()
    bot._open_chat_card.assert_not_called()


def test_filter_lost_after_return_never_opens_the_next_chat(bot):
    cards = [{'idx': i, 'key': str(i), 'text': 'Role\nTest'} for i in range(2)]
    prepare_scan(bot, cards, Mock(return_value=snapshot()))
    bot._set_unread_filter.side_effect = [True, False]
    bot._capture_chat_diagnostics = Mock()
    bot.process_unread_messenger_chats()
    assert bot.messenger_summary['viewed'] == 1
    assert bot.messenger_summary['blocked'] is True
    bot._open_chat_card.assert_called_once()


def test_unread_fetch_never_opens_old_rejection_pages(bot):
    refusal = snapshot()
    bot.process_unread_messenger_chats = Mock(return_value=[refusal])
    bot.messenger_summary = {'complete': True}
    bot.goto = Mock()
    assert bot.fetch_rejected_chats() == [refusal]
    bot.process_unread_messenger_chats.assert_called_once_with(max_chats=0)
    bot.goto.assert_not_called()


def test_scrolling_continues_past_two_pages_without_new_targets(bot):
    first = {'idx': 0, 'key': 'chat:1', 'text': 'Role\nCompany'}
    last = {'idx': 1, 'key': 'chat:65', 'text': 'Last\nCompany'}
    bot._set_unread_filter = Mock(return_value=True)
    bot._scroll_chat_list = Mock(side_effect=[{'moved': True}] * 5 + [{'moved': False}] * 4)
    bot._read_chat_cards = Mock(side_effect=[[first]] * 4 + [[last]] * 4)
    bot._open_chat_card = Mock(side_effect=lambda c: dict(snapshot(), identity=c['key']))
    bot._handle_chat = Mock(return_value=None)
    bot._back_to_chat_list = Mock(return_value=True)
    bot._mark_chat_read = Mock()
    bot.process_unread_messenger_chats()
    assert bot._handle_chat.call_count == 2


def test_rejection_reason_asked_once_even_after_restart(bot):
    chat = snapshot()
    bot._ask_rejection_reason(chat)
    bot._ask_rejection_reason(chat)
    bot.__dict__.pop('_chat_actions', None)
    bot._ask_rejection_reason(snapshot('Ваш профиль не подходит.'))
    assert bot.send_chat_reply.call_count == 1
    assert 'причин' in bot.send_chat_reply.call_args.args[0]


def test_rejection_reason_guard_uses_original_turn_with_repeated_lines(bot):
    chat = snapshot('Здравствуйте!\nК сожалению, мы не готовы пригласить вас.')
    chat['messages'].append({'text': 'Здравствуйте!\nСпасибо за интерес к вакансии.', 'isOut': False})
    bot._read_open_chat = Mock(return_value=chat)
    bot._current_chat_matches = RejectionAnalyzer._current_chat_matches.__get__(bot)
    assert bot._ask_rejection_reason(chat) is True
    bot.send_chat_reply.assert_called_once()
    assert next(iter(bot._load_chat_actions().values()))['status'] == 'sent'


def test_rejection_reason_guard_still_blocks_a_new_incoming_message(bot):
    chat = snapshot()
    current = snapshot('Подождите, пожалуйста, решение по отклику ещё не принято.')
    bot._read_open_chat = Mock(return_value=current)
    bot._current_chat_matches = RejectionAnalyzer._current_chat_matches.__get__(bot)
    assert bot._ask_rejection_reason(chat) is False
    bot.send_chat_reply.assert_not_called()
    assert next(iter(bot._load_chat_actions().values()))['status'] == 'deferred'


def test_rejection_with_disabled_composer_is_not_recorded_as_sent(bot):
    bot.find_chat_message_input.return_value = None
    bot._ask_rejection_reason(snapshot())
    bot.send_chat_reply.assert_not_called()
    state = bot._load_chat_actions()
    assert not any(a['status'] == 'sent' for a in state.values())


def test_uncertain_send_not_retried_and_existing_manual_reply_respected(bot):
    bot.send_chat_reply.return_value = False
    bot._ask_rejection_reason(snapshot())
    bot._ask_rejection_reason(snapshot())
    assert bot.send_chat_reply.call_count == 1
    assert next(iter(bot._load_chat_actions().values()))['status'] == 'pending_confirmation'
    bot.send_chat_reply.reset_mock()
    chat = snapshot()
    chat['identity'] = 'chat:other'
    chat['messages'].append({'text': 'Подскажите, почему отказали?', 'isOut': True})
    bot._ask_rejection_reason(chat)
    bot.send_chat_reply.assert_not_called()


def test_repeated_question_deduplicated_but_new_question_answered(bot):
    bot.compose_chat_reply = Mock(return_value='Здравствуйте! Есть смежный опыт с Python. Готов обсудить задачи.')
    chat = snapshot('Есть ли опыт ELMA365?')
    bot._handle_chat(chat, 'Собеседование')
    bot._handle_chat(chat, 'Собеседование')
    assert bot.send_chat_reply.call_count == 1
    new = snapshot('Какие зарплатные ожидания?')
    new['messages'][0]['id'] = 'm2'
    bot._handle_chat(new, 'Собеседование')
    assert bot.send_chat_reply.call_count == 2


def test_finished_recruiter_form_does_not_answer_echoed_questions_again(bot):
    chat = snapshot('На каких игровых проектах вы работали?')
    chat['messages'].extend([
        {'text': 'Есть смежный опыт.', 'isOut': True},
        {'text': 'На каких игровых проектах вы работали? Есть смежный опыт.', 'isOut': False, 'isBot': True},
        {'text': 'Спасибо! Ваши ответы отправлены работодателю. Он напишет в этом чате.',
         'isOut': False, 'isBot': True},
    ])
    bot.compose_chat_reply = Mock(return_value='Не следует отправлять')
    bot._handle_chat(chat)
    bot.compose_chat_reply.assert_not_called()
    bot.send_chat_reply.assert_not_called()
    chat['messages'].append({'text': 'Какие зарплатные ожидания?', 'isOut': False})
    bot._handle_chat(chat)
    bot.compose_chat_reply.assert_called_once()
    bot.send_chat_reply.assert_called_once()


def test_external_interview_is_queued_not_called_an_ad_or_completed(bot):
    link = 'https://telegram.me/Giga_recruiter_bot?start=example'
    bot._open_external_interview = Mock(return_value=True)
    bot.find_chat_message_input.return_value = None
    chat = snapshot('Пройдите короткое первичное интервью с ГигаРекрутером: ' + link)
    bot._handle_chat(chat, 'Собеседование')
    bot._handle_chat(chat, 'Собеседование')
    bot._open_external_interview.assert_called_once_with(link)
    bot.send_chat_reply.assert_not_called()
    action = next(iter(bot._load_chat_actions().values()))
    assert action['status'] == 'external_interview_required'
    assert action['url'] == link


def test_unknown_or_spoofed_external_links_not_auto_opened(bot):
    bot._open_external_interview = Mock()
    for link in ('https://telegram.me.evil.test/Giga_recruiter_bot',
                 'http://telegram.me/Giga_recruiter_bot',
                 'https://evil.test/interview',
                 'https://telegram.me/another_bot'):
        bot._handle_chat(snapshot('Пройдите интервью: ' + link), 'Собеседование')
    bot._open_external_interview.assert_not_called()


def test_chat_prompt_uses_profile_conditions_and_covers_each_question(bot):
    prompts = {}
    def call(prompt, system_prompt, validate=None):
        prompts.update(user=prompt, system=system_prompt)
        return 'Здравствуйте! Готов обсудить задачи и показать применимый опыт.'
    bot.ai_assistant = SimpleNamespace(enabled=True, _call_llm=call, candidate_profile={
        'location': 'Калининград', 'work_format': 'удалённо',
        'expected_salary': '200 000 руб.', 'skills': ['Python', 'JavaScript'],
    })
    bot.compose_chat_reply('1. Где живёте? 2. Есть опыт ELMA365? 3. Custom UI? 4. Ожидания?', 'Developer', 'Test')
    assert 'Калининград' in prompts['user'] and '200 000' not in prompts['user']
    assert 'удалённо' in prompts['user']
    assert 'живу в Сибири' not in prompts['system']
    assert 'по пунктам' in prompts['system']
    assert 'отсутствие упоминания' in prompts['system'].lower()


def test_corrupt_action_history_blocks_sends_instead_of_duplicate(bot):
    from pathlib import Path
    Path(bot._chat_state_path()).write_text('{broken', encoding='utf-8')
    bot._ask_rejection_reason(snapshot())
    bot.send_chat_reply.assert_not_called()


def test_response_to_new_question_after_old_rejection(bot):
    chat = snapshot()
    chat['messages'].append({'text': 'Уточните, какой формат работы рассматриваете?', 'isOut': False})
    bot.compose_chat_reply = Mock(return_value='Рассматриваю удалённую работу. Готов обсудить задачи.')
    bot._handle_chat(chat, 'AppSec\nTest\nОтказ')
    assert 'Рассматриваю' in bot.send_chat_reply.call_args.args[0]


def test_one_bad_chat_does_not_stop_other_chats(bot):
    cards = [{'idx': i, 'key': str(i), 'text': 'Role\nTest'} for i in range(3)]
    bot._set_unread_filter = Mock(return_value=True)
    bot._scroll_chat_list = Mock(return_value={'moved': False})
    bot._read_chat_cards = Mock(return_value=cards)
    bot._open_chat_card = Mock(return_value=snapshot())
    bot._handle_chat = Mock(side_effect=[RuntimeError('one chat failed'), None, None])
    bot._back_to_chat_list = Mock(return_value=True)
    bot._mark_chat_read = Mock()
    bot.process_unread_messenger_chats()
    assert bot._handle_chat.call_count == 3
    assert bot.messenger_summary['failed'] == 1
    assert bot.messenger_summary['complete'] is False


@pytest.mark.parametrize('timestamp', ['вчера', 'сегодня', '03 октября', '03.10.2026', 'yesterday'])
def test_timestamp_between_title_and_company_does_not_prevent_opening(bot, timestamp):
    bot.driver.execute_script = Mock(return_value=True)
    bot._read_open_chat = Mock(return_value=snapshot())
    card = {'idx': 0, 'key': 'chat:42', 'text': f'AppSec\n{timestamp}\nTest\nОтклик на\u00a0вакансию'}
    assert bot._open_chat_card(card) is not None


def prepare_scan(bot, cards, opened):
    bot._set_unread_filter = Mock(return_value=True)
    bot._scroll_chat_list = Mock(return_value={'moved': False})
    bot._read_chat_cards = Mock(return_value=cards)
    bot._open_chat_card = opened
    bot._recover_messenger = Mock(return_value=True)
    bot._handle_chat = Mock(return_value=None)
    bot._back_to_chat_list = Mock(return_value=True)
    bot._mark_chat_read = Mock()


def test_repeated_open_failures_stop_scan_instead_of_warning_for_every_chat(bot):
    cards = [{'idx': i, 'key': str(i), 'text': f'Role {i}\nTest'} for i in range(6)]
    prepare_scan(bot, cards, Mock(return_value=None))
    bot.process_unread_messenger_chats()
    assert bot.messenger_summary.get('blocked') is True
    assert bot.messenger_summary['complete'] is False
    assert bot._open_chat_card.call_count <= 4
    bot._recover_messenger.assert_called_once()
    bot._handle_chat.assert_not_called()
    bot._mark_chat_read.assert_not_called()
    assert 'Role' in bot.messenger_summary['reason']


def test_transient_open_failure_recovers_before_trying_other_chats(bot):
    cards = [{'idx': 0, 'key': '42', 'text': 'AppSec\nTest'}]
    prepare_scan(bot, cards, Mock(side_effect=[None, snapshot()]))
    bot.process_unread_messenger_chats()
    bot._recover_messenger.assert_called_once()
    bot._handle_chat.assert_called_once()
    assert bot.messenger_summary['complete'] is True
    assert not bot.messenger_summary.get('blocked')


def test_isolated_unreadable_chat_is_saved_and_does_not_block_healthy_chats(bot):
    cards = [{'idx': i, 'key': str(i), 'text': f'Role {i}\nTest'} for i in range(3)]
    prepare_scan(bot, cards, Mock(side_effect=[None, None, snapshot(), snapshot()]))
    bot.process_unread_messenger_chats()
    assert not bot.messenger_summary.get('blocked')
    assert bot.messenger_summary['viewed'] == 2
    assert bot.messenger_summary['failed'] == 1
    pending = [a for a in bot._load_chat_actions().values() if a['kind'] == 'chat_open']
    assert len(pending) == 1
    assert pending[0]['status'] == 'deferred'


def test_failed_recovery_stops_scan_without_sending(bot):
    prepare_scan(bot, [{'idx': 0, 'key': '42', 'text': 'AppSec\nTest'}], Mock(return_value=None))
    bot._recover_messenger.return_value = False
    bot.process_unread_messenger_chats()
    assert bot.messenger_summary.get('blocked') is True
    assert bot._open_chat_card.call_count == 1
    bot.send_chat_reply.assert_not_called()


def test_recovery_exception_also_blocks_full_cycle(bot):
    prepare_scan(bot, [{'idx': 0, 'key': '42', 'text': 'AppSec\nTest'}], Mock(return_value=None))
    bot._recover_messenger.side_effect = RuntimeError('page reload failed')
    bot.process_unread_messenger_chats()
    assert bot.messenger_summary.get('blocked') is True
    assert 'page reload failed' in bot.messenger_summary['reason']
    bot.send_chat_reply.assert_not_called()


def test_recovery_preserves_a_manual_draft(bot):
    composer = Mock()
    composer.get_attribute.return_value = 'Мой незаконченный ответ'
    bot.find_chat_message_input.return_value = composer
    bot.driver.refresh = Mock()
    assert RejectionAnalyzer._recover_messenger(bot) is False
    bot.driver.refresh.assert_not_called()
    assert 'черновик' in bot._messenger_recovery_problem


def test_recovery_refreshes_once_and_waits_for_chat_list(bot):
    bot.find_chat_message_input.return_value = None
    bot.driver.refresh = Mock()
    bot._set_unread_filter = Mock()
    bot._scroll_chat_list = Mock()
    bot._read_chat_cards = Mock(side_effect=[[], [{'idx': 0, 'key': '42', 'text': 'AppSec\nTest'}]])
    assert RejectionAnalyzer._recover_messenger(bot) is True
    bot.driver.refresh.assert_called_once()


def test_unavailable_chat_list_after_refresh_does_not_loop_forever(bot):
    bot.find_chat_message_input.return_value = None
    bot.driver.refresh = Mock()
    bot._set_unread_filter = Mock()
    bot._scroll_chat_list = Mock()
    bot._read_chat_cards = Mock(return_value=[])
    bot._back_to_chat_list = Mock(return_value=False)
    assert RejectionAnalyzer._recover_messenger(bot) is False
    assert bot._read_chat_cards.call_count == 20
    assert 'не загрузился' in bot._messenger_recovery_problem


def test_recovery_returns_from_mobile_conversation_to_list(bot):
    bot.find_chat_message_input.return_value = None
    bot.driver.refresh = Mock()
    bot._set_unread_filter = Mock()
    bot._scroll_chat_list = Mock()
    card = {'idx': 0, 'key': '42', 'text': 'AppSec\nTest'}
    bot._read_chat_cards = Mock(side_effect=[[]] * 20 + [[card]])
    bot._back_to_chat_list = Mock(return_value=True)
    assert RejectionAnalyzer._recover_messenger(bot) is True
    bot._back_to_chat_list.assert_called_once()
    bot.driver.refresh.assert_called_once()


def test_recovery_detects_login_redirect_instead_of_clicking_more_chats(bot):
    bot.find_chat_message_input.return_value = None
    bot.driver.refresh = Mock()
    bot.driver.current_url = 'https://hh.ru/account/login'
    assert RejectionAnalyzer._recover_messenger(bot) is False
    assert 'входа' in bot._messenger_recovery_problem


def test_matching_requires_exact_name_unless_explicitly_truncated():
    from chat_workflow import chat_name_matches
    assert not chat_name_matches('AppSec', 'AppSec Senior')
    assert not chat_name_matches('Company 1', 'Company 10')
    assert chat_name_matches('Эксперт по кибербезоп...', 'Эксперт по кибербезопасности')
    assert chat_name_matches('Лаборатория Касперского', 'Лаборатория Касперского')


def test_stale_chat_with_similar_title_is_not_processed(bot):
    bot.driver.execute_script = Mock(return_value=True)
    stale = snapshot()
    stale['vacancy_title'] = 'AppSec Senior'
    bot._read_open_chat = Mock(return_value=stale)
    assert bot._open_chat_card({'idx': 0, 'key': '42', 'text': 'AppSec\nвчера\nTest'}) is None
    assert bot._last_chat_open_problem['actual_title'] == 'AppSec Senior'
    bot.send_chat_reply.assert_not_called()


def test_missing_title_has_specific_diagnosis_and_solution(bot, caplog):
    bot.driver.execute_script = Mock(return_value=True)
    bot._read_open_chat = Mock(return_value=dict(snapshot(), vacancy_title=''))
    assert bot._open_chat_card({'idx': 0, 'key': '42', 'text': 'AppSec\nTest'}) is None
    assert bot._last_chat_open_problem['code'] == 'missing_vacancy_title'
    bot.messenger_summary = {}
    bot._block_messenger('Два чата не подтверждены')
    assert 'Что делать' in caplog.text
    assert 'название вакансии' in bot.messenger_summary['recovery_hint']
    assert 'chat_followups.md' in bot.messenger_summary['followups_path']


def test_navigation_failure_after_a_chat_also_stops_cycle(bot):
    prepare_scan(bot, [{'idx': 0, 'key': '42', 'text': 'AppSec\nTest'}], Mock(return_value=snapshot()))
    bot._back_to_chat_list.return_value = False
    bot.process_unread_messenger_chats()
    assert bot.messenger_summary.get('blocked') is True
    assert 'Список чатов' in bot.messenger_summary['reason']


def test_deferred_unopened_chat_is_resolved_when_read_on_next_run(bot):
    cards = [{'idx': 0, 'key': '42', 'text': 'AppSec\nTest'}]
    prepare_scan(bot, cards, Mock(side_effect=[None, None]))
    bot.process_unread_messenger_chats()
    assert next(iter(bot._load_chat_actions().values()))['status'] == 'deferred'
    bot._open_chat_card = Mock(return_value=snapshot())
    bot.process_unread_messenger_chats()
    assert next(iter(bot._load_chat_actions().values()))['status'] == 'resolved'
    assert not bot.messenger_summary.get('blocked')


def test_blocked_messenger_does_not_fall_through_to_archive_scan(bot):
    bot.messenger_summary = {'blocked': True, 'reason': 'header mismatch', 'complete': False}
    bot.process_unread_messenger_chats = Mock(return_value=[])
    bot.goto = Mock(return_value=False)
    assert bot.fetch_rejected_chats() == []
    bot.goto.assert_not_called()


def test_blocked_messenger_does_not_analyze_cache_or_edit_resume(bot):
    bot.messenger_summary = {'blocked': True, 'reason': 'header mismatch', 'complete': False}
    bot.fetch_rejected_chats = Mock(return_value=[])
    bot._load_cached_chats = Mock(return_value=[])
    bot.seen_rejection_keys = Mock(return_value=set())
    result = bot.run_chat_analysis(use_mock_if_empty=False)
    assert result['status'] == 'messenger_blocked'
    bot._load_cached_chats.assert_not_called()


def test_full_cycle_stops_before_search_if_messenger_is_blocked(monkeypatch, tmp_path):
    import test as entrypoint
    analyzer = Mock(config={})
    analyzer._user_closed = False
    analyzer.messenger_summary = {'blocked': True, 'reason': 'header mismatch'}
    analyzer._chat_state_path.return_value = str(tmp_path / 'chat_actions.json')
    analyzer.run_chat_analysis.return_value = {'status': 'messenger_blocked', 'messenger': analyzer.messenger_summary}
    monkeypatch.setattr('rejection_analyzer.RejectionAnalyzer', lambda: analyzer)
    monkeypatch.setattr(entrypoint.sys, 'argv', ['test.py'])
    monkeypatch.setattr(entrypoint.sys, 'stdin', SimpleNamespace(isatty=lambda: False))
    applicant = entrypoint.HHAutoApplicant.__new__(entrypoint.HHAutoApplicant)
    applicant.sync_cache_with_applied = Mock()
    applicant.check_resume_status = Mock(return_value=False)
    applicant.run_auto_applications({})
    applicant.sync_cache_with_applied.assert_not_called()
    analyzer.close.assert_called_once()


def test_recovery_prompt_error_cannot_unblock_full_cycle(monkeypatch):
    import test as entrypoint
    analyzer = Mock(config={}, _user_closed=False)
    analyzer.messenger_summary = {'blocked': True, 'reason': 'header mismatch'}
    monkeypatch.setattr('rejection_analyzer.RejectionAnalyzer', lambda: analyzer)
    monkeypatch.setattr('rejection_analyzer.run_chat_analysis_with_recovery',
                        Mock(side_effect=RuntimeError('Recovery UI failed')))
    monkeypatch.setattr(entrypoint.sys, 'argv', ['test.py'])
    applicant = entrypoint.HHAutoApplicant.__new__(entrypoint.HHAutoApplicant)
    applicant.sync_cache_with_applied = Mock()
    applicant.run_auto_applications({})
    applicant.sync_cache_with_applied.assert_not_called()
    analyzer.close.assert_called_once()


def test_visible_recovery_carries_browser_mode_into_next_stage(monkeypatch, tmp_path):
    import test as entrypoint
    analyzer = Mock(config={}, _user_closed=False, headless=True)
    analyzer._chat_state_path.return_value = str(tmp_path / 'chat_actions.json')
    analyzer.find_chat_message_input.return_value = None
    analyzer.is_driver_alive.return_value = True
    analyzer.close.side_effect = lambda: setattr(analyzer.is_driver_alive, 'return_value', False)
    runs = iter([True, False])

    def analyze(**kwargs):
        blocked = next(runs)
        analyzer.messenger_summary = {'blocked': blocked, 'reason': 'header mismatch', 'complete': not blocked}
        return {'status': 'messenger_blocked' if blocked else 'no_chats_found',
                'messenger': analyzer.messenger_summary}

    analyzer.run_chat_analysis.side_effect = analyze
    monkeypatch.setattr('rejection_analyzer.RejectionAnalyzer', lambda: analyzer)
    monkeypatch.setattr(entrypoint.sys, 'argv', ['test.py', '--headless'])
    monkeypatch.setattr(entrypoint.sys, 'stdin', SimpleNamespace(isatty=lambda: True))
    answers = iter(['1', ''])
    monkeypatch.setattr('builtins.input', lambda _: next(answers))
    applicant = entrypoint.HHAutoApplicant.__new__(entrypoint.HHAutoApplicant)
    applicant.sync_cache_with_applied = Mock()
    applicant.check_resume_status = Mock(return_value=False)
    applicant.run_auto_applications({})
    applicant.sync_cache_with_applied.assert_called_once()
    assert '--show-browser' in entrypoint.sys.argv
    assert '--headless' not in entrypoint.sys.argv
    assert analyzer.run_chat_analysis.call_count == 2


@pytest.fixture
def chrome_chat(tmp_path, bot):
    if os.environ.get('HH_BROWSER_SMOKE') != '1':
        pytest.skip('Run explicitly with HH_BROWSER_SMOKE=1; synthetic local chats only')
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    html = r'''<!doctype html><meta charset="utf-8">
    <style>
    body {display:flex; margin:0; height:700px; font:16px Arial}
    #left {width:350px} #list {height:500px; overflow:auto; position:relative}
    #spacer {height:5850px} #items {position:absolute; top:0; left:0; right:0}
    .chat-cell {position:absolute; height:85px; width:100%; cursor:pointer}
    #right {width:600px} #messages {height:240px; overflow:auto}
    .chat-bubble {padding:8px; margin:5px; min-height:80px}
    textarea {width:500px; height:100px}
    </style>
    <div id="left"><label><input id="unread" type="checkbox" checked>Только непрочитанные</label>
    <div id="list"><div id="spacer"></div><div id="items"></div></div></div>
    <div id="right"><div data-qa="participant-info-details" id="company"></div>
    <a data-qa="chatik-header-vacancy-link" id="vacancy"></a>
    <div id="messages" data-qa="chatik-chat-messages"></div>
    <textarea data-qa="chatik-chat-message-input" id="composer"></textarea></div>
    <script>
    const list = document.getElementById('list'), items = document.getElementById('items');
    window.opened = []; window.sent = []; window.active = -1; window.rejectSend = false;
    window.histories = {};
    window.dynamicUnread = false; window.readChats = new Set();
    function render() {
      const queue = Array.from({length:65}, (_,i)=>i)
        .filter(i => !dynamicUnread || !document.getElementById('unread').checked || !readChats.has(i));
      const start = Math.max(0, Math.floor(list.scrollTop / 90));
      document.getElementById('spacer').style.height = (queue.length*90)+'px';
      items.innerHTML = '';
      if (!queue.length) items.innerHTML = '<div class="magritte-placeholder___fixture"><div data-qa="title">Нет непрочитанных чатов</div></div>';
      for (let row=start; row<Math.min(queue.length,start+7); row++) {
        const i=queue[row];
        const card = document.createElement('div'); card.className = 'chat-cell';
        card.setAttribute('data-chat-id', 'topic:'+i); card.setAttribute('data-qa', 'chatik-open-chat');
        card.style.top = (row*90)+'px';
        card.innerHTML = '<span class="chat-cell-title">Role '+i+'</span><br>вчера<br>Company '+i+'<br>'
          +(i%3===0 ? 'Отказ' : 'Собеседование');
        card.onclick=()=>openChat(i); items.append(card);
      }
    }
    function openChat(i) {
      active=i; opened.push(i); document.getElementById('company').textContent='Company '+i;
      const vacancy = document.getElementById('vacancy');
      vacancy.textContent='Role '+i; vacancy.href='https://hh.ru/vacancy/'+(1000+i);
      const text = i%3===0 ? 'К сожалению, сейчас не готовы пригласить вас на следующий этап.'
        : i%3===1 ? 'Есть ли у вас опыт реализации custom UI?'
        : 'Пройдите короткое первичное интервью с ГигаРекрутером: https://telegram.me/Giga_recruiter_bot?start=chat'+i;
      if (!histories[i]) histories[i] = '<div class="chat-bubble" data-qa="chatik-chat-message" data-message-id="in'+i+'">'
        + '<div data-qa="chatik-chat-message-text">'+text+'</div><div>13:24</div></div>';
      document.getElementById('messages').innerHTML=histories[i];
      const composer=document.getElementById('composer'); composer.value=''; composer.disabled=i%3===2;
      if (dynamicUnread) {readChats.add(i); render();}
    }
    document.getElementById('composer').onkeydown=function(e) {
      if (e.key!=='Enter') return;
      e.preventDefault(); const text=this.value; this.value='';
      if (rejectSend) return;
      const bubble=document.createElement('div'); bubble.className='chat-bubble outgoing';
      bubble.setAttribute('data-qa','chatik-chat-message'); bubble.textContent=text;
      document.getElementById('messages').append(bubble);
      histories[active]=document.getElementById('messages').innerHTML; sent.push({id:active,text});
    };
    list.onscroll=render; document.getElementById('unread').onchange=render; render();
    </script>'''
    directory = tmp_path / 'chat'
    directory.mkdir()
    path = directory / 'index.html'
    path.write_text(html, encoding='utf-8')
    options = Options()
    options.add_argument('--headless=new')
    options.add_argument('--disable-background-networking')
    options.add_argument('--window-size=1280,900')
    options.add_argument(f'--user-data-dir={tmp_path / "chrome"}')
    driver = webdriver.Chrome(options=options)
    bot.driver = driver
    bot.is_driver_alive = RejectionAnalyzer.is_driver_alive.__get__(bot)
    bot.find_chat_message_input = RejectionAnalyzer.find_chat_message_input.__get__(bot)
    bot.send_chat_reply = RejectionAnalyzer.send_chat_reply.__get__(bot)
    bot._current_chat_matches = RejectionAnalyzer._current_chat_matches.__get__(bot)
    bot._open_external_interview = Mock(return_value=True)
    bot.compose_chat_reply = Mock(return_value='Есть смежный опыт разработки на Python. Готов обсудить задачи custom UI.')
    driver.get(path.as_uri())
    try:
        yield bot
    finally:
        driver.quit()


def test_real_chrome_virtual_list_65_chats_and_no_duplicate_sends(chrome_chat):
    bot = chrome_chat
    refusals = bot.process_unread_messenger_chats()
    assert len(refusals) == 22
    assert bot.messenger_summary == {'viewed': 65, 'rejections': 22, 'sent': 44, 'failed': 0, 'complete': True}
    assert set(bot.driver.execute_script('return opened;')) == set(range(65))
    assert len(bot.driver.execute_script('return sent;')) == 44
    assert bot._open_external_interview.call_count == 21
    bot.process_unread_messenger_chats()
    assert len(bot.driver.execute_script('return sent;')) == 44
    assert bot._open_external_interview.call_count == 21


@pytest.mark.parametrize('mode', ['advance', 'stuck', 'draft', 'disabled', 'changed'])
def test_real_chrome_recruiter_buttons_and_progress_confirmation(chrome_chat, mode):
    from ai_assistant import AIAssistant
    bot = chrome_chat
    bot.ai_assistant = AIAssistant({'ai_config': {'enabled': False}, 'candidate_profile': {'skills': ['Python']}})
    bot.driver.execute_script(r'''
        openChat(1); window.choiceClicks = []; window.recruiterMode = arguments[0];
        const messages = document.getElementById('messages');
        messages.innerHTML = '';
        const choices = document.createElement('div'); choices.id='fixture-choices';
        document.getElementById('right').insertBefore(choices, document.getElementById('composer'));
        function question(text, last=false) {
            const bubble = document.createElement('div'); bubble.className='chat-bubble chat-bubble_bot';
            bubble.setAttribute('data-qa','chatik-chat-message');
            const content=document.createElement('div'); content.setAttribute('data-qa','chat-bubble-text');
            content.textContent=text; bubble.append(content); messages.append(bubble);
            choices.innerHTML='';
            if (last) return;
            for (const label of ['Да','Нет']) {
                const button=document.createElement('button'); button.textContent=label;
                button.disabled=recruiterMode==='disabled';
                button.onclick=()=>{
                    choiceClicks.push(label);
                    if (recruiterMode==='stuck') return;
                    const outgoing=document.createElement('div'); outgoing.className='chat-bubble outgoing';
                    outgoing.textContent=label; messages.append(outgoing);
                    if (choiceClicks.length===1) question('Готовы работать удалённо?');
                    else question('Ваши ответы отправлены работодателю.', true);
                };
                choices.append(button);
            }
        }
        question('Готовы ли вы оформить допуск по третьей форме?');
        document.getElementById('composer').value=recruiterMode==='draft' ? 'Мой черновик' : '';
    ''', mode)
    chat = dict(bot._read_open_chat(), identity='topic:1')
    assert [c['label'] for c in chat['choice_options']] == ['Да', 'Нет']
    assert chat['messages'][-1]['isBot']
    if mode == 'changed':
        # Replace the actual question between the final snapshot and the click.
        execute = bot.driver.execute_script
        def change_before_click(script, *args):
            if "return 'ok';" in script:
                execute("document.querySelector('[data-qa=chat-bubble-text]').textContent='Другой вопрос?';")
            return execute(script, *args)
        bot.driver.execute_script = change_before_click
    bot._handle_chat(chat)
    actual = bot.driver.execute_script('return choiceClicks;')
    assert actual == (['Да', 'Да'] if mode == 'advance' else ['Да'] if mode == 'stuck' else [])
    bot.compose_chat_reply.assert_not_called()
    assert bot.driver.execute_script('return sent;') == []
    if mode == 'draft':
        assert bot.driver.execute_script("return document.getElementById('composer').value;") == 'Мой черновик'
    if mode == 'stuck':
        bot._handle_chat(dict(bot._read_open_chat(), identity='topic:1'))
        assert bot.driver.execute_script('return choiceClicks;') == ['Да']
        assert next(iter(bot._load_chat_actions().values()))['status'] == 'pending_confirmation'


def test_real_chrome_unread_queue_shrinks_without_opening_read_chats(chrome_chat):
    bot = chrome_chat
    bot.driver.execute_script('''
        dynamicUnread = true;
        readChats = new Set(Array.from({length:65}, (_,i)=>i).filter(i=>i%3===2));
        document.getElementById('unread').checked = false;
        render();
    ''')
    expected = [i for i in range(65) if i % 3 != 2]
    bot.process_unread_messenger_chats()
    assert bot.driver.execute_script('return opened;') == expected
    assert bot.driver.execute_script('return document.getElementById("unread").checked;') is True
    assert bot.messenger_summary == {'viewed': 44, 'rejections': 22, 'sent': 44, 'failed': 0, 'complete': True}
    assert bot._chat_list_visible() is True
    bot.process_unread_messenger_chats()
    assert bot.messenger_summary == {'viewed': 0, 'rejections': 0, 'sent': 0, 'failed': 0, 'complete': True}
    assert bot.driver.execute_script('return opened;') == expected
    assert bot.driver.execute_script('return sent.length;') == 44


def test_real_chrome_card_identity_uses_company_instead_of_date(chrome_chat):
    bot = chrome_chat
    bot.driver.execute_script("document.querySelector('.chat-cell').removeAttribute('data-chat-id')")
    card = bot._read_chat_cards()[0]
    assert card['key'] == 'Role 0|Company 0'
    assert card['company_name'] == 'Company 0'
    chat = bot._open_chat_card(card)
    assert chat['company_name'] == 'Company 0'


@pytest.mark.parametrize('date,company', [('пт', 'Company 0'), ('Неделю назад', 'Company 0'), ('вчера', 'ПТ')])
def test_real_chrome_card_uses_semantic_title_and_employer(chrome_chat, date, company):
    bot = chrome_chat
    bot.driver.execute_script("""
        const card = document.querySelector('.chat-cell');
        card.removeAttribute('data-chat-id');
        card.innerHTML = '<div data-qa="chat-cell-title" class="title--new">Role 0</div>'
            + '<div data-qa="chat-cell-meta"><span data-qa="chat-cell-creation-time"></span></div>'
            + '<div data-qa="chat-cell-subtitle" class="subtitle--new"></div>'
            + '<div>Собеседование</div>';
        card.querySelector('[data-qa="chat-cell-creation-time"]').textContent = arguments[0];
        card.querySelector('[data-qa="chat-cell-subtitle"]').textContent = arguments[1];
    """, date, company)
    card = bot._read_chat_cards()[0]
    assert card['vacancy_title'] == 'Role 0'
    assert card['company_name'] == company
    assert card['key'] == 'Role 0|' + company
    if company == 'Company 0':
        assert bot._open_chat_card(card)['company_name'] == company


@pytest.mark.parametrize('letter', ['', 'Здравствуйте! Готов обсудить задачи команды.'])
def test_real_chrome_system_application_bubble_is_loaded_and_outgoing(chrome_chat, letter):
    bot = chrome_chat
    body = letter or 'Без сопроводительного письма'
    markup = ('<div data-qa="chatik-chat-message-15717475069">'
              '<div data-qa="chat-marker">Вчера</div>'
              '<div class="chat-bubble-container--hash" data-qa="chat-bubble-wrapper">'
              '<div class="chat-bubble--hash chat-bubble_outgoing--hash">'
              '<div data-qa="chat-bubble-title">Отклик на вакансию</div>'
              f'<span data-qa="chat-bubble-text">{body}</span>'
              '<span data-qa="chat-buble-display-time">19:51</span></div>'
              '<a data-qa="chatik-chat-message-applicant-action">Добавить сопроводительное</a>'
              '</div></div>')
    bot.driver.execute_script("openChat(0); histories[0]=arguments[0]; openChat(0)", markup)
    chat = bot._open_chat_card(bot._read_chat_cards()[0])
    assert chat is not None
    assert chat['messages_loaded'] is True
    assert chat['messages'] == ([{'text': letter, 'raw_text': letter, 'isOut': True, 'isBot': False, 'id': '',
                                 'failed': False, 'pending': False, 'links': []}] if letter else [])
    assert bot._handle_chat(chat) is None
    bot.compose_chat_reply.assert_not_called()
    assert bot.driver.execute_script('return sent.length') == 0


def test_matching_header_without_message_nodes_is_not_loaded(bot):
    bot.driver.execute_script = Mock(return_value=True)
    bot._read_open_chat = Mock(return_value=dict(snapshot(), messages=[], messages_loaded=False))
    assert bot._open_chat_card({'idx': 0, 'key': '42', 'text': 'AppSec\nTest'}) is None


def test_real_chrome_confirms_full_reply_before_analytical_text_cleanup(chrome_chat):
    bot = chrome_chat
    chat = bot._open_chat_card(bot._read_chat_cards()[1])
    bot.driver.execute_script("document.querySelector('style').textContent += '.chat-bubble {white-space:pre-wrap}'")
    reply = '—\n\nЕсть смежный опыт разработки на Python. Готов обсудить задачи.'
    assert bot.send_chat_reply(reply) is True
    current = bot._read_open_chat()
    assert current['messages'][-1]['raw_text'] == reply
    assert current['messages'][-1]['text'] != reply
    assert len(bot.driver.execute_script('return sent;')) == 1
    chat.update(current)
    bot._save_chat_action('uncertain', dict(kind='answer', status='pending_confirmation',
                                          identity=chat['identity'], reply=reply))
    bot._handle_chat(chat)
    assert bot._load_chat_actions()['uncertain']['status'] == 'sent'
    assert len(bot.driver.execute_script('return sent;')) == 1


def test_real_chrome_rendered_bold_reply_reconciles_without_resending(chrome_chat):
    bot = chrome_chat
    chat = bot._open_chat_card(bot._read_chat_cards()[1])
    reply = '1. Есть **опыт Python**.\n2. Зарплата **200 000 рублей**.'
    bot.driver.execute_script("""
        const message = document.createElement('div');
        message.className = 'chat-bubble outgoing';
        message.innerHTML = '<div data-qa="chat-bubble-text">1. Есть <strong>опыт Python</strong>.'
            + '<br>2. Зарплата <strong>200 000 рублей</strong>.</div>';
        document.getElementById('messages').append(message);
    """)
    bot._save_chat_action('uncertain', dict(kind='answer', status='pending_confirmation',
                                          identity=chat['identity'], reply=reply))
    chat.update(bot._read_open_chat())
    bot._handle_chat(chat)
    assert bot._load_chat_actions()['uncertain']['status'] == 'sent'
    assert len(bot.driver.execute_script('return sent;')) == 0


@pytest.mark.parametrize('matching_url', [True, False])
def test_real_chrome_blocked_company_is_skipped_only_in_expected_chat(chrome_chat, matching_url):
    bot = chrome_chat
    card = bot._read_chat_cards()[0]
    bot.driver.execute_script("""
        document.querySelector('.chat-cell').onclick = () => {
            document.getElementById('company').textContent = 'Company 0';
            document.getElementById('vacancy').remove();
            document.getElementById('messages').innerHTML = '';
            document.getElementById('composer').remove();
            const splash = document.createElement('div');
            splash.className = 'dialog-splash-screen--new';
            splash.innerHTML = '<h2 data-qa="title">Компания заблокирована</h2>';
            document.getElementById('right').append(splash);
        };
    """)
    card['href'] = bot.driver.current_url if matching_url else 'https://hh.ru/chat/other'
    chat = bot._open_chat_card(card)
    if matching_url:
        assert chat['unavailable_reason'] == 'Компания заблокирована'
        assert chat['vacancy_title'] == 'Role 0'
        bot._handle_chat(chat)
        bot.compose_chat_reply.assert_not_called()
    else:
        assert chat is None
    assert len(bot.driver.execute_script('return sent;')) == 0


def test_real_chrome_recognizes_completed_bot_form_and_ignores_departure_marker(chrome_chat):
    bot = chrome_chat
    markup = ('<div data-qa="chatik-chat-message-1"><div class="chat-bubble--hash chat-bubble_outgoing--hash">'
              '<span data-qa="chat-bubble-text">Есть смежный опыт.</span></div></div>'
              '<div data-qa="chatik-chat-message-2"><div class="chat-bubble--hash chat-bubble_bot--hash">'
              '<span data-qa="chat-bubble-text">Есть ли игровой опыт? Есть смежный опыт.</span></div></div>'
              '<div data-qa="chatik-chat-message-3"><div class="chat-bubble--hash chat-bubble_bot--hash">'
              '<span data-qa="chat-bubble-text">Спасибо! Ваши ответы отправлены работодателю.</span></div></div>'
              '<div data-qa="chatik-chat-message-4"><div data-qa="chat-marker">'
              'Пользователь Робот-рекрутер покинул чат</div></div>')
    bot.driver.execute_script("histories[0]=arguments[0]; openChat(0)", markup)
    chat = bot._open_chat_card(bot._read_chat_cards()[0])
    assert len(chat['messages']) == 3
    assert chat['messages'][-1]['isBot'] is True
    bot._handle_chat(chat)
    bot.compose_chat_reply.assert_not_called()
    assert bot.driver.execute_script('return sent.length') == 0


@pytest.mark.parametrize('markup', [
    '<a href="https://hh.ru/vacancy/1000">Role 0</a>',
    '<section><div>Вакансия</div><div>Role 0</div><a href="https://hh.ru/vacancy/1000">Перейти</a></section>',
    '<section><div>Вакансия в архиве</div><div>Role 0</div><a data-qa="chatik-header-vacancy-link" href="https://hh.ru/vacancy/1000">Перейти</a></section>',
    '<a style="display:none" data-qa="chatik-header-vacancy-link" href="https://hh.ru/vacancy/999">Stale</a>'
    '<a data-qa="chatik-header-vacancy-link" href="https://hh.ru/vacancy/1000">Role 0</a>',
    '<a data-qa="chatik-header-vacancy-link" href="https://hh.ru/vacancy/1000">в архиве<br>Role 0</a>',
    '<a data-qa="chatik-header-vacancy-link" href="https://hh.ru/vacancy/1000">Вакансия<br>в архиве<br>Role 0</a>',
    '<section><div>в архиве</div><div>Role 0</div><a href="https://hh.ru/vacancy/1000">Перейти</a></section>',
])
def test_real_chrome_reads_visible_vacancy_header_variants(chrome_chat, markup):
    bot = chrome_chat
    bot.driver.execute_script("openChat(0); document.getElementById('vacancy').outerHTML=arguments[0]", markup)
    chat = bot._read_open_chat()
    assert chat['vacancy_title'] == 'Role 0'
    assert chat['vacancy_url'] == 'https://hh.ru/vacancy/1000'
    assert bot._current_chat_matches(dict(snapshot(), vacancy_title='Role 0',
                                        company_name='Company 0', vacancy_url='https://hh.ru/vacancy/1000'))
    assert bot.driver.execute_script('return sent.length') == 0


def test_real_chrome_does_not_take_title_from_message_or_sidebar(chrome_chat):
    bot = chrome_chat
    bot.driver.execute_script('''
        openChat(0); document.getElementById('vacancy').remove();
        document.querySelector('.chat-cell').insertAdjacentHTML('beforeend',
            '<a href="https://hh.ru/vacancy/1000">Role 0</a>');
        document.querySelector('.chat-bubble').insertAdjacentHTML('beforeend',
            '<a href="https://hh.ru/vacancy/1000">Role 0</a>');
    ''')
    assert not bot._read_open_chat().get('vacancy_title')
    assert bot._open_chat_card(bot._read_chat_cards()[0]) is None
    assert bot.driver.execute_script('return sent.length') == 0


def test_real_chrome_ambiguous_header_is_not_used(chrome_chat):
    bot = chrome_chat
    bot.driver.execute_script('''
        openChat(0); document.getElementById('vacancy').removeAttribute('data-qa');
        document.getElementById('vacancy').insertAdjacentHTML('afterend',
            '<a href="https://hh.ru/vacancy/999">Different role</a>');
    ''')
    assert not bot._read_open_chat().get('vacancy_title')
    assert bot.driver.execute_script('return sent.length') == 0


def test_real_chrome_foreign_vacancy_link_cannot_confirm_chat(chrome_chat):
    bot = chrome_chat
    bot.driver.execute_script('''
        openChat(0); document.getElementById('vacancy').removeAttribute('data-qa');
        document.getElementById('vacancy').href='https://external.example/vacancy/1000';
    ''')
    assert not bot._read_open_chat().get('vacancy_title')
    assert bot.driver.execute_script('return sent.length') == 0


def test_real_chrome_recovery_does_not_reload_a_manual_draft(chrome_chat):
    bot = chrome_chat
    bot.driver.execute_script("openChat(1); document.getElementById('composer').value='Незаконченный ответ'")
    assert bot._recover_messenger() is False
    assert bot.driver.execute_script("return document.getElementById('composer').value") == 'Незаконченный ответ'


def test_real_chrome_mark_read_does_not_jump_sidebar(chrome_chat):
    bot = chrome_chat
    bot._open_chat_card(bot._read_chat_cards()[0])
    bot.driver.execute_script('document.getElementById("list").scrollTop=400;')
    before = bot.driver.execute_script('return document.getElementById("list").scrollTop;')
    bot._mark_chat_read()
    assert bot.driver.execute_script('return document.getElementById("list").scrollTop;') == before


def test_real_chrome_manual_draft_preserved(chrome_chat):
    bot = chrome_chat
    bot._open_chat_card(bot._read_chat_cards()[0])
    bot.driver.execute_script('document.getElementById("composer").value="Мой черновик";')
    assert bot.send_chat_reply('Не отправлять поверх черновика') is False
    assert bot.driver.execute_script('return document.getElementById("composer").value;') == 'Мой черновик'
    assert bot.driver.execute_script('return sent.length;') == 0


def test_real_chrome_empty_field_without_outgoing_is_not_success(chrome_chat):
    bot = chrome_chat
    bot._open_chat_card(bot._read_chat_cards()[0])
    bot.driver.execute_script('rejectSend=true;')
    assert bot.send_chat_reply('Здравствуйте! Подскажите причину отказа.') is False
    assert bot.driver.execute_script('return sent.length;') == 0


def test_real_chrome_multiline_reply_is_one_complete_message(chrome_chat):
    bot = chrome_chat
    bot._open_chat_card(bot._read_chat_cards()[1])
    text = 'Здравствуйте!\n\n1. Есть смежный опыт с Python.\n2. Готов обсудить задачи на созвоне.'
    assert bot.send_chat_reply(text) is True
    sent = bot.driver.execute_script('return sent;')
    assert sent == [{'id': 1, 'text': text}]


def replace_with_dynamic_editor(bot, rerender=False, switch_chat=False):
    bot.driver.execute_script('''
        openChat(1);
        document.getElementById('composer').outerHTML =
            '<div id="composer" contenteditable="true" role="textbox" '
            + 'data-qa="chatik-chat-message-input" style="white-space:pre-wrap;width:500px;height:100px"></div>';
        window.composerChanges = 0;
        function bindEditor(editor) {
            editor.onkeydown = function(e) {
                if (e.key !== 'Enter') return;
                e.preventDefault();
                const text = this.innerText;
                this.textContent = '';
                const bubble = document.createElement('div');
                bubble.className = 'chat-bubble outgoing';
                bubble.setAttribute('data-qa', 'chatik-chat-message'); bubble.textContent = text;
                document.getElementById('messages').append(bubble);
                sent.push({id:active, text});
                histories[active] = document.getElementById('messages').innerHTML;
            };
            editor.oninput = function() {
                if (window.composerChanges++) return;
                if (argumentsRerender) {
                    const clone = this.cloneNode(true); this.replaceWith(clone); bindEditor(clone);
                }
                if (argumentsSwitch) openChat(2);
            };
        }
        const argumentsRerender = arguments[0], argumentsSwitch = arguments[1];
        bindEditor(document.getElementById('composer'));
    ''', rerender, switch_chat)


@pytest.mark.parametrize('rerender', [False, True])
def test_real_chrome_contenteditable_multiline_survives_render(chrome_chat, rerender):
    bot = chrome_chat
    replace_with_dynamic_editor(bot, rerender=rerender)
    text = 'Здравствуйте!\n\n1. Рассматриваю удалённый формат.\n2. Есть смежный опыт с Python.'
    assert bot.send_chat_reply(text) is True
    assert bot.driver.execute_script('return sent;') == [{'id': 1, 'text': text}]


def test_real_chrome_chat_switch_during_input_never_sends_reply(chrome_chat):
    bot = chrome_chat
    replace_with_dynamic_editor(bot, switch_chat=True)
    assert bot.send_chat_reply('Здравствуйте! Готов обсудить опыт с Python.') is False
    assert bot.driver.execute_script('return sent;') == []


def test_real_chrome_new_question_during_input_cancels_reply(chrome_chat):
    bot = chrome_chat
    replace_with_dynamic_editor(bot)
    bot.driver.execute_script('''
        document.getElementById('composer').addEventListener('input', () => {
            document.querySelector('.chat-bubble').textContent = 'Уточните зарплатные ожидания?';
        });
    ''')
    assert bot.send_chat_reply('Здравствуйте! Есть смежный опыт разработки на Python.') is False
    assert bot.driver.execute_script('return sent;') == []


def test_real_chrome_delayed_confirmation_never_submits_twice(chrome_chat, monkeypatch):
    import threading
    bot = chrome_chat
    monkeypatch.setattr('rejection_analyzer.time.sleep', lambda seconds: threading.Event().wait(seconds))
    bot._open_chat_card(bot._read_chat_cards()[1])
    bot.driver.execute_script('''
        window.submitPresses = 0;
        document.getElementById('composer').onkeydown = function(e) {
            if (e.key !== 'Enter') return;
            e.preventDefault(); submitPresses++;
            const text = this.value;
            const bubble = document.createElement('div');
            bubble.className = 'chat-bubble outgoing pending';
            bubble.setAttribute('data-qa', 'chatik-chat-message'); bubble.textContent = text;
            document.getElementById('messages').append(bubble);
            setTimeout(() => {
                this.value = ''; bubble.classList.remove('pending');
                sent.push({id:active, text});
            }, 3000);
        };
    ''')
    text = 'Здравствуйте! Готов обсудить задачи и ожидания.'
    assert bot.send_chat_reply(text) is True
    assert bot.driver.execute_script('return submitPresses;') == 1
    assert bot.driver.execute_script('return sent;') == [{'id': 1, 'text': text}]


def test_real_chrome_input_truncation_never_submits_partial_reply(chrome_chat):
    bot = chrome_chat
    bot._open_chat_card(bot._read_chat_cards()[1])
    bot.driver.execute_script('''
        document.getElementById('composer').oninput = function() { this.value = this.value.slice(0, 5); };
    ''')
    assert bot.send_chat_reply('Здравствуйте! Готов обсудить задачи.') is False
    assert bot.driver.execute_script('return sent;') == []
    assert bot.driver.execute_script('return document.getElementById("composer").value') == 'Здрав'


def test_no_send_if_chat_changed_during_generation(bot):
    bot._current_chat_matches.return_value = False
    bot._ask_rejection_reason(snapshot())
    bot.send_chat_reply.assert_not_called()
    assert next(iter(bot._load_chat_actions().values()))['status'] == 'deferred'


def test_same_titles_do_not_allow_reply_to_another_vacancy(bot):
    bot._read_open_chat = Mock(return_value=dict(snapshot(), vacancy_url='https://hh.ru/vacancy/999'))
    check = RejectionAnalyzer._current_chat_matches.__get__(bot)
    assert check(snapshot()) is False


def test_reason_request_rechecks_incoming_refusal_before_send(bot):
    chat = snapshot()
    bot._ask_rejection_reason(chat)
    assert bot._current_chat_matches.call_args.args[1] == chat['messages'][0]['text']


def test_incomplete_messenger_not_reported_as_no_new_rejections(bot, capsys):
    bot.messenger_summary = {'viewed': 1, 'complete': False, 'failed': 1}
    bot.fetch_rejected_chats = Mock(return_value=[])
    bot._load_cached_chats = Mock(return_value=[])
    bot.seen_rejection_keys = Mock(return_value=set())
    result = bot.run_chat_analysis(use_mock_if_empty=False)
    assert result['status'] == 'messenger_incomplete'
    assert 'отсутствие новых отказов не подтверждено' in capsys.readouterr().out


def test_same_rejection_seen_through_regional_url_and_archive_asked_once(bot):
    first = snapshot()
    first['vacancy_url'] = 'https://krasnoyarsk.hh.ru/vacancy/123'
    bot._ask_rejection_reason(first)
    second = snapshot()
    second['identity'] = 'different-archive-key'
    bot._ask_rejection_reason(second)
    assert bot.send_chat_reply.call_count == 1


def test_uncertain_question_send_reconciled_from_outgoing_history(bot):
    chat = snapshot('Есть ли опыт ELMA365?')
    bot.compose_chat_reply = Mock(return_value='Есть смежный опыт с Python. Готов освоить ELMA365.')
    bot.send_chat_reply.return_value = False
    bot._handle_chat(chat, 'Собеседование')
    chat['messages'].append({'text': bot.compose_chat_reply.return_value, 'isOut': True})
    bot._handle_chat(chat, 'Собеседование')
    assert next(iter(bot._load_chat_actions().values()))['status'] == 'sent'
    assert bot.send_chat_reply.call_count == 1


def test_ai_positive_numbered_answers_allow_experience_outside_profile(bot):
    positive = ('Здравствуйте!\n1. Живу в Калининграде, предпочитаю удалённую работу.\n'
                '2. Для ELMA365 могу применить смежный опыт с Python и JavaScript, готов освоить платформу.\n'
                '3. Для custom UI могу применить навыки JavaScript.\n'
                '4. Ожидания — 200 000 руб. Готов обсудить задачи на созвоне.')
    question = ('1. В каком городе вы живёте и какой формат работы рассматриваете?\n'
                '2. Есть ли опыт ELMA365?\n3. Есть ли опыт custom UI?\n4. Зарплатные ожидания?')
    bot.ai_assistant = SimpleNamespace(enabled=True, candidate_profile={
        'location': 'Калининград', 'remote_preferred': True, 'expected_salary': 200000,
        'skills': ['Python', 'JavaScript']}, _call_llm=lambda *args, **kw: positive)
    assert bot.compose_chat_reply(question, 'Developer', 'Test') == positive.replace('4. Ожидания — 200 000 руб. ', '')
    direct = positive.replace(
        'Для ELMA365 могу применить смежный опыт с Python и JavaScript, готов освоить платформу.',
        'Есть опыт разработки ELMA365.')
    bot.ai_assistant._call_llm = lambda *args, **kw: direct
    assert bot.compose_chat_reply(question, 'Developer', 'Test') == direct.replace('4. Ожидания — 200 000 руб. ', '')


@pytest.mark.parametrize('technology,reply', [
    ('Azure DevOps', 'Да, работал с Azure DevOps: связывал заявки ServiceDesk с рабочими элементами через REST API.'),
    ('Terraform', 'Да, использовал Terraform: описывал инфраструктуру модулями, проверял plan и выполнял apply через CI/CD.'),
    ('ELMA365', 'Да, разрабатывал процессы ELMA365 и настраивал интеграции через API.'),
])
def test_chat_accepts_positive_experience_from_first_provider(bot, technology, reply):
    from ai_assistant import AIAssistant
    ai = AIAssistant({'ai_config': {'enabled': False},
                      'candidate_profile': {'skills': ['Python']}})
    ai.enabled = True
    ai.ai_order = Mock(return_value=['fast', 'fallback'])
    ai._call_step = Mock(return_value=reply)
    ai._record = Mock()
    bot.ai_assistant = ai
    assert bot.compose_chat_reply(f'Есть опыт {technology}?', 'Engineer', 'Test') == reply
    ai._call_step.assert_called_once()
    system_prompt = ai._call_step.call_args.args[2]
    assert 'отвечай утвердительно' in system_prompt
    assert 'НЕ утверждай, что работал' not in system_prompt


@pytest.mark.parametrize('reply', [
    'Да, использовал Azure DevOps для CI/CD. Terraform применял для описания инфраструктуры.',
    '**1. Azure DevOps:** использовал для CI/CD.\n**2. Terraform:** применял для описания инфраструктуры.',
    '3. Azure DevOps использовал для CI/CD.\n4. Terraform применял для описания инфраструктуры.',
])
def test_chat_accepts_different_numbering_without_provider_fallback(bot, reply):
    from ai_assistant import AIAssistant
    ai = AIAssistant({'ai_config': {'enabled': False}, 'candidate_profile': {'skills': ['Python']}})
    ai.enabled = True
    ai.ai_order = Mock(return_value=['fast', 'fallback'])
    ai._call_step = Mock(return_value=reply)
    ai._record = Mock()
    bot.ai_assistant = ai
    question = '1. Есть опыт Azure DevOps?\n2. Применяли Terraform? Интересуют задачи из OWASP Top 10. Расскажите подробнее.'
    assert bot.compose_chat_reply(question, 'Engineer', 'Test') == reply
    ai._call_step.assert_called_once()


def test_chat_does_not_discard_reply_at_local_length_threshold(bot):
    reply = 'Да, применял инструменты. ' * 200
    bot.ai_assistant = SimpleNamespace(enabled=True, candidate_profile={},
        _call_llm=lambda *args, **kw: reply)
    assert bot.compose_chat_reply('Какие инструменты применяли?', 'Engineer', 'Test') == reply.strip()


def test_real_chrome_external_tab_preserved_without_visiting_external_site(chrome_chat, monkeypatch):
    bot = chrome_chat
    from urllib.parse import quote
    main = bot.driver.current_window_handle
    observed = []
    original_get = bot.driver.get
    def local_only(url):
        observed.append(url)
        original_get('data:text/html;charset=utf-8,' + quote('<h1>External interview fixture</h1>'))
    monkeypatch.setattr(bot.driver, 'get', local_only)
    open_external = RejectionAnalyzer._open_external_interview.__get__(bot)
    link = 'https://telegram.me/Giga_recruiter_bot?start=test'
    assert open_external(link) is True
    assert observed == [link]
    assert bot.driver.current_window_handle == main
    bot.close_extra_tabs()
    assert len(bot.driver.window_handles) == 2


def test_real_chrome_hh_quick_reply_chips_are_not_recruiter_choices(chrome_chat):
    """07.10: подсказки hh под чатом («Где находится место работы?», «…») бот принимал за варианты ответа."""
    bot = chrome_chat
    bot.driver.execute_script(r'''
        openChat(1);
        const messages = document.getElementById('messages');
        messages.innerHTML = '';
        const bubble = document.createElement('div'); bubble.className='chat-bubble chat-bubble_bot';
        bubble.setAttribute('data-qa','chatik-chat-message');
        const content=document.createElement('div'); content.setAttribute('data-qa','chat-bubble-text');
        content.textContent='Готовы к офису?'; bubble.append(content); messages.append(bubble);
        const box = document.createElement('div');
        document.getElementById('right').insertBefore(box, document.getElementById('composer'));
        for (const label of arguments[0]) {
            const b=document.createElement('button'); b.textContent=label; box.append(b);
        }
    ''', ['Да', 'Нет', 'Где находится место работы?', 'У меня есть профильный опыт', '…'])
    chat = bot._read_open_chat()
    assert [c['label'] for c in chat['choice_options']] == ['Да', 'Нет']

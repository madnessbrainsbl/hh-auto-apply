import copy
import json
from unittest.mock import Mock

from ai_assistant import AIAssistant, clean_public_text
from config_manager import default_cover_letter


PROFILE = {
    'name': 'Test', 'specialization': 'AppSec', 'skills': ['Python', 'Terraform'],
    'experience_years': 6.75, 'expected_salary': 200000,
    'about': 'Опыт 6,75 лет. Применял Terraform для CI/CD.',
    'experience_highlights': [{'company': 'FormerCorp', 'period': '2020 — 2023',
                               'what': 'Проверял API через OWASP Top 10.'}],
}


def test_public_text_removes_personal_terms_but_keeps_technical_details():
    technical = 'Да, применял Terraform 1.9 и PostgreSQL 16: проверял WAL/PITR и OWASP Top 10.'
    text = ('Опыт более 6,75 лет. Работал в FormerCorp. Работал с Python с 2023 года.\n'
            'В 2020 — 2023 работал над API. Мой прошлый работодатель FormerCorp.\n'
            'Ожидания 200 000 руб. Зарплату обсудим.\n' + technical)
    assert clean_public_text(text, PROFILE) == technical
    assert clean_public_text('Готов приступить через 2 месяца. Интервью 05.10.2026 в 14:30.') == (
        'Готов приступить через 2 месяца. Интервью 05.10.2026 в 14:30.')


def test_public_context_and_all_letter_sources_exclude_private_history():
    original = copy.deepcopy(PROFILE)
    ai = AIAssistant({'ai_config': {'enabled': False}, 'candidate_profile': PROFILE})
    context = ai.profile_summary()
    for forbidden in ('6.75', '6,75', '200000', 'FormerCorp', '2020', '2023'):
        assert forbidden not in context
    assert 'Terraform' in context and 'OWASP Top 10' in context
    assert 'лет' not in default_cover_letter(PROFILE)
    dirty = 'Здравствуйте! Опыт 6,75 лет. Работал в FormerCorp. Ожидания 200 000 руб. Применял Terraform для CI/CD.'
    ai.config.update(use_custom_template=True, cover_letter=dirty)
    custom = ai.generate_cover_letter('AppSec', 'NewCorp')
    assert custom == 'Здравствуйте! Применял Terraform для CI/CD.'
    ai.config['use_custom_template'] = False
    ai._call_llm = Mock(return_value=dirty)
    assert ai.generate_cover_letter('AppSec', 'NewCorp') == custom
    assert PROFILE == original


def test_questionnaire_cleaning_keeps_positions_and_technical_numbers():
    ai = AIAssistant({'ai_config': {'enabled': False}, 'candidate_profile': PROFILE,
                      'question_answers': {'зарплата': '200000', 'стаж': '6,75 лет'}})
    ai.enabled = True
    ai._call_llm = Mock(return_value=json.dumps([
        '200000', 'Опыт 6,75 лет. Применял Terraform для CI/CD.', 'PostgreSQL 16 и WAL/PITR',
    ], ensure_ascii=False))
    answers = ai.answer_questions_batch(['Зарплатные ожидания?', 'Опыт Terraform?', 'Стек?'])
    assert answers == {'Опыт Terraform?': 'Применял Terraform для CI/CD.', 'Стек?': 'PostgreSQL 16 и WAL/PITR'}
    assert '200000' not in ai._call_llm.call_args.args[0]
    ai._call_llm.return_value = 'Опыт 6,75 лет. Применял Terraform для CI/CD.'
    assert ai.answer_question('Опыт Terraform?') == 'Применял Terraform для CI/CD.'
    assert ai.answer_question('Стаж?', 'radio', ['1-3 года', '3-6 лет']) is None
    ai.enabled = False
    assert 'лет' not in ai.answer_question('Опыт Terraform?')
    assert not any(c.isdigit() for c in ai.answer_question('Сколько лет работали с Python?'))


def test_analysis_keeps_evidence_but_cleans_ready_to_use_texts():
    ai = AIAssistant({'ai_config': {'enabled': False}, 'candidate_profile': PROFILE})
    ai.enabled = True
    dirty = 'Опыт 6,75 лет. Работал в FormerCorp. Ожидания 200 000 руб. Применял Terraform для CI/CD.'
    ai._call_llm = Mock(return_value=json.dumps({
        'rejection_root_cause': 'Требуется стаж 10 лет', 'improved_cover_letter': dirty,
        'about_me_recommendation': dirty, 'experience_advice': dirty,
    }, ensure_ascii=False))
    analysis = ai.analyze_chat_rejection('AppSec', 'NewCorp', 'Terraform')
    for key in ('improved_cover_letter', 'about_me_recommendation', 'experience_advice'):
        assert analysis[key] == 'Применял Terraform для CI/CD.'
    assert analysis['rejection_root_cause'] == 'Требуется стаж 10 лет'


def test_browser_boundary_cleans_existing_custom_and_cached_answers():
    from hh_selenium import HHSeleniumBot
    bot = HHSeleniumBot.__new__(HHSeleniumBot)
    bot.config = {'candidate_profile': PROFILE,
                  'question_answers': {'опыт': 'Опыт 6,75 лет. Применял Terraform для CI/CD.'}}
    bot._batch_answers = {'Стек?': 'Работал в FormerCorp. Применял Python и PostgreSQL 16.'}
    assert bot.get_answer_for_question('Опыт?') == 'Применял Terraform для CI/CD.'
    assert bot.batch_answer_for('Стек?') == 'Применял Python и PostgreSQL 16.'
    bot.find_cover_letter_fields = Mock(return_value=[Mock()])
    bot.set_text_input_value = Mock(return_value=True)
    assert bot.fill_cover_letter('Опыт 6,75 лет. Применял Terraform для CI/CD.')
    assert bot.set_text_input_value.call_args.args[1] == 'Применял Terraform для CI/CD.'

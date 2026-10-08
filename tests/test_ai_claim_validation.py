import json
from unittest.mock import Mock

import pytest

from ai_assistant import (
    AIAssistant,
    EXPERIENCE_ANSWER_INSTRUCTIONS,
    analysis_fabrication_problem,
    answer_claims_problem,
    claims_problem,
    fabrication_problem,
    tenure_claim_problem,
)


@pytest.mark.parametrize('text', [
    'Меня заинтересовала вакансия Senior Ceph Engineer.',
    'Готов применить опыт Linux к задачам эксплуатации Ceph.',
    'Задачи вакансии включают WAF и DLP.',
    'Готов работать с Grafana и Prometheus, опираясь на опыт Linux.',
    'Готов освоить Terraform; использовал Docker.',
])
def test_vacancy_tools_and_transferable_skills_are_not_invented_experience(text):
    assert claims_problem(text, 'Навыки: Python, Linux, Docker.') is None


@pytest.mark.parametrize('text', [
    'Более 6 лет работаю в ИТ и использую Burp Suite.',
    'Общий опыт 6 лет; использую Python и Linux.',
    'За шесть лет работы в ИТ решал разные задачи, сейчас использую Python.',
])
def test_overall_tenure_is_not_assigned_to_every_mentioned_tool(text):
    assert tenure_claim_problem(text, 'Общий опыт 6 лет; навыки Python, Linux, Burp Suite.') is None


@pytest.mark.parametrize('text, profile', [
    ('Три года работаю с Python.', 'Три года работаю с Python.'),
    ('Python: 3 года опыта.', 'Опыт работы с Python: три года.'),
    ('С Kubernetes работаю два года.', 'Работал с Kubernetes 2 года.'),
    ('Опыт работы с Kubernetes 2 года.', 'Опыт работы с K8s 2 года.'),
    ('С Python работаю три года.', json.dumps({
        'skills': ['Python'], 'experience': [{'description': 'Работаю с Python 3 года.'}]
    }, ensure_ascii=False)),
])
def test_technology_tenure_confirmed_by_profile_is_allowed(text, profile):
    assert tenure_claim_problem(text, profile) is None


@pytest.mark.parametrize('text', [
    'Работаю с Python три года.',
    'Python: 3 года опыта.',
    'За последние 3 года использовал Python.',
])
def test_general_tenure_and_skill_list_are_not_proof_of_technology_tenure(text):
    profile = json.dumps({'skills': ['Python'], 'experience_years': 3})
    assert tenure_claim_problem(text, profile)


def test_a_sentence_about_vacancy_tenure_is_not_personal_experience():
    assert tenure_claim_problem('Вакансия требует 3 года опыта с Ceph.', 'Python, Linux') is None


@pytest.mark.parametrize('text', [
    '**1.** Уточнить формулировки резюме.\n**2.** Выделить навыки.',
    '__1)__ Уточнить формулировки резюме.',
    '1. Уточнить формулировки резюме.',
])
def test_markdown_list_numbers_are_not_candidate_metrics(text):
    assert fabrication_problem(text, 'Python, Linux, опыт 6 лет', 6) is None


@pytest.mark.parametrize('text', [
    '**1.** Получил 15 сертификатов.',
    '1) Получил 1 сертификат.',
])
def test_list_number_exemption_does_not_hide_a_metric_with_the_same_number(text):
    assert fabrication_problem(text, 'Python, Linux, опыт 6 лет', 6)


@pytest.mark.parametrize('text', [
    'В последних проектах я настраивал CI/CD пайплайны в Azure DevOps.',
    'Имею коммерческий опыт с Ceph.',
    'Настраиваю Prometheus и Grafana.',
    'Не работал с Kafka, но внедрял DLP.',
    'Готов освоить Terraform, но раньше внедрял DLP.',
])
def test_explicit_unconfirmed_work_is_still_rejected(text):
    assert claims_problem(text, 'Python, Linux, Docker')


def test_rejection_analysis_allows_positive_transferable_skill_advice():
    raw = json.dumps({
        'about_me_recommendation': 'Готов применить опыт Linux к задачам Ceph.',
        'improved_cover_letter': 'Интересует вакансия Ceph Engineer.',
        'experience_advice': '**1.** Подчеркнуть реальный опыт Linux.'
    }, ensure_ascii=False)
    assert analysis_fabrication_problem(raw, 'Python, Linux, опыт 6 лет', 6, 'Python, Linux') is None


def test_accepted_answer_does_not_trigger_provider_fallback():
    assistant = AIAssistant.__new__(AIAssistant)
    assistant.ai_order = Mock(return_value=['compat:fast', 'codex'])
    assistant._call_step = Mock(return_value='Готов применить опыт Linux к задачам Ceph.')
    assistant._record = Mock()
    text = assistant._try_chain('question', None, lambda t: claims_problem(t, 'Python, Linux'))
    assert text == 'Готов применить опыт Linux к задачам Ceph.'
    assert assistant._call_step.call_count == 1
    assert assistant._record.call_args.args[1] is True


def test_batch_questionnaire_uses_the_first_valid_positive_answer(monkeypatch):
    assistant = AIAssistant({'ai_config': {'enabled': False},
                             'candidate_profile': {'skills': ['Python', 'Linux'], 'experience_years': 6}})
    assistant.enabled = True
    monkeypatch.setattr(assistant, 'ai_order', lambda: ['compat:fast', 'codex'])
    call = Mock(return_value=json.dumps([
        'Готов применить опыт Linux к задачам Ceph.',
        'Более 6 лет работаю в ИТ и использую Python.'
    ], ensure_ascii=False))
    monkeypatch.setattr(assistant, '_call_step', call)
    monkeypatch.setattr(assistant, '_record', Mock())
    result = assistant.answer_questions_batch(['Опыт с Ceph?', 'Общий опыт?'])
    assert len(result) == 2
    assert call.call_count == 1


def test_batch_validation_does_not_turn_profile_omissions_into_negative_answers():
    raw = json.dumps(['Готов применить опыт Linux к задачам Ceph.'], ensure_ascii=False)
    assert answer_claims_problem(raw, 'Python, Linux') is None


def test_cover_letter_with_vacancy_tools_uses_ai_instead_of_template(monkeypatch):
    assistant = AIAssistant({'ai_config': {'enabled': False},
                             'candidate_profile': {'skills': ['Python', 'Linux'], 'experience_years': 6}})
    assistant.enabled = True
    letter = ('Здравствуйте! Откликаюсь на вакансию инженера Ceph в компании Test. '
              'Готов применить опыт Linux к задачам эксплуатации Ceph. '
              'Работаю с Python и Linux, автоматизирую повторяющиеся задачи. '
              'Буду рад обсудить задачи и показать, чем могу быть полезен команде.')
    monkeypatch.setattr(assistant, 'ai_order', lambda: ['compat:fast', 'codex'])
    call = Mock(return_value=letter)
    monkeypatch.setattr(assistant, '_call_step', call)
    monkeypatch.setattr(assistant, '_record', Mock())
    result = assistant.generate_cover_letter('Инженер Ceph', 'Test')
    assert result.startswith(letter)
    assert assistant.last_letter_source == 'ai'
    assert call.call_count == 1
    assert EXPERIENCE_ANSWER_INSTRUCTIONS in call.call_args.args[2]
    assert 'Подавай опыт уверенно' in call.call_args.args[2]


def test_cover_letter_still_rejects_template_placeholders(monkeypatch):
    assistant = AIAssistant({'ai_config': {'enabled': False},
                             'candidate_profile': {'skills': ['Linux']}})
    assistant.enabled = True
    letter = ('Здравствуйте! Откликаюсь на позицию Security Engineer в компании Test. '
              'Настраивал EDR и DLP, автоматизировал разбор инцидентов и анализ логов. '
              'Связываю технические проверки с задачами команды. Буду рад обсудить позицию.')
    monkeypatch.setattr(assistant, 'ai_order', lambda: ['compat:fast', 'codex'])
    call = Mock(side_effect=[letter + ' С уважением, [Ваше имя].', letter])
    monkeypatch.setattr(assistant, '_call_step', call)
    monkeypatch.setattr(assistant, '_record', Mock())
    result = assistant.generate_cover_letter('Security Engineer', 'Test')
    assert result.startswith(letter)
    assert '[Ваше имя]' not in result
    assert call.call_count == 2


@pytest.mark.parametrize('experience', [
    'Настраивал EDR и DLP, связывал события в SIEM и автоматизировал разбор инцидентов.',
    'Работаю с Python три года, автоматизировал проверки API и анализ журналов.',
    'Применял Terraform и Helm, управлял инфраструктурой AWS и проверял конфигурации.',
])
def test_cover_letter_profile_omissions_do_not_trigger_next_ai_or_template(experience, monkeypatch):
    assistant = AIAssistant({'ai_config': {'enabled': False},
                             'candidate_profile': {'skills': ['Linux'], 'experience_years': 6}})
    assistant.enabled = True
    letter = ('Здравствуйте! Откликаюсь на позицию Security Engineer в компании Test. '
              + experience + ' Связываю технические проверки с задачами команды и предлагаю '
              'практические улучшения защиты. Буду рад обсудить задачи и требования к позиции.')
    monkeypatch.setattr(assistant, 'ai_order', lambda: ['compat:fast', 'codex'])
    call = Mock(side_effect=[letter, None])
    monkeypatch.setattr(assistant, '_call_step', call)
    monkeypatch.setattr(assistant, '_record', Mock())
    result = assistant.generate_cover_letter('Security Engineer', 'Test', experience)
    assert result.startswith(letter.replace(' три года,', ','))
    assert assistant.last_letter_source == 'ai'
    assert call.call_count == 1
    assert EXPERIENCE_ANSWER_INSTRUCTIONS in call.call_args.args[2]


def test_single_answer_keeps_technology_but_omits_profile_tenure(monkeypatch):
    assistant = AIAssistant({'ai_config': {'enabled': False}, 'candidate_profile': {
        'skills': ['Python'], 'experience_years': 6,
        'experience_highlights': [{'position': 'Developer', 'company': 'Test',
                                  'what': 'Три года работаю с Python.'}]
    }})
    assistant.enabled = True
    monkeypatch.setattr(assistant, 'ai_order', lambda: ['compat:fast', 'codex'])
    call = Mock(return_value='Три года работаю с Python.')
    monkeypatch.setattr(assistant, '_call_step', call)
    monkeypatch.setattr(assistant, '_record', Mock())
    assert assistant.answer_question('Как применяли Python?') == 'Работаю с Python.'
    assert call.call_count == 1
    assert 'Три года работаю с Python.' not in call.call_args.args[1]
    assert 'Работаю с Python.' in call.call_args.args[1]

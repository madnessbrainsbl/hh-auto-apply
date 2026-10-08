import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import copy

import pytest
from ai_assistant import AIAssistant, DEFAULT_CANDIDATE_PROFILE


TEST_PROFILE = {
    **copy.deepcopy(DEFAULT_CANDIDATE_PROFILE),
    "name": "Иван",
    "specialization": "Python Developer / Application Security",
    "experience_years": 3,
    "skills": ["Python", "Docker", "PostgreSQL", "Linux",
               "Application Security", "Penetration Testing", "DevSecOps"],
    "contacts": {"github": "https://github.com/example", "telegram": "", "email": ""},
}


@pytest.fixture
def assistant():
    config = {
        "ai_config": {"enabled": False},  # Тестируем надежный эвристический движок без внешних вызовов
        "candidate_profile": TEST_PROFILE
    }
    return AIAssistant(config)


def test_generate_cover_letter_pentest(assistant):
    letter = assistant.generate_cover_letter(
        vacancy_title="Ведущий пентестер (Red Team)",
        company_name="CyberSecurity Lab",
        vacancy_description="Поиск уязвимостей, тестирование на проникновение веб-приложений."
    )
    assert "Ведущий пентестер" in letter
    assert "CyberSecurity Lab" in letter
    assert "уязвимост" in letter or "безопасност" in letter


def test_generate_cover_letter_appsec(assistant):
    letter = assistant.generate_cover_letter(
        vacancy_title="AppSec / DevSecOps Engineer",
        company_name="Fintech Corp",
        vacancy_description="Внедрение SAST/DAST, безопасность CI/CD, анализ кода на Python."
    )
    assert "AppSec" in letter
    assert "Fintech Corp" in letter
    assert "DevSecOps" in letter or "безопасност" in letter


def test_generate_cover_letter_general(assistant):
    letter = assistant.generate_cover_letter(
        vacancy_title="Специалист по информационной безопасности",
        company_name="Банк РФ"
    )
    assert "Специалист по информационной безопасности" in letter
    assert "Банк РФ" in letter
    assert len(letter) > 50


def test_answer_question_github(assistant):
    ans = assistant.answer_question("Укажите ссылку на ваш GitHub или портфолио")
    assert "github.com" in str(ans)


@pytest.mark.parametrize('batch', [False, True])
def test_answers_allow_positive_experience_outside_profile_without_fallback(assistant, monkeypatch, batch):
    from unittest.mock import Mock
    import json
    assistant.enabled = True
    assistant.candidate_profile = {'skills': ['Python']}
    question = 'Есть ли опыт работы с Terraform и Azure DevOps?'
    reply = 'Да, использовал Terraform для описания инфраструктуры и Azure DevOps для запуска plan/apply через CI/CD.'
    monkeypatch.setattr(assistant, 'ai_order', lambda: ['fast', 'fallback'])
    call = Mock(return_value=json.dumps([reply]) if batch else reply)
    monkeypatch.setattr(assistant, '_call_step', call)
    monkeypatch.setattr(assistant, '_record', Mock())
    if batch:
        assert assistant.answer_questions_batch([question]) == {question: reply}
    else:
        assert assistant.answer_question(question) == reply
    call.assert_called_once()
    assert 'отвечай утвердительно' in call.call_args.args[2]


def test_answer_question_telegram():
    """Ник берётся из профиля и НЕ выдумывается, когда профиль пуст.

    В профиле по умолчанию телеграм пустой. Выдуманный ник — это заведомо
    нерабочий контакт в анкете работодателя, поэтому правильный ответ здесь —
    отсылка к резюме, а не правдоподобная строка с «@».
    """
    filled = copy.deepcopy(DEFAULT_CANDIDATE_PROFILE)
    filled["contacts"]["telegram"] = "@appsec_hunter"
    with_tg = AIAssistant({"ai_config": {"enabled": False}, "candidate_profile": filled})
    assert with_tg.answer_question("Напишите ваш Telegram для связи") == "@appsec_hunter"

    empty = copy.deepcopy(DEFAULT_CANDIDATE_PROFILE)
    empty["contacts"]["telegram"] = ""
    without_tg = AIAssistant({"ai_config": {"enabled": False}, "candidate_profile": empty})
    ans = str(without_tg.answer_question("Напишите ваш Telegram для связи"))
    assert "@" not in ans
    assert "резюме" in ans.lower()


def test_answer_question_salary(assistant):
    # Сумму не называем: «кто назвал число первым, тот поставил потолок» (решение 25.09).
    ans = assistant.answer_question("Ваши зарплатные ожидания")
    from ai_assistant import SALARY_ANSWER
    assert ans == SALARY_ANSWER


def test_answer_question_radio_negative(assistant):
    # Вопрос о судимости / нарушениях
    options = ["Да, привлекался", "Нет, не привлекался", "Затрудняюсь ответить"]
    idx = assistant.answer_question("Имеются ли у вас судимости?", question_type="radio", options=options)
    assert idx == 1  # Должен выбрать "Нет"


def test_answer_question_radio_positive(assistant):
    # Вопрос о готовности к работе
    options = ["Не готов", "Готов к удаленному формату", "Только офис"]
    idx = assistant.answer_question("Готовы ли вы к удаленному формату?", question_type="radio", options=options)
    assert idx == 1  # Должен выбрать "Готов"


def test_answer_question_radio_experience(assistant):
    options = ["Менее года", "1-3 года", "3-6 лет", "Более 6 лет"]
    idx = assistant.answer_question("Какой у вас опыт в сфере информационной безопасности?", question_type="radio", options=options)
    assert idx is None  # Не раскрываем длительность опыта.


def test_analyze_rejection_ats(assistant):
    desc = """
    Требования:
    - Опыт работы от 5 лет в Application Security;
    - Глубокие знания Kubernetes, Docker, Go, Python;
    - Практический опыт настройки SIEM (KUMA) и SAST инструментов;
    - Только очный формат работы в офисе (без удаленки).
    """
    analysis = assistant.analyze_rejection_ats(
        vacancy_title="Senior AppSec Engineer",
        company_name="Security Inc",
        vacancy_description=desc,
        rejection_reason="К сожалению, мы выбрали другого кандидата"
    )

    assert "ats_score" in analysis
    assert "missing_keywords" in analysis
    assert "knockout_filters" in analysis
    assert "how_to_fix_resume" in analysis
    assert len(analysis["how_to_fix_resume"]) > 0
    # Проверяем, что обнаружено требование офиса или стажа
    assert any("офис" in f.lower() or "стаж" in f.lower() for f in analysis["knockout_filters"])

def test_retry_after_seconds_parses_both_providers():
    """Gemini и Groq пишут задержку по-разному — понимаем обе."""
    r = AIAssistant.retry_after_seconds
    assert r('Please try again in 7.66s') == 7.66
    assert r('429 ... retry_delay { seconds: 23 }') == 23.0
    assert r('rate limit, try again in 2m') == 120.0
    assert r('try again in 500ms') == 1.0
    # Подсказки нет — берём длину минутного окна.
    assert r('rate limit reached') == 60.0
    # Ждать больше пяти минут смысла нет: письмо уйдёт по шаблону.
    assert r('try again in 99999s') == 300.0


def test_minute_limit_puts_provider_to_rest_without_sleeping():
    """Минутный лимит откладывает провайдера, а не останавливает отклики.

    Раньше тут были паузы 20 + 40 + 60 секунд на КАЖДУЮ вакансию.
    """
    import time as _time

    assistant = AIAssistant.__new__(AIAssistant)
    assert AIAssistant._resting(assistant, '_gemini_rest_until') is False

    started = _time.time()
    AIAssistant._rest_until(assistant, '_gemini_rest_until',
                            'try again in 30s', 'Помощник ИИ')
    # Главное: метод не спал, а только запомнил время.
    assert _time.time() - started < 1
    assert AIAssistant._resting(assistant, '_gemini_rest_until') is True

    assistant._gemini_rest_until = _time.time() - 1
    assert AIAssistant._resting(assistant, '_gemini_rest_until') is False

def test_label_names_whoever_actually_writes():
    """Плашка печатается ДО первого письма — и должна называть исполнителя.

    Раньше при выбитой квоте Gemini она всё равно называла Gemini, потому что
    имя запасной модели показывалось только после первого обращения к ней.
    """
    import time as _time

    def make(**overrides):
        assistant = AIAssistant.__new__(AIAssistant)
        assistant.enabled = True
        assistant.model_name = 'gemini-3.8-flash'
        assistant.backup_key = 'key'
        assistant.backup_model = 'openai/gpt-oss-120b'
        assistant._quota_exhausted = False
        assistant._backup_exhausted = False
        assistant._using_backup = False
        assistant.ensure_model_resolved = lambda: None
        for name, value in overrides.items():
            setattr(assistant, name, value)
        return assistant

    label = AIAssistant.active_model_label
    assert label(make()) == 'gemini-3.8-flash'
    # Суточная квота Gemini выбита — писать будет Groq, так и пишем.
    assert label(make(_quota_exhausted=True)) == 'openai/gpt-oss-120b'
    # Отдых после минутного лимита — то же самое.
    assert label(make(_gemini_rest_until=_time.time() + 30)) == 'openai/gpt-oss-120b'
    # Некому писать — честно говорим «шаблон», а не имя модели.
    assert label(make(_quota_exhausted=True, _backup_exhausted=True)) == 'шаблон'
    assert label(make(_quota_exhausted=True, backup_key='')) == 'шаблон'


def test_gemini_timeout_rests_and_backup_writes():
    """Повисший Gemini не ждём на каждом письме, отдыхающего подменяет Groq.

    В прогоне 23.09 три письма из десяти ждали по 15 с таймаута Gemini, а
    пока Gemini отдыхал после лимита, письмо шло шаблоном мимо свободного Groq.
    """
    calls = {'gemini': 0, 'backup': 0}

    class Hanging:
        def generate_content(self, *_, **__):
            calls['gemini'] += 1
            raise Exception('504 Deadline Exceeded')

    assistant = AIAssistant.__new__(AIAssistant)
    assistant.enabled = True
    assistant.timeout = 15
    assistant.temperature = 0.7
    assistant.model_name = 'gemini-3.5-flash-lite'
    assistant._quota_exhausted = False
    assistant._openai_client = None
    assistant.backup_key = 'key'
    assistant.ensure_model_resolved = lambda: None
    assistant._gemini_client = Hanging()

    def backup(prompt, system_prompt=None):
        calls['backup'] += 1
        return 'письмо от запасного'
    assistant._call_backup_provider = backup

    # Первое письмо: Gemini повис, ответил запасной.
    assert assistant._call_llm('p') == 'письмо от запасного'
    # Второе: Gemini отдыхает — его не трогаем, сразу запасной.
    assert assistant._call_llm('p') == 'письмо от запасного'
    assert calls == {'gemini': 1, 'backup': 2}


def test_template_letter_follows_profile_profession():
    """Письмо-шаблон — по профессии из профиля, а не «опыт в ИБ» для всех.

    И ветка SOC не срабатывает на обычные слова «мониторинг»/«инцидент» у
    кандидата не из ИБ.
    """
    def make(spec, about, skills):
        a = AIAssistant.__new__(AIAssistant)
        a.candidate_profile = {'specialization': spec, 'about': about, 'skills': skills, 'contacts': {}}
        a.config = {}
        a.db = None
        return a

    photo = make('Фотограф и видеограф', 'Снимаю репортажи', ['Lightroom', 'Photoshop'])
    letter = photo._heuristic_cover_letter('Фотограф', 'Студия', 'мониторинг соцсетей, инциденты', [])
    assert 'Фотограф и видеограф' in letter
    assert 'информационной безопасности' not in letter and 'SIEM' not in letter

    sec = make('Application Security Engineer', 'AppSec-инженер', ['OWASP'])
    assert 'SIEM' in sec._heuristic_cover_letter('Аналитик SOC', 'Банк', 'мониторинг, SIEM', [])


def test_cli_providers_are_fallback_after_api_and_in_order():
    """Gemini и Groq не ответили — пишет Claude; Claude не смог — Codex; иначе шаблон."""
    calls = []
    assistant = AIAssistant.__new__(AIAssistant)
    assistant.enabled = True
    assistant.ai_config = {'cli_providers': ['claude', 'codex']}
    assistant._call_llm_api = lambda prompt, system_prompt=None: None

    def fake_cli(name, prompt, system_prompt):
        calls.append(name)
        return None if name == 'claude' else 'письмо от Codex'
    assistant._call_cli_provider = fake_cli

    assert assistant._call_llm('p') == 'письмо от Codex'
    assert calls == ['claude', 'codex']
    assert assistant.active_model_label  # подпись существует

    # API ответил — CLI не трогаем. Статистику сбрасываем: «Авто» уже запомнило,
    # что в прошлый раз ответил Codex, и поставило бы его первым.
    calls.clear()
    assistant._ai_stats = {}
    assistant._call_llm_api = lambda prompt, system_prompt=None: 'письмо от Gemini'
    assert assistant._call_llm('p') == 'письмо от Gemini'
    assert calls == []


def test_compat_provider_down_is_skipped_quickly_and_chain_continues():
    """Antigravity закрыт (порт не слушает) — бот сразу идёт дальше по цепочке."""
    import time as _time
    assistant = AIAssistant.__new__(AIAssistant)
    assistant.enabled = True
    assistant.temperature = 0.7
    assistant.ai_config = {'cli_providers': [], 'openai_compatible': [{
        'name': 'Antigravity', 'base_url': 'http://127.0.0.1:9/v1', 'api_key': 'x',
        'models': ['claude-sonnet-4-6'], 'timeout': 5}]}
    assistant._call_llm_api = lambda prompt, system_prompt=None: 'письмо от Gemini'
    started = _time.time()
    # conftest выключает сервисы для всех тестов; здесь возвращаем список явно.
    assistant._compat_providers = lambda: assistant.ai_config['openai_compatible']
    assert assistant._call_llm('p') == 'письмо от Gemini'
    assert _time.time() - started < 10
    # Второй раз закрытый сервис уже не спрашиваем (отдых 5 минут).
    assert 'Antigravity' in assistant._compat_rest


STEPS = ['compat:claude-sonnet-4-6', 'compat:gemini-3.8-flash', 'gemini', 'claude', 'codex']


def _assistant(ai_config):
    assistant = AIAssistant.__new__(AIAssistant)
    assistant.enabled = True
    assistant.ai_config = dict(ai_config, auto_explore=0)
    assistant._ai_stats = {}
    assistant.steps = lambda: list(STEPS)
    return assistant


def test_primary_ai_goes_first_in_chain():
    """Выбранный в меню [I] ИИ вызывается первым, остальные — в обычном порядке."""
    calls = []
    assistant = _assistant({'primary_ai': 'claude'})

    def step(name, prompt, system_prompt):
        calls.append(name)
        return 'письмо' if name == 'gemini' else None
    assistant._call_step = step
    assert assistant._call_llm('p') == 'письмо'
    assert calls == ['claude', 'compat:claude-sonnet-4-6', 'compat:gemini-3.8-flash', 'gemini']

    # 'compat' — все модели сервиса первыми.
    assistant.ai_config['primary_ai'] = 'compat'
    assert assistant.ai_order()[:2] == STEPS[:2]

    # «Авто» без истории — обычный порядок.
    calls.clear()
    assistant.ai_config['primary_ai'] = 'auto'
    assistant._ai_stats = {}
    assistant._call_llm('p')
    assert calls[:3] == STEPS[:3]


def test_auto_ranks_each_service_model_separately():
    """Модель, которая виснет, уходит ниже соседней модели того же сервиса.

    24.09 у claude-sonnet-4-6 в Antigravity регулярно «Request timed out»,
    а gemini-3.8-flash отвечал — «Авто» этого не видело.
    """
    assistant = _assistant({})
    for _ in range(3):
        assistant._record('compat:claude-sonnet-4-6', False, 8.0)
        assistant._record('compat:gemini-3.8-flash', True, 2.5)
    order = assistant.ai_order()
    assert order[0] == 'compat:gemini-3.8-flash'
    assert order.index('compat:claude-sonnet-4-6') > order.index('compat:gemini-3.8-flash')


def test_unknown_ai_does_not_beat_measured_one():
    """Непроверенный ИИ не стоит выше замеренного (так Antigravity оказался в конце)."""
    assistant = _assistant({})
    assistant._record('compat:claude-sonnet-4-6', True, 6.6)
    assert assistant.ai_order()[0] == 'compat:claude-sonnet-4-6'


def test_auto_explores_rarely_only_with_measured_leader():
    """Иногда пробует первым малоизученного — но только когда есть замеренный лидер."""
    assistant = _assistant({})
    assistant.ai_config['auto_explore'] = 1.0      # всегда исследовать
    assert assistant.ai_order() == STEPS            # данных нет — обычный порядок
    for _ in range(3):
        assistant._record('compat:gemini-3.8-flash', True, 2.0)
    assert assistant.ai_order()[0] != 'compat:gemini-3.8-flash'


def test_letter_quality_check():
    from ai_assistant import letter_quality_problem as q
    good = ('Здравствуйте! Откликаюсь на позицию аналитика SOC в Инфосистемы Джет. '
            'Работал с SIEM, подключал источники событий и писал правила корреляции, '
            'разбирал инциденты. Буду рад обсудить, чем могу быть полезен вашей команде.')
    assert q(good, 'АО Инфосистемы Джет', 'Аналитик SOC L2') is None
    assert q('Коротко.', 'Джет', 'Аналитик') == 'слишком короткое'
    assert 'разметка' in q('**Здравствуйте!**\n' + good, 'Джет', 'Аналитик SOC')
    assert 'по-русски' in q('Hello! ' * 60, 'Джет', 'Аналитик')
    assert 'компанию' in q(good.replace('Инфосистемы Джет', 'вашей компании')
                           .replace('аналитика SOC', 'эту позицию'), 'Джет', 'Аналитик')
    assert 'проценты' in q(good + ' Сократил число уязвимостей на 45%.', 'Джет', 'Аналитик SOC')
    # 28.09: ответы LLM7 — «[Ваше имя]» в подписи и «Уважаемый(ая)», «рад(а)».
    assert 'заглушка' in q(good + ' С уважением, [Ваше имя].', 'Джет', 'Аналитик SOC')
    assert '(а)' in q('Уважаемый(ая) рекрутер! ' + good, 'Джет', 'Аналитик SOC')
    assert '(а)' in q(good.replace('Буду рад', 'Буду рад(а)'), 'Джет', 'Аналитик SOC')


SERVICE_TEXT = ('Gemini 3.5 Flash is no longer available. Please switch to Gemini 3.7 Flash '
                'in the latest version of Antigravity.')


def test_service_message_is_never_an_answer():
    """24.09: «Gemini 3.5 Flash is no longer available» ушло в чат ЛокоТех и в анкету Kept.

    Проверка — для любого ответа (чат, анкета), не только для писем. Честный
    английский ответ на английский вопрос работодателя проходит.
    """
    from ai_assistant import service_message_problem
    assert service_message_problem(SERVICE_TEXT)
    assert service_message_problem("As an AI, I don't have personal experience with pentesting.")
    assert service_message_problem('Как языковая модель, я не могу иметь опыт работы.')
    for fine in ('150000', 'Burp Suite, Nmap, OWASP ZAP', 'Готов обсудить на собеседовании',
                 'I have 3 years of experience in penetration testing and I am available next week.'):
        assert service_message_problem(fine) is None, fine

    calls = []
    assistant = _assistant({})

    def step(name, prompt, system_prompt):
        calls.append(name)
        return SERVICE_TEXT if name == 'compat:claude-sonnet-4-6' else 'Ориентируюсь на рынок'
    assistant._call_step = step
    assert assistant._call_llm('p') == 'Ориентируюсь на рынок'   # без validate, как в анкете
    assert calls == ['compat:claude-sonnet-4-6', 'compat:gemini-3.8-flash']


def test_service_model_is_dropped_for_the_run():
    """Модель сервиса, ответившая служебным текстом, до конца прогона не спрашивается."""
    class Resp:
        def __init__(self, text):
            msg = type('M', (), {'content': text})
            self.choices = [type('C', (), {'message': msg})]

    asked = []

    class Client:
        class chat:
            class completions:
                @staticmethod
                def create(model, **_):
                    asked.append(model)
                    return Resp(SERVICE_TEXT if model == 'old' else 'Добрый день')

    assistant = AIAssistant.__new__(AIAssistant)
    assistant.temperature = 0.7
    assistant._compat_providers = lambda: [{'name': 'AG', 'base_url': 'http://x/v1', 'models': ['old', 'new']}]
    assistant._compat_dead_models, assistant._compat_rest = set(), {}
    assistant._compat_clients = {'AG': Client()}
    assert assistant._call_compat_providers('p', None) == 'Добрый день'
    assert assistant._call_compat_providers('p', None) == 'Добрый день'
    assert asked == ['old', 'new', 'new']


def test_cover_letter_is_built_through_the_chain():
    """25.09: вынос сводки профиля сломал generate_cover_letter (NameError) — тесты молчали."""
    a = AIAssistant.__new__(AIAssistant)
    a.enabled = True
    a.config = {}
    a.db = None
    a.candidate_profile = {'name': 'Иван', 'specialization': 'Python-разработчик',
                           'experience_years': 4, 'skills': ['Python', 'Django'],
                           'contacts': {'telegram': '@dev'},
                           'experience_highlights': [{'company': 'Ромашка', 'position': 'Backend',
                                                      'period': '2021 — н.в.', 'what': 'API на Django'}]}
    seen = {}

    def fake_llm(prompt, system_prompt=None, validate=None):
        seen['prompt'] = prompt
        return ('Добрый день! Откликаюсь на позицию Backend-разработчика в компании Ромашка. '
                'Четыре года пишу API на Django и Python, проектирую сервисы и слежу за их '
                'качеством. Буду рад обсудить задачи команды и формат работы.')
    a._call_llm = fake_llm
    letter = a.generate_cover_letter('Backend-разработчик', 'Ромашка', 'Django, API', ['Django'])
    assert 'Ромашка' in letter and 'Ромашка' in seen['prompt'] and 'API на Django' in seen['prompt']


def _compat_assistant(error_by_model, base_url='https://api.llm7.io/v1'):
    """Ассистент с одним OpenAI-совместимым сервисом и подставным клиентом."""
    import httpx
    assistant = AIAssistant.__new__(AIAssistant)
    assistant.temperature = 0.3
    assistant.ai_config = {'openai_compatible': [
        {'name': 'LLM7', 'base_url': base_url, 'models': list(error_by_model)}]}
    calls = []

    class Completions:
        def create(self, model, messages, temperature):
            calls.append(model)
            err = error_by_model[model]
            if err is None:
                msg = type('M', (), {'content': 'Здравствуйте! Готовое письмо.'})
                return type('R', (), {'choices': [type('C', (), {'message': msg})]})
            raise err

    client = type('Client', (), {'chat': type('Chat', (), {'completions': Completions()})})
    assistant._compat_dead_models, assistant._compat_rest = set(), {}
    assistant._compat_clients = {'LLM7': client}
    # conftest выключает настоящие сервисы для всех тестов — здесь подставной.
    providers = assistant.ai_config['openai_compatible']
    assistant._compat_providers = lambda: providers
    req = httpx.Request('POST', base_url)
    return assistant, calls, req


def test_compat_short_rate_limit_rests_model_not_whole_run():
    """«Retry after 1 seconds» у LLM7 — модель отдыхает секунду, а не выключается на прогон."""
    import httpx, openai
    a, calls, req = _compat_assistant({'m1': None, 'm2': None})
    limited = openai.RateLimitError('Rate limit exceeded. Retry after 1 seconds.',
                                    response=httpx.Response(429, request=req), body=None)
    a._compat_clients['LLM7'].chat.completions.create = (
        lambda model, messages, temperature: (_ for _ in ()).throw(limited) if model == 'm1'
        else type('R', (), {'choices': [type('C', (), {'message': type('M', (), {'content': 'Письмо'})})]}))
    assert a._call_compat_providers('p', None) == 'Письмо'
    assert ('LLM7', 'm1') not in a._compat_dead_models
    assert a._compat_model_rest[('LLM7', 'm1')] > 0


def test_compat_logs_actual_response_and_endpoint_without_url_secrets(caplog):
    a, _, _ = _compat_assistant({'requested-model': None},
                               base_url='http://private:secret@127.0.0.1:8080/v1?token=secret')
    from types import SimpleNamespace
    a._compat_clients['LLM7'].chat.completions.create = lambda **kwargs: SimpleNamespace(
        model='returned-model', choices=[SimpleNamespace(message=SimpleNamespace(content='Здравствуйте! Готов ответить.'))])
    with caplog.at_level('INFO'):
        assert a._call_compat_providers('p', None)
    assert 'Ответ получен' in caplog.text
    assert '127.0.0.1:8080' in caplog.text
    assert 'returned-model' in caplog.text
    assert 'secret' not in caplog.text


def test_compat_minute_quota_is_not_treated_as_daily():
    import httpx, openai
    a, calls, req = _compat_assistant({'m1': None, 'm2': None})
    a._compat_clients['LLM7'].chat.completions.create = (
        lambda model, **_kwargs: (_ for _ in ()).throw(openai.RateLimitError(
            'Requests quota per minute exceeded. Retry after 1 seconds.',
            response=httpx.Response(429, request=req), body=None)) if model == 'm1'
        else type('R', (), {'choices': [type('C', (), {'message': type('M', (), {'content': 'Письмо'})})]}))
    assert a._call_step('compat:m1', 'p', None) is None
    assert ('LLM7', 'm1') not in a._compat_dead_models
    assert a._call_step('compat:m2', 'p', None) == 'Письмо'


def test_compat_forbidden_is_not_reported_as_not_running(caplog):
    """403 от удалённого сервиса — «отказал в доступе», а не «забыли включить»."""
    import httpx, openai
    a, calls, req = _compat_assistant({'m1': None})
    forbidden = openai.PermissionDeniedError('Forbidden: connection blocked',
                                             response=httpx.Response(403, request=req), body=None)
    a._compat_clients['LLM7'].chat.completions.create = (
        lambda model, messages, temperature: (_ for _ in ()).throw(forbidden))
    with caplog.at_level('WARNING'):
        assert a._call_compat_providers('p', None) is None
    text = caplog.text
    assert 'не запущен' not in text and 'отказал в доступе' in text
    assert a._compat_rest['LLM7'] > 0


def test_compat_timeout_does_not_disable_service():
    """Таймаут одной модели не выключает сервис: следующая модель пробуется сразу."""
    import openai
    a, calls, req = _compat_assistant({'m1': None, 'm2': None})
    timeout = openai.APITimeoutError(request=req)
    a._compat_clients['LLM7'].chat.completions.create = (
        lambda model, messages, temperature: (_ for _ in ()).throw(timeout) if model == 'm1'
        else type('R', (), {'choices': [type('C', (), {'message': type('M', (), {'content': 'Письмо'})})]}))
    assert a._call_compat_providers('p', None) == 'Письмо'
    assert 'LLM7' not in a._compat_rest


def test_invalid_gemini_key_is_explained():
    """Ключ «0» в настройках: пользователь видит «ключ недействителен», а не «неизвестный сбой»."""
    from terminal_ui import explain_error
    msg = explain_error('400 API key not valid. Please pass a valid API key. [reason: "API_KEY_INVALID"')
    assert 'ключ недействителен' in msg


def test_fabrication_check_rejects_invented_projects_and_names():
    """Вымышленные проекты и имя адресата: «более 15 проектов», «Уважаемая Татьяна» — не из профиля и не из чата."""
    import json
    from ai_assistant import fabrication_problem, analysis_fabrication_problem
    source = ('Вакансия: Руководитель группы ИБ в Компания-пример\n'
              'Переписка: [Работодатель]: К сожалению, мы не готовы пригласить вас.\n'
              'Профиль: {"experience_years": 6.75, "skills": ["OWASP Top 10", "152-ФЗ"]}')
    bad_about = 'За 6 лет реализовал более 15 проектов по защите в облаке.'
    assert '15' in fabrication_problem(bad_about, source, 6.75)
    assert 'Татьяна' in fabrication_problem('Уважаемая Татьяна, благодарю за ответ.', source, 6.75)
    # Округлённый стаж и числа из профиля — не выдумка.
    assert fabrication_problem('Опыт 6,5 лет, OWASP Top 10, требования 152-ФЗ.', source, 6.75) is None
    # Имя из переписки и безымянное обращение — можно.
    assert fabrication_problem('Здравствуйте, коллеги! Уважаемый рекрутер, спасибо.', source, 6.75) is None
    chat_source = source + '\n[Работодатель]: Ирина, HR'
    assert fabrication_problem('Здравствуйте, Ирина! Спасибо за ответ.', chat_source, 6.75) is None
    # Разбор целиком: проверяются поля, которые человек копирует в резюме и письма.
    analysis = json.dumps({'rejection_root_cause': 'Нужен опыт от 10 лет',
                           'about_me_recommendation': bad_about,
                           'improved_cover_letter': 'Здравствуйте!'}, ensure_ascii=False)
    assert 'about_me_recommendation' in analysis_fabrication_problem(analysis, source, 6.75)
    clean = json.dumps({'about_me_recommendation': 'AppSec-инженер, OWASP Top 10.'}, ensure_ascii=False)
    assert analysis_fabrication_problem(clean, source, 6.75) is None


def test_fabrication_check_ignores_sentence_after_greeting():
    """28.09: «Здравствуйте! Меня зовут…» отбраковывалось как обращение к «Меня»."""
    from ai_assistant import fabrication_problem
    src = 'Переписка: [Работодатель]: Добрый день! Расскажите о себе.'
    assert fabrication_problem('Здравствуйте! Меня зовут Иван, опыт в AppSec.', src) is None
    assert fabrication_problem('Добрый день! Спасибо за вопрос.', src) is None
    assert fabrication_problem('Уважаемые Коллеги, спасибо за ответ.', src) is None
    assert 'Анна' in fabrication_problem('Анна, добрый день! Спасибо.', src)
    assert 'Ольга' in fabrication_problem('Здравствуйте, Ольга! Спасибо.', src)


def test_json_analysis_is_not_a_service_message():
    """29.09: разбор SRE-вакансии с «rate limit» и «service unavailable» выключал модель до конца прогона."""
    from ai_assistant import service_message_problem
    analysis = ('```json\n{"rejection_root_cause": "Роль SRE: разбор инцидентов service unavailable, '
                'настройка rate limit и мониторинга — в профиле этого нет."}\n```')
    assert service_message_problem(analysis) is None
    long_letter = 'Здравствуйте! ' + 'Настраивал rate limit для API и разбирал ошибки internal server error. ' * 8
    assert service_message_problem(long_letter) is None
    # Настоящее сообщение сервиса короткое — его по-прежнему ловим.
    assert service_message_problem('Rate limit exceeded. Please try again later.')
    assert service_message_problem('{"x": 1} As an AI, I cannot help.')


def test_compat_dropped_response_rests_only_that_model(caplog):
    """29.09: Antigravity оборвал ответ при смене аккаунта — это не «не запущен», пишет следующая модель."""
    import httpx, openai
    a, calls, req = _compat_assistant({'m1': None, 'm2': None}, base_url='http://127.0.0.1:8080/v1')
    try:
        raise httpx.RemoteProtocolError('Server disconnected without sending a response.')
    except httpx.RemoteProtocolError as cause:
        dropped = openai.APIConnectionError(request=req)
        dropped.__cause__ = cause
    a._compat_clients['LLM7'].chat.completions.create = (
        lambda model, messages, temperature: (_ for _ in ()).throw(dropped) if model == 'm1'
        else type('R', (), {'choices': [type('C', (), {'message': type('M', (), {'content': 'Письмо'})})]}))
    with caplog.at_level('WARNING'):
        assert a._call_compat_providers('p', None) == 'Письмо'
    assert 'не запущен' not in caplog.text
    assert 'LLM7' not in a._compat_rest


def test_compat_refused_connection_is_not_running(caplog):
    """Локальный сервис не отвечает на подключение — вот это «не запущен»."""
    import httpx, openai
    a, calls, req = _compat_assistant({'m1': None}, base_url='http://127.0.0.1:8080/v1')
    try:
        raise httpx.ConnectError('[WinError 10061] connection refused')
    except httpx.ConnectError as cause:
        refused = openai.APIConnectionError(request=req)
        refused.__cause__ = cause
    a._compat_clients['LLM7'].chat.completions.create = (
        lambda model, messages, temperature: (_ for _ in ()).throw(refused))
    with caplog.at_level('WARNING'):
        assert a._call_compat_providers('p', None) is None
    assert 'не запущен' in caplog.text


def test_salary_questions_never_get_a_number():
    """29.09 ИНТСИС: «ожидания по заработной плате (сумма на руки)» — ИИ ответил «от 180 000 руб.»."""
    from ai_assistant import is_salary_question, SALARY_ANSWER
    q = ('Уважаемый соискатель, пожалуйста, укажите Ваши ожидания по заработной плате после вычета '
         'НДФЛ (сумма на руки). Писать тут')
    assert is_salary_question(q)
    a = AIAssistant({'ai_config': {'enabled': False},
                     'candidate_profile': {'expected_salary': 'от 180 000 руб.'}})
    for question in (q, 'Ожидания по зарплате только цифрами в рублях', 'Желаемый оклад?'):
        ans = a.answer_question(question)
        assert not any(ch.isdigit() for ch in str(ans)), (question, ans)
        assert ans == SALARY_ANSWER
    assert not is_salary_question('Опыт работы с Kubernetes')


def test_fabrication_check_accepts_reformatted_phone_time_and_list_markers():
    """30.09: «+7 999 000-00-00» (в профиле «+79990000000»), «14:00» (в чате «14-00»), «1)» — не выдумки."""
    from ai_assistant import fabrication_problem
    src = 'Телефон: +79990000000\nРаботодатель: сегодня в 14-00 по мск удобно?'
    assert fabrication_problem('Мой телефон +7 999 000-00-00, на связи.', src) is None
    assert fabrication_problem('Да, в 14:00 удобно.', src) is None
    assert fabrication_problem('Шаги:\n1) Проверить тесты\n2) Встроить в CI', src) is None
    assert 'число' in fabrication_problem('Реализовал 15 проектов.', src)
    assert 'число' in fabrication_problem('Телефон +7 999 111-22-33.', src)


def test_unsupported_technology_claims():
    from ai_assistant import unsupported_claims, analysis_fabrication_problem
    profile = '{"skills": ["Python", "Docker", "Kubernetes", "SIEM"], "about": "AppSec"}'
    assert unsupported_claims('Опыт Docker, K8s и SIEM.', profile) == []
    assert set(unsupported_claims('Prometheus, Grafana и Terraform.', profile)) == {'prometheus', 'grafana', 'terraform'}
    import json
    bad = json.dumps({'about_me_recommendation': 'Настраиваю Prometheus и Ansible.'}, ensure_ascii=False)
    assert 'технологии' in analysis_fabrication_problem(bad, '', None, profile)


def test_claims_problem_blocks_invented_tools_but_allows_honest_denial():
    """30.09 РУСАЛ: «настраивал CI/CD в Azure DevOps» ушло в чат, а Azure DevOps в профиле нет."""
    from ai_assistant import claims_problem
    profile = '{"skills": ["Python", "GitLab CI", "Jenkins", "Docker"]}'
    assert 'azure' in claims_problem('В последних проектах я настраивал CI/CD пайплайны в Azure DevOps.', profile)
    assert claims_problem('С Kafka и OpenSearch напрямую не работал, зато настраивал GitLab CI и Jenkins.', profile) is None
    assert claims_problem('С Ansible опыта нет, но готов освоить.', profile) is None
    assert claims_problem('Работаю с Docker и Jenkins.', profile) is None


def test_answer_claims_and_tenure_checks():
    """30.09: анкеты — kubeadm/kind, network policies, «Kubernetes 6 лет / 2 года / 1+ year»."""
    import json
    from ai_assistant import answer_claims_problem, tenure_claim_problem
    profile = '{"skills": ["Python", "Docker", "Kubernetes", "GitLab CI"], "experience_years": 6.75}'
    assert tenure_claim_problem('В коммерческих проектах я работаю с Kubernetes около шести лет.', profile)
    assert tenure_claim_problem('Опыт работы с Kubernetes – более двух лет.', profile)
    assert tenure_claim_problem('За почти семь лет я занимался безопасностью веб-приложений.', profile) is None
    batch = json.dumps(['Да, использовал kubeadm и Ansible.', 'Нет, с Helm не работал.'], ensure_ascii=False)
    assert answer_claims_problem(batch, profile)
    ok = json.dumps(['Работаю с Docker и GitLab CI.', 'Нет, с Helm не работал.'], ensure_ascii=False)
    assert answer_claims_problem(ok, profile) is None


def test_analysis_headless_choice():
    """Окно браузера при разборе отказов — выбор пользователя: настройка, флаги ей главнее."""
    from rejection_analyzer import analysis_headless_enabled
    assert analysis_headless_enabled({}, []) is False
    assert analysis_headless_enabled({'analysis_headless': True}, []) is True
    assert analysis_headless_enabled({'analysis_headless': True}, ['--show-browser']) is False
    assert analysis_headless_enabled({}, ['--headless']) is True



def test_kill_profile_chrome_ends_real_process(tmp_path):
    """Осиротевший Chrome на профиле бота мешал следующему запуску (05.10)."""
    import subprocess, time, os, pytest
    chrome = r'C:\Program Files\Google\Chrome\Application\chrome.exe'
    if not os.path.exists(chrome):
        pytest.skip('Chrome не установлен')
    from terminal_ui import kill_profile_chrome
    profile = str(tmp_path / 'prof')
    proc = subprocess.Popen([chrome, '--headless=new', f'--user-data-dir={profile}', '--no-first-run', 'about:blank'])
    time.sleep(4)
    assert proc.poll() is None
    assert kill_profile_chrome(profile) >= 1
    assert proc.poll() is not None


def test_template_letter_stack_grounded_deduped_and_vacancy_aware():
    """05.10: «(стек: OWASP Top 10, Bash, OWASP Top 10)», «Ansible, Terraform» не из профиля,
    ИБ-письмо на SRE-вакансию из-за слова «мониторинг»."""
    a = AIAssistant.__new__(AIAssistant)
    a.candidate_profile = {'specialization': 'Application Security Engineer', 'about': 'AppSec-инженер',
                           'skills': ['OWASP Top 10', 'Bash', 'Python', 'Docker', 'SAST'], 'contacts': {}}
    a.config = {}
    a.db = None
    letter = a._heuristic_cover_letter('SRE engineer', 'Сбер', 'мониторинг, инциденты, Terraform, Ansible, Docker', [])
    assert 'Terraform' not in letter and 'Ansible' not in letter
    assert 'SIEM' not in letter and 'информационной безопасности' not in letter
    assert 'Docker' in letter
    appsec = a._heuristic_cover_letter('AppSec инженер', 'Банк', 'DevSecOps, SAST, OWASP, owasp, Terraform', [])
    stack = appsec.split('стек:')[1].split(')')[0] if 'стек:' in appsec else ''
    names = [x.strip().lower() for x in stack.split(',') if x.strip()]
    assert len(names) == len(set(names)) and 'terraform' not in stack.lower()


def test_primary_ai_can_pin_whole_service_by_name():
    """«Antigravity всегда первым»: закреплён сервис целиком, даже при плохой статистике."""
    a = AIAssistant.__new__(AIAssistant)
    a.ai_config = {'primary_ai': 'Antigravity', 'auto_explore': 0, 'cli_providers': ['claude'],
                   'openai_compatible': [
                       {'name': 'Antigravity', 'base_url': 'http://127.0.0.1:1/v1', 'models': ['m-a', 'm-b']},
                       {'name': 'LLM7', 'base_url': 'http://127.0.0.1:2/v1', 'models': ['m-c']}]}
    a._compat_providers = lambda: a.ai_config['openai_compatible']
    a._load_stats = lambda: {'compat:m-a': {'avg': 9, 'success': 0.0002, 'n': 900},
                             'compat:m-b': {'avg': 8, 'success': 0.5, 'n': 10},
                             'claude': {'avg': 3, 'success': 0.99, 'n': 50}, '_down': ['compat:m-a', 'compat:m-b']}
    order = a.ai_order()
    assert order[:2] == ['compat:m-a', 'compat:m-b']   # порядок из настроек, не из статистики
    assert order.index('claude') > 1


def test_rank_service_models_picks_best_and_follows_new_releases():
    from ai_assistant import rank_service_models
    ids = ['gemini-3.8-flash', 'gemini-3.8-flash-high', 'gemini-3.7-flash', 'gemini-3.1-pro', 'gemini-3.1-pro-low',
           'claude-opus-4-6', 'claude-opus-5-5', 'claude-opus-5-5-high', 'claude-sonnet-4-6', 'claude-sonnet-5-5',
           'claude-opus-4-*', 'gpt-oss-120b-medium', 'gemini-3.1-flash-image']
    assert rank_service_models(ids) == ['gemini-3.8-flash', 'gemini-3.7-flash', 'gemini-3.1-pro',
                                        'claude-opus-5-5', 'claude-sonnet-5-5', 'gpt-oss-120b-medium']
    newer = ids + ['gemini-3.9-flash', 'claude-opus-5-6']
    assert rank_service_models(newer)[:2] == ['gemini-3.9-flash', 'gemini-3.8-flash']
    assert 'claude-opus-5-6' in rank_service_models(newer) and 'claude-opus-5-5' not in rank_service_models(newer)


def test_employer_contact_request_gets_answer_with_telegram():
    """Работодатель пишет «хотим связаться» без вопроса — бот соглашается и даёт Telegram из профиля."""
    from chat_workflow import wants_contact, contact_reply
    assert wants_contact('Иван, здравствуйте! Хотим с вами связаться, давайте созвонимся.')
    assert wants_contact('Предлагаем обсудить детали в телеграм')
    assert not wants_contact('Рассмотрим ваше резюме. Если навыки и опыт подойдут, мы свяжемся с вами.')
    assert not wants_contact('К сожалению, мы не готовы пригласить вас на следующий этап.')
    reply = contact_reply({'contacts': {'telegram': 'candidate_example'}})
    assert '@candidate_example' in reply and 'готов обсудить' in reply
    assert 'Telegram' not in contact_reply({'contacts': {}})


def test_probe_does_not_wait_for_a_hung_model():
    """06.10: запуск висел минутами, потому что проверка ждала самую медленную модель."""
    import time as _time
    a = AIAssistant.__new__(AIAssistant)
    a.ai_config = {'cli_providers': [], 'openai_compatible': [
        {'name': 'S', 'base_url': 'http://127.0.0.1:1/v1', 'models': ['fast', 'hung']}]}
    a._compat_providers = lambda: a.ai_config['openai_compatible']
    a._ai_stats = {}
    a._save_stats = lambda: None
    a.PROBE_DEADLINE = 1

    def fake(step):
        if step.endswith('hung'):
            _time.sleep(8)
        return step, True, 0.1
    a._probe_one = fake
    started = _time.time()
    results = {s: ok for s, ok, _ in a.probe_providers(force=True)}
    assert _time.time() - started < 4
    assert results['compat:fast'] is True and results['compat:hung'] is False


def test_chat_templates_salary_only_on_direct_question_and_experience_yes():
    """06.10: «есть ли опыт с X?» — всегда «Да»; сумма — только на прямой вопрос о деньгах."""
    from chat_workflow import split_salary, experience_yes_no, salary_line, EXPERIENCE_YES_REPLY
    rest, asked = split_salary('Подскажите, какие у вас финансовые ожидания?')
    assert asked and rest == ''
    rest, asked = split_salary('Вакансия: зарплата обсуждается по итогам собеседования. Когда вам удобно созвониться?')
    assert not asked and 'созвониться' in rest
    rest, asked = split_salary('Расскажите про опыт с Kubernetes. Какие у вас зарплатные ожидания?')
    assert asked and rest.startswith('Расскажите')
    assert experience_yes_no('Иван, подскажите, пожалуйста, есть ли у вас опыт работы с VMware, Hyper-V или zVirt?')
    assert not experience_yes_no('Расскажите подробнее, есть ли у вас опыт работы с Kubernetes?')
    assert not experience_yes_no('Есть ли опыт с Docker? А с Kubernetes? Работали ли с Helm?')
    from ai_assistant import SALARY_ANSWER
    assert salary_line({}) == SALARY_ANSWER and not any(ch.isdigit() for ch in salary_line({}))
    assert salary_line({'salary_answer': 'От 350 000 руб.'}) == 'От 350 000 руб.'
    assert EXPERIENCE_YES_REPLY.startswith('Да')


def test_followup_question_right_after_our_reply_is_answered():
    """07.10 EKONIKA: бот рекрутера задал следующий вопрос сразу после нашего ответа."""
    from unittest.mock import MagicMock
    from chat_workflow import ChatWorkflowMixin
    bot = ChatWorkflowMixin.__new__(ChatWorkflowMixin)
    bot.FOLLOWUP_WAIT_SECONDS = 4
    bot._read_open_chat = lambda: {'messages': [
        {'text': 'Опишите опыт', 'isOut': False}, {'text': 'Ответ', 'isOut': True},
        {'text': 'Расскажите, с каким вендором NGFW вы внедряли правила?', 'isOut': False}]}
    bot._handle_chat = MagicMock(return_value=None)
    bot._answer_followup({'company_name': 'EKONIKA'}, 'Опишите опыт', 0)
    args = bot._handle_chat.call_args.args
    assert 'NGFW' in args[0]['messages'][-1]['text'] and args[2] == 1


def test_invitation_with_request_is_answered_by_agent_not_contact_template():
    """07.10 СЕЙВИНФОРМ: «Сориентируйте, где вы находитесь» — это вопрос, ответ пишет агент (город), а не шаблон."""
    from rejection_analyzer import RejectionAnalyzer
    from chat_workflow import OPEN_QUESTION
    msg = ('Иван, здравствуйте. Готовы пригласить Вас на собеседование. Сориентируйте, пожалуйста, '
           'где территориально Вы находитесь и определим формат, дату и время встречи.')
    assert RejectionAnalyzer.looks_like_question_card(msg)
    assert OPEN_QUESTION.search('С какими технологиями вам интереснее всего работать и почему?')
    assert not OPEN_QUESTION.search('Готовы ли вы работать из офиса?')


def test_tenure_first_time_general_second_time_concrete():
    """07.10: на первый вопрос о стаже — обобщённо, на повторный — «6 лет»."""
    from chat_workflow import tenure_asked_again, tenure_line, allowed_tail_lines
    first = {'messages': [{'text': 'Здравствуйте', 'isOut': True},
                          {'text': 'Какой у вас общий стаж работы в сфере управления рисками?', 'isOut': False}]}
    assert not tenure_asked_again(first, first['messages'][-1]['text'])
    again = {'messages': first['messages'] + [{'text': 'Да, такой опыт есть…', 'isOut': True},
                                              {'text': 'А сколько лет именно?', 'isOut': False}]}
    assert tenure_asked_again(again, 'А сколько лет именно?')
    assert tenure_line({'tenure_years': 6}) == 'Общий опыт работы — 6 лет.'
    assert tenure_line({'tenure_years': 2}) == 'Общий опыт работы — 2 года.'
    assert tenure_line({}) is None
    assert 'Общий опыт работы — 6 лет.' in allowed_tail_lines({'tenure_years': 6})


def test_recently_answered_chats_are_revisited_when_employer_wrote_again():
    """07.10 EKONIKA: новый вопрос пришёл, пока бот был в чате; чат стал прочитанным и выпадал из обхода."""
    from datetime import datetime, timedelta
    from unittest.mock import MagicMock
    from chat_workflow import ChatWorkflowMixin
    bot = ChatWorkflowMixin.__new__(ChatWorkflowMixin)
    now = datetime.now()
    bot._load_chat_actions = lambda: {
        'a': {'kind': 'answer', 'status': 'sent', 'chat_url': 'https://hh.ru/chat/1', 'identity': 'id1',
              'updated_at': now.isoformat()},
        'b': {'kind': 'answer', 'status': 'sent', 'chat_url': 'https://hh.ru/chat/2', 'identity': 'id2',
              'updated_at': (now - timedelta(days=10)).isoformat()},
        'c': {'kind': 'answer', 'status': 'composer_disabled', 'chat_url': 'https://hh.ru/chat/3', 'identity': 'id3',
              'updated_at': now.isoformat()}}
    assert bot._recently_answered_chats() == [('id1', 'https://hh.ru/chat/1')]
    assert bot._recently_answered_chats(seen={'id1'}) == []
    bot.driver = MagicMock()
    bot.goto = MagicMock()
    bot._read_open_chat = lambda: {'messages_loaded': True, 'messages': [
        {'text': 'Наш ответ', 'isOut': True}, {'text': 'С каким вендором NGFW?', 'isOut': False}]}
    bot._handle_chat = MagicMock(return_value=None)
    summary = {'viewed': 0}
    bot._revisit_answered_chats(set(), summary)
    bot._handle_chat.assert_called_once()
    assert summary['viewed'] == 1


def test_default_candidate_profile_has_no_personal_data_or_assumed_skills():
    assert not DEFAULT_CANDIDATE_PROFILE["name"]
    assert not DEFAULT_CANDIDATE_PROFILE["specialization"]
    assert DEFAULT_CANDIDATE_PROFILE["experience_years"] == 0
    assert not DEFAULT_CANDIDATE_PROFILE["skills"]
    assert not DEFAULT_CANDIDATE_PROFILE["about"]
    assert not any(DEFAULT_CANDIDATE_PROFILE["contacts"].values())

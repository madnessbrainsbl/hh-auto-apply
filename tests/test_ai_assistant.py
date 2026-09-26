import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import copy

import pytest
from ai_assistant import AIAssistant, DEFAULT_CANDIDATE_PROFILE


TEST_PROFILE = {
    **copy.deepcopy(DEFAULT_CANDIDATE_PROFILE),
    "specialization": "Python Developer",
    "experience_years": 3,
    "skills": ["Python", "Docker", "PostgreSQL", "Linux"],
    "expected_salary": "от 180 000 руб.",
}


@pytest.fixture
def assistant():
    config = {
        "ai_config": {"enabled": False},  # Тестируем надежный эвристический движок без внешних вызовов
        "candidate_profile": TEST_PROFILE
    }
    return AIAssistant(config)


def test_generate_cover_letter_uses_profile_skills(assistant):
    letter = assistant.generate_cover_letter(
        vacancy_title="Backend-разработчик",
        company_name="Tech Lab",
        vacancy_description="Python, PostgreSQL, высоконагруженные сервисы."
    )
    assert "Backend-разработчик" in letter
    assert "Tech Lab" in letter
    assert "PostgreSQL" in letter


def test_generate_cover_letter_does_not_claim_foreign_stack(assistant):
    letter = assistant.generate_cover_letter(
        vacancy_title="Java Developer",
        company_name="Fintech Corp",
        vacancy_description="Java, Spring, Kafka."
    )
    assert "Fintech Corp" in letter
    assert "Kafka" not in letter and "Spring" not in letter


def test_generate_cover_letter_general(assistant):
    letter = assistant.generate_cover_letter(
        vacancy_title="Системный аналитик",
        company_name="Банк РФ"
    )
    assert "Системный аналитик" in letter
    assert "Банк РФ" in letter
    assert len(letter) > 50


def test_answer_question_github(assistant):
    ans = assistant.answer_question("Укажите ссылку на ваш GitHub или портфолио")
    assert "github.com" in str(ans)


def test_answer_question_telegram():
    """Ник берётся из профиля и НЕ выдумывается, когда профиль пуст.

    В профиле по умолчанию телеграм пустой. Выдуманный ник — это заведомо
    нерабочий контакт в анкете работодателя, поэтому правильный ответ здесь —
    отсылка к резюме, а не правдоподобная строка с «@».
    """
    filled = copy.deepcopy(DEFAULT_CANDIDATE_PROFILE)
    filled["contacts"]["telegram"] = "@backend_hunter"
    with_tg = AIAssistant({"ai_config": {"enabled": False}, "candidate_profile": filled})
    assert with_tg.answer_question("Напишите ваш Telegram для связи") == "@backend_hunter"

    empty = copy.deepcopy(DEFAULT_CANDIDATE_PROFILE)
    empty["contacts"]["telegram"] = ""
    without_tg = AIAssistant({"ai_config": {"enabled": False}, "candidate_profile": empty})
    ans = str(without_tg.answer_question("Напишите ваш Telegram для связи"))
    assert "@" not in ans
    assert "резюме" in ans.lower()


def test_answer_question_salary(assistant):
    ans = assistant.answer_question("Ваши зарплатные ожидания")
    assert "180" in str(ans)


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
    idx = assistant.answer_question("Какой у вас опыт в разработке?", question_type="radio", options=options)
    assert idx in (1, 2)  # 1-3 года или 3-6 лет для кандидата с 3 годами опыта


def test_analyze_rejection_ats(assistant):
    desc = """
    Требования:
    - Опыт работы от 5 лет в backend-разработке;
    - Глубокие знания Kubernetes, Docker, Go, Python;
    - Практический опыт с Kafka и Terraform;
    - Только очный формат работы в офисе (без удаленки).
    """
    analysis = assistant.analyze_rejection_ats(
        vacancy_title="Senior Backend Engineer",
        company_name="Tech Inc",
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
    """Письмо-шаблон — по профессии и навыкам из профиля, одна профессия на всех не зашита."""
    def make(spec, about, skills):
        a = AIAssistant.__new__(AIAssistant)
        a.candidate_profile = {'specialization': spec, 'about': about, 'skills': skills, 'contacts': {}}
        a.config = {}
        a.db = None
        return a

    designer = make('Графический дизайнер', 'Делаю айдентику', ['Figma', 'Photoshop'])
    letter = designer._heuristic_cover_letter('Дизайнер', 'Студия', 'Figma, брендбуки', [])
    assert 'Графический дизайнер' in letter
    assert 'Figma' in letter and 'Python' not in letter

    dev = make('Python Developer', 'Пишу бэкенд', ['Python', 'Django'])
    assert 'Django' in dev._heuristic_cover_letter('Backend', 'Банк', 'Django, REST', [])


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
    good = ('Здравствуйте! Откликаюсь на позицию аналитика данных в Ромашка Технологии. '
            'Работал с ClickHouse, строил витрины и дашборды для продуктовых команд, '
            'разбирал метрики. Буду рад обсудить, чем могу быть полезен вашей команде.')
    assert q(good, 'АО Ромашка Технологии', 'Аналитик данных L2') is None
    assert q('Коротко.', 'Ромашка', 'Аналитик') == 'слишком короткое'
    assert 'разметка' in q('**Здравствуйте!**\n' + good, 'Ромашка', 'Аналитик данных')
    assert 'по-русски' in q('Hello! ' * 60, 'Ромашка', 'Аналитик')
    assert 'компанию' in q(good.replace('Ромашка Технологии', 'вашей компании')
                           .replace('аналитика данных', 'эту позицию'), 'Ромашка', 'Аналитик')
    assert 'проценты' in q(good + ' Сократил расходы на 45%.', 'Ромашка', 'Аналитик данных')


SERVICE_TEXT = ('Gemini 3.5 Flash is no longer available. Please switch to Gemini 3.7 Flash '
                'in the latest version of Antigravity.')


def test_service_message_is_never_an_answer():
    """24.09: «Gemini 3.5 Flash is no longer available» ушло в чат и в анкету работодателя.

    Проверка — для любого ответа (чат, анкета), не только для писем. Честный
    английский ответ на английский вопрос работодателя проходит.
    """
    from ai_assistant import service_message_problem
    assert service_message_problem(SERVICE_TEXT)
    assert service_message_problem("As an AI, I don't have personal experience with Kubernetes.")
    assert service_message_problem('Как языковая модель, я не могу иметь опыт работы.')
    for fine in ('150000', 'Django, Flask, FastAPI', 'Готов обсудить на собеседовании',
                 'I have 3 years of experience in backend development and I am available next week.'):
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

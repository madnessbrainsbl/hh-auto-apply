# -*- coding: utf-8 -*-
"""Проверки правок разбора отказов. Запуск: python test_rejection_fixes.py"""
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rejection_analyzer import (RejectionAnalyzer, is_ui_noise,
                                dedupe_texts, clean_chat_history, dialog_key,
                                classify_rejection_reason, is_evidence_based_reason,
                                short_error, is_network_error, explain_network_error)


def test_ui_noise():
    # подсказки быстрых ответов hh — не реплики кандидата
    assert is_ui_noise('Можно без опыта?')
    assert is_ui_noise('Где находится место работы?')
    assert is_ui_noise('Был онлайн вчера')
    assert is_ui_noise('20:09')
    assert is_ui_noise('Отказ')
    assert is_ui_noise('   ')
    # настоящие сообщения проходят
    assert not is_ui_noise('К сожалению, мы остановили выбор на другом кандидате.')
    assert not is_ui_noise('Здравствуйте! Спасибо за отклик, но нам нужен опыт с Kubernetes.')


def test_vacancy_id_is_stable():
    # md5 от «вакансия+компания» одинаков между запусками, в отличие от hash()
    key = 'инженер иб_альфа'
    a = 'chat_' + hashlib.md5(key.encode('utf-8')).hexdigest()[:12]
    b = 'chat_' + hashlib.md5(key.encode('utf-8')).hexdigest()[:12]
    assert a == b and a.startswith('chat_') and len(a) == 17


def test_cache_loader():
    an = RejectionAnalyzer.__new__(RejectionAnalyzer)  # без браузера и БД
    chats = RejectionAnalyzer._load_cached_chats(an, limit=0)
    assert isinstance(chats, list)
    if chats:
        assert all(c.get('employer_messages') for c in chats), 'пустые диалоги не должны проходить'
        assert len(RejectionAnalyzer._load_cached_chats(an, limit=2)) == 2


def test_archive_scan_not_gated():
    # регресс: ранний return отключал сканирование /negotiations?filter=discard
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'rejection_analyzer.py'), encoding='utf-8').read()
    assert 'Сканирование старого архива отключено' not in src




def test_daily_quota_stops_retrying():
    """Суточный лимит не должен уходить в 60 с пауз на каждый отказ."""
    import time as _t
    from ai_assistant import AIAssistant
    from unittest.mock import patch

    ai = AIAssistant.__new__(AIAssistant)
    ai.enabled = True
    ai._openai_client = None
    ai._quota_exhausted = False
    ai.model_name = 'gemini-flash-latest'
    ai.temperature = 0.3
    ai.ai_config = {'cli_providers': [], 'openai_compatible': []}

    class DailyQuota:
        # **kwargs: в вызов добавлен request_options с таймаутом — без него
        # запрос к Gemini висел бесконечно и подвешивал весь прогон.
        def generate_content(self, _, **kwargs):
            raise RuntimeError(
                '429 You exceeded your current quota. quota_id: '
                '"GenerateRequestsPerDayPerProjectPerModel-FreeTier"'
            )

    ai._gemini_client = DailyQuota()
    # соседние модели тоже пусты — проверяем именно отказ от 60-секундных пауз
    ai._retry_on_other_gemini_model = lambda _p: None
    with patch('terminal_ui.network_is_up', return_value=True):
        start = _t.time()
        assert ai._call_llm('тест') is None
        assert _t.time() - start < 5, 'суточный лимит уходил в backoff'
        assert ai._quota_exhausted is True
        # следующий вызов вообще не идет в сеть
        assert ai._call_llm('тест') is None


def test_cache_is_cleaned_on_load():
    """Кеш собран до фильтрации — мусор и повторы должны отсекаться при чтении."""
    raw = ['Можно без опыта?', '20:09', 'Нам нужен опыт с Kubernetes.',
           'Нам нужен опыт с   Kubernetes.', 'Был онлайн вчера']
    assert dedupe_texts(raw) == ['Нам нужен опыт с Kubernetes.']

    hist = [{'sender': 'Соискатель', 'text': 'Здравствуйте!  Готов обсудить.'},
            {'sender': 'Работодатель', 'text': 'здравствуйте! готов обсудить.'},
            {'sender': 'Работодатель', 'text': 'Отказ'}]
    cleaned = clean_chat_history(hist)
    assert len(cleaned) == 1 and cleaned[0]['sender'] == 'Соискатель'


def test_sender_attribution_not_inverted():
    """Отказ работодателя не должен уезжать в реплики кандидата (и наоборот)."""
    an = RejectionAnalyzer.__new__(RejectionAnalyzer)
    an.config = {'candidate_profile': {'name': 'Иван',
                                       'contacts': {'telegram': '@candidate_example'}}}
    hist = [
        {'sender': 'Работодатель', 'text': 'Имею опыт в AppSec и пентесте.'},
        {'sender': 'Соискатель', 'text': 'Для оперативной связи: Telegram @candidate_example'},
        {'sender': 'Соискатель', 'text': 'Иван, здравствуйте!'},
        {'sender': 'Соискатель', 'text': 'К сожалению, мы не готовы пригласить вас на этап.'},
    ]
    by_text = {m['text']: m['sender'] for m in an._attribute_senders(hist)}
    assert by_text['Имею опыт в AppSec и пентесте.'] == 'Соискатель'
    assert by_text['Для оперативной связи: Telegram @candidate_example'] == 'Соискатель'
    assert by_text['Иван, здравствуйте!'] == 'Работодатель'
    assert by_text['К сожалению, мы не готовы пригласить вас на этап.'] == 'Работодатель'


def test_cache_merge_does_not_wipe():
    """Прогон на 5 диалогов не должен затирать накопленные 45."""
    import json, tempfile, shutil
    import rejection_analyzer as RA

    an = RejectionAnalyzer.__new__(RejectionAnalyzer)
    tmp = tempfile.mkdtemp()
    real_dir = RA.SCRIPT_DIR
    RA.SCRIPT_DIR = tmp
    try:
        path = os.path.join(tmp, 'rejected_chats_cache.json')
        old = [{'vacancy_title': f'Вакансия {i}', 'company_name': f'Компания {i}'} for i in range(45)]
        json.dump(old, open(path, 'w', encoding='utf-8'), ensure_ascii=False)

        fresh = [{'vacancy_title': 'Вакансия 0', 'company_name': 'Компания 0', 'new': True},
                 {'vacancy_title': 'Свежая', 'company_name': 'НоваяКо'}]
        an._merge_into_cache(fresh)

        got = json.load(open(path, encoding='utf-8'))
        assert len(got) == 46, f'ожидалось 46, получено {len(got)}'
        by_key = {(c['vacancy_title'], c['company_name']): c for c in got}
        assert by_key[('Вакансия 0', 'Компания 0')].get('new') is True, 'свежая версия не вытеснила старую'
        assert ('Свежая', 'НоваяКо') in by_key
    finally:
        RA.SCRIPT_DIR = real_dir
        shutil.rmtree(tmp, ignore_errors=True)


def test_dialog_key_survives_read():
    """Карточка после прочтения теряет бейдж и время — ключ меняться не должен."""
    unread = 'ONESEC\nСпециалист по ИБ\nОтказ\n2\n12:43'
    read = 'ONESEC\nСпециалист по ИБ\nОтказ\n14:07'
    assert dialog_key(unread) == dialog_key(read)
    assert dialog_key('Сбер\nData Analyst\nОтказ') != dialog_key(unread)


def test_rejection_reasons_are_grouped():
    """Причина отказа — свободный текст ИИ. Он должен схлопываться в типы, иначе
    30 отказов дают 30 уникальных строк и агрегировать нечего."""
    # формулировки взяты из реальных строк таблицы rejections
    cases = [
        ('Автоматический или быстрый отказ на этапе скрининга резюме/отклика на HeadHunter. '
         'Возможные причины: несоответствие ожиданиям по зарплате (кандидат запрашивает '
         'от 180 000 руб. для Junior-позиции)', 'salary'),
        ('Полное отсутствие сопроводительного письма (пустой отклик) при разнице фокусов',
         'application_quality'),
        ('Несоответствие позиционирования кандидата требованиям вакансии: профиль смещен '
         'в сторону Web/AppSec', 'stack_mismatch'),
        ('Недостаточный стаж или несоответствие требуемому грейду', 'experience_grade'),
        ('Высокая конкуренция среди откликов или ручной отсев рекрутером', 'auto_screening'),
        ('Несоответствие формату работы (требуется присутствие в офисе/регионе)', 'location'),
        ('', 'other'),
    ]
    for text, expected in cases:
        key, label = classify_rejection_reason(text)
        assert key == expected, f'{text[:40]!r} -> {key}, ожидалось {expected}'
        assert label

    # шаблонные причины бот пишет сам — за ними нет ответа работодателя
    assert not is_evidence_based_reason('Автоматический скрининг или отбор более опытного кандидата')
    assert not is_evidence_based_reason('Отказ работодателя.')
    assert is_evidence_based_reason('Работодатель ищет специалиста по КИИ и 187-ФЗ, а не пентестера')


def test_fix_plan_uses_real_rejections_only():
    """План правок строится по накопленным отказам, но выдуманные образцы в него
    попадать не должны, а experience_advice больше не теряется."""
    class StubDB:
        def get_recent_rejections(self, limit=10):
            return [
                {'title': 'AppSec', 'company': 'Альфа',
                 'rejection_reason': 'Кандидат не прикрепил сопроводительное письмо, отправил только имя',
                 'missing_keywords': ['AppSec', 'appsec', 'Kubernetes'],
                 'remediation_advice': ['В письме не были явно подсвечены ключевые требования вакансии.',
                                        'В «О себе» указать опыт код-ревью',
                                        'В опыте описать внедрение SAST в GitLab CI']},
                {'title': 'SOC', 'company': 'Бета',
                 'rejection_reason': 'Автоматический скрининг или отбор более опытного кандидата',
                 'missing_keywords': ['kubernetes'],
                 'remediation_advice': []},
            ]

    an = RejectionAnalyzer.__new__(RejectionAnalyzer)
    an.db = StubDB()

    fake = {'vacancy_title': 'Образец', 'company_name': 'Выдумка', 'is_sample': True,
            'rejection_root_cause': 'Выдуманная причина про зарплату',
            'missing_skills': ['НесуществующийНавык'],
            'about_me_recommendation': 'Выдуманное О себе',
            'experience_advice': 'Выдуманный опыт'}
    real = {'vacancy_title': 'SOC-аналитик', 'company_name': 'Гамма',
            'rejection_root_cause': 'Ожидания по зарплате выше вилки позиции',
            'missing_skills': ['EDR'],
            'about_me_recommendation': 'В «О себе» добавить триаж алертов SOC',
            'experience_advice': 'Описать дежурства в SOC и SLA реагирования'}

    plan = an.build_resume_fix_plan([fake, real])

    dump = json.dumps(plan, ensure_ascii=False)
    assert 'Выдуман' not in dump and 'НесуществующийНавык' not in dump, 'образец протёк в план правок'

    assert plan['total_rejections'] == 3, plan['total_rejections']
    by_key = {c['key']: c for c in plan['categories']}
    assert set(by_key) == {'application_quality', 'experience_grade', 'salary'}, sorted(by_key)
    assert by_key['application_quality']['count'] == 1
    # шаблонная причина считается, но подтверждённой не признаётся
    assert by_key['experience_grade']['evidenced'] == 0
    assert by_key['salary']['evidenced'] == 1
    assert plan['evidence_based'] == 2
    assert abs(sum(c['share'] for c in plan['categories']) - 100.0) < 0.5

    # разные написания одного навыка складываются, а не дробятся
    skills = dict(plan['skills_to_add'])
    assert skills.get('AppSec') == 2, plan['skills_to_add']
    assert skills.get('Kubernetes') == 2, plan['skills_to_add']

    about = [t for t, _ in plan['about_me']]
    exp = [t for t, _ in plan['experience']]
    assert any('код-ревью' in t for t in about)
    assert any('SAST' in t for t in exp), exp
    assert any('дежурства в SOC' in t for t in exp), 'experience_advice свежего разбора потерян'
    # шаблонная критика письма в план правок резюме не идёт
    assert not any('подсвечены ключевые требования' in t for t in about + exp)
    assert plan['systemic']


def test_skill_limit_adapts_to_existing_count():
    """У разных пользователей разное число навыков. Бот обязан считать свободные
    места от ФАКТИЧЕСКОГО содержимого резюме, а не добавлять фиксированный список.

    Регресс: превышение лимита hh (30) не предупреждение, а отказ сохранить —
    форма откатывается, и теряется всё добавленное за проход.
    """
    from resume_updater import HHResumeUpdater

    class FakeEl:
        def __init__(self, box): self.box = box
        def click(self): pass
        def send_keys(self, *a): self.box['typed'] = a[0] if a else ''

    class FakeDriver:
        """Имитирует форму навыков: всё, что «кликнули», попадает в selected."""
        def __init__(self, existing):
            self.selected = list(existing)
            self.current_url = 'https://hh.ru/resume/edit/x/keySkills'
            self._box = {}
        def get(self, url): pass
        def find_element(self, by, sel): return FakeEl(self._box)
        def find_elements(self, by, sel):
            # один вариант в выпадающем списке на введённый навык
            return [FakeEl(self._box)] if 'option' in sel else []
        def execute_script(self, script, *args):
            if 'chips-trigger-chip-' in script:
                return list(self.selected)
            if 'resume-editor-skills-recommended-' in script and 'map' in script:
                return []                       # hh ничего не рекомендует
            if 'Количество навыков превышено' in script:
                # hh отверг бы сохранение при перелимите
                return ('Количество навыков превышено'
                        if len(self.selected) > 30 else None)
            return None

    import resume_updater as RU
    orig_ac = None
    orig_sleep = RU.time.sleep
    RU.time.sleep = lambda *_a, **_k: None   # в методе реальные паузы, тест бы шёл 7 минут
    try:
        from selenium.webdriver.common import action_chains as _ac
        orig_ac = _ac.ActionChains

        class FakeChain:
            def __init__(self, driver): self.d = driver
            def move_to_element(self, el): self._el = el; return self
            def pause(self, _): return self
            def click(self): return self
            def perform(self):
                # клик по варианту = навык добавлен в форму
                t = self.d._box.get('typed')
                if t and t.lower() not in [s.lower() for s in self.d.selected]:
                    self.d.selected.append(t)
                    self.d._box['typed'] = None
        _ac.ActionChains = FakeChain

        wanted = [f'Навык {i}' for i in range(1, 41)]     # просим 40 штук

        for existing_count in (0, 6, 25, 30):
            existing = [f'Старый {i}' for i in range(existing_count)]
            u = HHResumeUpdater.__new__(HHResumeUpdater)
            u.resume_id = 'x'; u._user_closed = False
            u.driver = FakeDriver(existing)
            u.is_driver_alive = lambda: True

            ok, msg, added = u.add_skills_to_resume(wanted)
            total = len(u.driver.selected)
            assert total <= 30, (
                f'при {existing_count} имеющихся стало {total} — превышен лимит hh')
            if existing_count < 30:
                assert len(added) == 30 - existing_count, (
                    f'при {existing_count} имеющихся добавлено {len(added)}, '
                    f'ожидалось {30 - existing_count}')
            else:
                assert not added, 'при полном резюме не должно добавляться ничего'
    finally:
        RU.time.sleep = orig_sleep
        if orig_ac is not None:
            from selenium.webdriver.common import action_chains as _ac2
            _ac2.ActionChains = orig_ac


def test_network_error_retried_not_fatal():
    """ERR_NAME_NOT_RESOLVED на первой навигации после старта Chrome — не повод
    ронять разбор и вываливать пользователю стек chromedriver."""
    selenium_err = (
        "Message: unknown error: net::ERR_NAME_NOT_RESOLVED\n"
        "  (Session info: chrome=153.0.8010.50)\n"
        "Stacktrace:\n\tchromedriver!GetHandleVerifier [0x7ff658eb1035+5a95]"
    )
    # стек не должен попадать пользователю
    assert 'Stacktrace' not in short_error(selenium_err)
    assert 'chromedriver' not in short_error(selenium_err)
    assert 'ERR_NAME_NOT_RESOLVED' in short_error(selenium_err)

    # Пользователю нужна причина, а не код ошибки: «ERR_NAME_NOT_RESOLVED» он
    # прочитает как поломку бота, хотя это отвалился DNS (обычно из-за VPN).
    explained = explain_network_error(selenium_err)
    assert 'DNS' in explained and 'VPN' in explained
    assert 'ERR_NAME_NOT_RESOLVED' not in explained
    assert explain_network_error('net::ERR_INTERNET_DISCONNECTED') == 'нет подключения к интернету'
    assert 'прокси' in explain_network_error('net::ERR_PROXY_CONNECTION_FAILED')

    assert is_network_error(selenium_err)
    assert is_network_error('Message: timeout: Timed out receiving message')
    # закрытый пользователем браузер — НЕ сетевая ошибка, повторять бессмысленно
    assert not is_network_error('invalid session id')
    assert not is_network_error('no such window')

    # goto повторяет попытку и добивается успеха
    an = RejectionAnalyzer.__new__(RejectionAnalyzer)
    an._user_closed = False

    class FlakyDriver:
        def __init__(self): self.calls = 0
        def get(self, url):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError(selenium_err)

    an.driver = FlakyDriver()
    import rejection_analyzer as RA
    orig = RA.time.sleep
    RA.time.sleep = lambda *_a, **_k: None
    try:
        assert an.goto('https://hh.ru/chat') is True
        assert an.driver.calls == 2, f'ожидался повтор, вызовов: {an.driver.calls}'
    finally:
        RA.time.sleep = orig


def test_remind_button_needs_real_click_and_proof():
    """«Напомнить об отклике» кликается настоящей мышью, а успех засчитывается,
    только если кнопка после клика пропала.

    Раньше клик шёл через execute_script — React на вёрстке hh его не принимает,
    кнопка оставалась на месте, а в лог писалось «Нажата кнопка»."""
    import rejection_analyzer as RA
    from selenium.webdriver.common import action_chains as _ac

    class FakeEl:
        def is_displayed(self): return True

    class FakeDriver:
        def __init__(self, disappears):
            self.disappears = disappears
            self.clicked_with_js = False
            self.gone = False
        def find_elements(self, by, sel):
            if 'iframe' in str(sel).lower():
                return []
            return [] if self.gone else [FakeEl()]
        def execute_script(self, script, *a):
            if 'click' in script:
                self.clicked_with_js = True
            return None

    class FakeChain:
        def __init__(self, driver): self.d = driver
        def move_to_element(self, el): return self
        def pause(self, _): return self
        def click(self): return self
        def perform(self):
            if self.d.disappears:
                self.d.gone = True

    orig_ac = _ac.ActionChains
    orig_sleep = RA.time.sleep
    _ac.ActionChains = FakeChain
    RA.time.sleep = lambda *_a, **_k: None
    try:
        for disappears, expected in ((True, True), (False, False)):
            an = RejectionAnalyzer.__new__(RejectionAnalyzer)
            an._user_closed = False
            an.driver = FakeDriver(disappears)
            an.is_driver_alive = lambda: True

            got = an.check_and_click_remind_button('тест', stay_in_frame=True)
            assert got is expected, (
                f'кнопка {"пропала" if disappears else "осталась"} — '
                f'ожидалось {expected}, получено {got}')
            assert not an.driver.clicked_with_js, (
                'клик ушёл через execute_script — React его не примет')
    finally:
        _ac.ActionChains = orig_ac
        RA.time.sleep = orig_sleep

def test_cover_letter_never_borrowed_from_employer():
    """«Письмо кандидата» берётся только из его собственных реплик.

    Если в диалоге нет исходящих сообщений,
    сборщик подставил в cover_letter весь текст страницы — вместе с подписью
    рекрутера и его телеграмом. ИИ выдал вердикт «кандидат оставил чужое имя Алексей
    при имени кандидата Иван». Разбор отказа обязан молчать там, где данных нет.
    """
    an = RejectionAnalyzer.__new__(RejectionAnalyzer)

    # 1. Исходящих нет — письма нет, чужой текст не подставляется
    borrowed = ('Здравствуйте! Рассмотрели ваш отклик, к сожалению отказ. '
                'Алексей @hr_example')
    c = an.normalize_chat({
        'vacancy_title': 'Специалист по ИБ',
        'company_name': 'Компания-пример',
        'cover_letter': borrowed,
        'chat_history': [
            {'sender': 'Работодатель', 'text': 'Здравствуйте! К сожалению, отказ.'},
            {'sender': 'Работодатель', 'text': 'Алексей'},
        ],
    })
    assert c is not None, 'диалог с ответом работодателя не должен отбрасываться'
    assert c['cover_letter'] == '', (
        f"подставлен чужой текст вместо письма: {c['cover_letter']!r}")

    # 2. Своё письмо есть — оно и попадает в разбор
    mine = 'Добрый день! Меня заинтересовала вакансия, опыт в AppSec 6 лет.'
    c2 = an.normalize_chat({
        'vacancy_title': 'Специалист по ИБ',
        'company_name': 'Компания-пример',
        'cover_letter': 'мусор со страницы',
        'chat_history': [
            {'sender': 'Соискатель', 'text': mine},
            {'sender': 'Работодатель',
             'text': 'К сожалению, мы остановили выбор на другом кандидате.'},
        ],
    })
    assert c2 is not None
    assert c2['cover_letter'] == mine, (
        f"собственное письмо потеряно: {c2['cover_letter']!r}")

    # 3. В письмо затесался ответ компании — значит это не письмо
    c3 = an.normalize_chat({
        'vacancy_title': 'Специалист по ИБ',
        'company_name': 'Компания-пример',
        'cover_letter': 'Вакансия Специалист по ИБ К сожалению, отказ. Алексей @hr_example',
        'chat_history': [
            {'sender': 'Работодатель', 'text': 'К сожалению, мы вам отказываем.'},
        ],
    })
    assert c3 is not None
    assert c3['cover_letter'] == '', (
        f"текст страницы принят за письмо: {c3['cover_letter']!r}")

    # 4. Обрывки, которые письмом не являются
    for junk in ('HR Сбер', 'Вера', 'Отклик на вакансию',
                 'Для оперативной связи: Telegram @candidate_example'):
        c4 = an.normalize_chat({
            'vacancy_title': 'Специалист по ИБ',
            'company_name': 'Тест',
            'cover_letter': junk,
            'chat_history': [
                {'sender': 'Работодатель', 'text': 'К сожалению, отказ.'},
            ],
        })
        assert c4 is not None
        assert c4['cover_letter'] == '', f'обрывок принят за письмо: {junk!r}'




if __name__ == '__main__':
    for name, fn in sorted(globals().items()):
        if name.startswith('test_'):
            fn()
            print('[OK]', name)
    print('Все проверки пройдены.')

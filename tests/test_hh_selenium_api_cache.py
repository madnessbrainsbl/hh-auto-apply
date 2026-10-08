import importlib.util
import json
import requests
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "hh_selenium.py"
AUTO_MODULE_PATH = Path(__file__).resolve().parents[1] / "test.py"


def load_selenium_module():
    spec = importlib.util.spec_from_file_location("hh_selenium_module", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def load_auto_module():
    spec = importlib.util.spec_from_file_location("hh_auto_module", AUTO_MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_get_api_vacancy_url_prefers_regular_vacancy_page():
    module = load_selenium_module()

    url = module.get_api_vacancy_url({
        "alternate_url": "https://hh.ru/vacancy/123",
        "apply_alternate_url": "https://hh.ru/applicant/vacancy_response?vacancyId=123",
    })

    assert url == "https://hh.ru/vacancy/123"


def test_get_api_vacancy_url_rejects_external_urls():
    module = load_selenium_module()

    url = module.get_api_vacancy_url({
        "alternate_url": "https://example.com/vacancy/123",
        "apply_alternate_url": "http://hh.ru/applicant/vacancy_response?vacancyId=123",
    })

    assert url is None


def test_detect_response_state_treats_denial_as_denied():
    module = load_selenium_module()

    class FakeDriver:
        page_source = ""

        def execute_script(self, *_args):
            return {"hasModal": False, "controls": [{"text": "Вам отказали"}]}

        def find_elements(self, *_args):
            return []

    bot = module.HHSeleniumBot.__new__(module.HHSeleniumBot)
    bot.driver = FakeDriver()

    assert bot.detect_response_state() == "denied"


def test_detect_response_state_treats_sent_response_as_success():
    module = load_selenium_module()

    class FakeDriver:
        page_source = ""

        def execute_script(self, *_args):
            return {
                "hasModal": False,
                "controls": [
                    {"text": "Отклик другим резюме"},
                    {"text": "Чат"},
                ],
            }

        def find_elements(self, *_args):
            return []

    bot = module.HHSeleniumBot.__new__(module.HHSeleniumBot)
    bot.driver = FakeDriver()

    assert bot.detect_response_state() == "success"


def test_detect_response_state_treats_english_sent_response_as_success():
    module = load_selenium_module()

    class FakeDriver:
        page_source = ""

        def execute_script(self, *_args):
            return {
                "hasModal": False,
                "controls": [
                    {"text": "Apply with another resume"},
                    {"text": "Chat"},
                ],
            }

        def find_elements(self, *_args):
            return []

    bot = module.HHSeleniumBot.__new__(module.HHSeleniumBot)
    bot.driver = FakeDriver()

    assert bot.detect_response_state() == "success"


def test_detect_response_state_prefers_english_success_over_related_respond_buttons():
    module = load_selenium_module()

    class ButtonElement:
        def __init__(self, text):
            self.text = text

        def is_displayed(self):
            return True

    class FakeDriver:
        page_source = ""

        def execute_script(self, *_args):
            return {"hasModal": False, "controls": []}

        def find_elements(self, *_args):
            return [
                ButtonElement("Apply with another resume"),
                ButtonElement("Chat"),
                ButtonElement("Respond"),
            ]

    bot = module.HHSeleniumBot.__new__(module.HHSeleniumBot)
    bot.driver = FakeDriver()

    assert bot.detect_response_state() == "success"


def test_detect_response_state_prefers_active_apply_button_over_stale_denial_text():
    module = load_selenium_module()

    class BodyElement:
        text = "Вам отказали"

    class ButtonElement:
        text = "Откликнуться"

        def is_displayed(self):
            return True

    class FakeDriver:
        page_source = ""

        def execute_script(self, *_args):
            return {"hasModal": False, "controls": [{"text": "Откликнуться"}]}

        def find_element(self, *_args):
            return BodyElement()

        def find_elements(self, *_args):
            return [ButtonElement()]

    bot = module.HHSeleniumBot.__new__(module.HHSeleniumBot)
    bot.driver = FakeDriver()

    assert bot.detect_response_state() == "ready"


def test_detect_response_state_stops_on_hh_response_limit():
    module = load_selenium_module()

    class BodyElement:
        text = (
            "Отклик на вакансию\n"
            "В течение 24 часов можно совершить не более 200 откликов. "
            "Вы исчерпали лимит откликов, попробуйте отправить отклик позднее."
        )

    class FakeDriver:
        page_source = BodyElement.text

        def execute_script(self, *_args):
            return {"hasModal": True, "controls": [{"text": "Откликнуться"}]}

        def find_element(self, *_args):
            return BodyElement()

        def find_elements(self, *_args):
            return []

    bot = module.HHSeleniumBot.__new__(module.HHSeleniumBot)
    bot.driver = FakeDriver()

    assert bot.detect_response_state() == "limit"


def test_detect_response_state_ignores_stale_success_text_in_body():
    module = load_selenium_module()

    class BodyElement:
        text = "Похожая вакансия ниже страницы\nВы откликнулись\nЧат"

    class FakeDriver:
        page_source = ""

        def execute_script(self, *_args):
            return {"hasModal": False, "controls": []}

        def find_element(self, *_args):
            return BodyElement()

        def find_elements(self, *_args):
            return []

    bot = module.HHSeleniumBot.__new__(module.HHSeleniumBot)
    bot.driver = FakeDriver()

    assert bot.detect_response_state() == "unknown"


def test_get_response_blocker_message_detects_captcha():
    module = load_selenium_module()

    class BodyElement:
        text = "Подтвердите, что вы не робот"

    class FakeDriver:
        page_source = BodyElement.text

        def find_element(self, *_args):
            return BodyElement()

    bot = module.HHSeleniumBot.__new__(module.HHSeleniumBot)
    bot.driver = FakeDriver()

    assert bot.get_response_blocker_message() == "Требуется капча"


def test_click_element_with_mouse_refuses_disabled_button():
    module = load_selenium_module()

    class DisabledButton:
        def is_enabled(self):
            return False

        def click(self):
            raise AssertionError("disabled button must not be clicked")

    bot = module.HHSeleniumBot.__new__(module.HHSeleniumBot)

    assert bot.click_element_with_mouse(DisabledButton()) is False


def test_find_visible_apply_text_buttons_skips_disabled_buttons():
    module = load_selenium_module()

    class Button:
        text = "Откликнуться"

        def __init__(self, enabled):
            self.enabled = enabled

        def is_displayed(self):
            return True

        def is_enabled(self):
            return self.enabled

        def get_attribute(self, _name):
            return None

    class FakeDriver:
        def find_elements(self, *_args):
            return [Button(False), Button(True)]

    bot = module.HHSeleniumBot.__new__(module.HHSeleniumBot)
    bot.driver = FakeDriver()

    buttons = bot.find_visible_apply_text_buttons()

    assert len(buttons) == 1
    assert buttons[0].enabled is True


def test_bot_accepts_debugger_address(tmp_path, monkeypatch):
    module = load_selenium_module()
    monkeypatch.setattr(module, "SCRIPT_DIR", str(tmp_path))

    bot = module.HHSeleniumBot(headless=False, debugger_address="127.0.0.1:9122")

    assert bot.debugger_address == "127.0.0.1:9122"


def test_selenium_filter_accepts_python_fallback_with_strict_include_config():
    module = load_selenium_module()
    bot = module.HHSeleniumBot.__new__(module.HHSeleniumBot)
    bot.config = {
        "allow_technical_fallback": True,
        "keywords_include": ["appsec", "pentest"],
        "keywords_exclude": [],
    }

    suitable, reason = bot.validate_security_title("Senior Python Developer")

    assert suitable is True
    assert reason == "OK"


def test_selenium_history_retries_old_skipped_filter_when_title_now_allowed():
    module = load_selenium_module()
    bot = module.HHSeleniumBot.__new__(module.HHSeleniumBot)
    bot.config = {
        "allow_technical_fallback": True,
        "keywords_include": ["appsec", "pentest"],
        "keywords_exclude": [],
    }
    bot.applied_vacancies = {
        "123": {
            "name": "Senior Python Developer",
            "date": "2026-06-16 13:00:00",
            "status": "skipped_filter",
        },
        "456": {
            "name": "Пентестер",
            "date": "2026-06-16 13:01:00",
            "status": "sent",
        },
    }

    assert bot.should_skip_known_vacancy("123", "Senior Python Developer") is False
    assert bot.should_skip_known_vacancy("456", "Пентестер") is True


def test_close_driver_does_not_wait_when_pause_disabled(monkeypatch):
    module = load_selenium_module()

    class FakeDriver:
        def __init__(self):
            self.closed = False

        def quit(self):
            self.closed = True

    bot = module.HHSeleniumBot.__new__(module.HHSeleniumBot)
    bot.driver = FakeDriver()
    bot.pause_before_close = False

    monkeypatch.setattr(
        module,
        "wait_before_browser_close",
        lambda: (_ for _ in ()).throw(AssertionError("wait must not be called")),
    )

    bot.close_driver()

    assert bot.driver.closed is True


def test_close_driver_waits_when_pause_enabled(monkeypatch):
    module = load_selenium_module()

    class FakeDriver:
        def __init__(self):
            self.closed = False

        def quit(self):
            self.closed = True

    calls = {"wait": 0}
    bot = module.HHSeleniumBot.__new__(module.HHSeleniumBot)
    bot.driver = FakeDriver()
    bot.pause_before_close = True

    def fake_wait():
        calls["wait"] += 1

    monkeypatch.setattr(module, "wait_before_browser_close", fake_wait)

    bot.close_driver()

    assert calls["wait"] == 1
    assert bot.driver.closed is True


def test_api_filter_rejects_project_manager_title():
    module = load_selenium_module()
    bot = module.HHSeleniumBot.__new__(module.HHSeleniumBot)
    bot.config = {"keywords_include": [], "keywords_exclude": []}

    suitable, _ = bot.is_api_vacancy_suitable({"name": "Менеджер проекта"})

    # Не ИБ — отсекается. «Менеджер по ИБ» с 25.09 проходит, поэтому причина
    # теперь не слово «менеджер», а отсутствие ИБ в названии.
    assert suitable is False


def test_api_filter_rejects_sales_title_even_with_security_context():
    module = load_selenium_module()
    bot = module.HHSeleniumBot.__new__(module.HHSeleniumBot)
    bot.config = {"keywords_include": [], "keywords_exclude": []}

    suitable, reason = bot.is_api_vacancy_suitable({
        "name": "Директор по продажам (Информационная безопасность)",
    })

    assert suitable is False
    assert "директор" in reason.lower() or "продаж" in reason.lower()


def test_api_filter_accepts_pentest_title():
    module = load_selenium_module()
    bot = module.HHSeleniumBot.__new__(module.HHSeleniumBot)
    bot.config = {"keywords_include": [], "keywords_exclude": []}

    suitable, reason = bot.is_api_vacancy_suitable({
        "name": "Специалист по анализу защищенности / Пентестер",
    })

    assert suitable is True
    assert reason == "OK"


def test_api_filter_accepts_appsec_title():
    module = load_selenium_module()
    bot = module.HHSeleniumBot.__new__(module.HHSeleniumBot)
    bot.config = {"keywords_include": [], "keywords_exclude": []}

    suitable, reason = bot.is_api_vacancy_suitable({"name": "AppSec-инженер"})

    assert suitable is True
    assert reason == "OK"


def test_api_filter_accepts_any_grade_security_title():
    """Уровень должности не отсекается — решение 24.09: подаёмся массово.

    Раньше «Junior-пентестер», «Начинающий специалист по ИБ» и «Начальник
    отдела ИБ» отсекались. Отказ по уровню — тоже данные для разбора.
    """
    module = load_selenium_module()
    bot = module.HHSeleniumBot.__new__(module.HHSeleniumBot)
    bot.config = {"keywords_include": [], "keywords_exclude": []}
    for title in ("Junior-пентестер",
                  "Начинающий специалист в области информационной безопасности",
                  "Начальник отдела информационной безопасности"):
        suitable, reason = bot.is_api_vacancy_suitable({"name": title})
        assert suitable is True, (title, reason)


def test_api_filter_user_exclusion_still_works():
    """Слово, вписанное в исключения вручную, по-прежнему отсекает."""
    module = load_selenium_module()
    bot = module.HHSeleniumBot.__new__(module.HHSeleniumBot)
    bot.config = {"keywords_include": [], "keywords_exclude": ["junior"]}
    suitable, reason = bot.is_api_vacancy_suitable({"name": "Junior-пентестер"})
    assert suitable is False
    assert "junior" in reason.lower()


def test_api_filter_rejects_student_work_author_title():
    module = load_selenium_module()
    bot = module.HHSeleniumBot.__new__(module.HHSeleniumBot)
    bot.config = {"keywords_include": [], "keywords_exclude": []}

    suitable, reason = bot.is_api_vacancy_suitable({
        "name": "Автор студенческих работ по направлению «Кибербезопасность»",
    })

    assert suitable is False
    assert "студенческ" in reason.lower()


def test_api_filter_accepts_certification_title():
    """25.09: сертификация СЗИ — работа в ИБ, подаёмся массово."""
    module = load_selenium_module()
    bot = module.HHSeleniumBot.__new__(module.HHSeleniumBot)
    bot.config = {"keywords_include": [], "keywords_exclude": []}

    suitable, _ = bot.is_api_vacancy_suitable({
        "name": "Специалист по сертификации (Кибербезопасность)",
    })

    assert suitable is True


def test_save_applied_syncs_shared_history_and_removes_from_cache(tmp_path):
    module = load_selenium_module()
    bot = module.HHSeleniumBot.__new__(module.HHSeleniumBot)
    bot.applied_vacancies = {}
    bot.applied_file = str(tmp_path / "applied_vacancies_selenium.json")
    bot.shared_applied_file = str(tmp_path / "applied_vacancies.json")
    bot.api_cache_file = str(tmp_path / "vacancies_cache.json")

    cache_data = {
        "total_count": 2,
        "vacancies": [
            {"id": "123", "name": "Пентестер"},
            {"id": "456", "name": "AppSec-инженер"},
        ],
    }
    Path(bot.api_cache_file).write_text(
        json.dumps(cache_data, ensure_ascii=False),
        encoding="utf-8",
    )

    bot.save_applied("123", "Пентестер", "sent")

    selenium_history = json.loads(Path(bot.applied_file).read_text(encoding="utf-8"))
    shared_history = json.loads(Path(bot.shared_applied_file).read_text(encoding="utf-8"))
    updated_cache = json.loads(Path(bot.api_cache_file).read_text(encoding="utf-8"))

    assert selenium_history["123"]["status"] == "sent"
    assert "123" in shared_history
    assert updated_cache["total_count"] == 1
    assert [vacancy["id"] for vacancy in updated_cache["vacancies"]] == ["456"]


def test_save_applied_does_not_sync_already_applied_as_daily_sent(tmp_path):
    module = load_selenium_module()
    bot = module.HHSeleniumBot.__new__(module.HHSeleniumBot)
    bot.applied_vacancies = {}
    bot.applied_file = str(tmp_path / "applied_vacancies_selenium.json")
    bot.shared_applied_file = str(tmp_path / "applied_vacancies.json")
    bot.api_cache_file = str(tmp_path / "vacancies_cache.json")

    Path(bot.shared_applied_file).write_text("{}", encoding="utf-8")
    Path(bot.api_cache_file).write_text(
        json.dumps({"total_count": 1, "vacancies": [{"id": "123"}]}),
        encoding="utf-8",
    )

    bot.save_applied("123", "Пентестер", "already_applied")

    shared_history = json.loads(Path(bot.shared_applied_file).read_text(encoding="utf-8"))
    updated_cache = json.loads(Path(bot.api_cache_file).read_text(encoding="utf-8"))

    assert "123" not in shared_history
    assert updated_cache["vacancies"] == []


def test_count_sent_today_counts_only_sent_status_in_rolling_24h_window():
    module = load_selenium_module()
    recent = (module.datetime.now() - module.timedelta(hours=23)).strftime("%Y-%m-%d %H:%M:%S")
    old = (module.datetime.now() - module.timedelta(hours=25)).strftime("%Y-%m-%d %H:%M:%S")
    bot = module.HHSeleniumBot.__new__(module.HHSeleniumBot)
    bot.applied_vacancies = {
        "1": {"date": recent, "status": "sent"},
        "2": {"date": recent, "status": "already_applied"},
        "3": {"date": old, "status": "sent"},
        "4": {"date": recent, "status": "sent_wrong_filter"},
        "5": "2026-06-09 10:00:00",
    }

    assert bot.count_sent_today() == 2


def test_count_recent_timestamps_uses_rolling_24h_window():
    module = load_auto_module()
    recent = (module.datetime.now() - module.timedelta(hours=23)).strftime("%Y-%m-%d %H:%M:%S")
    old = (module.datetime.now() - module.timedelta(hours=25)).strftime("%Y-%m-%d %H:%M:%S")

    assert module.count_recent_timestamps([recent, old, None, "bad date"], 24) == 1


def test_api_already_applied_does_not_invent_a_fresh_timestamp(tmp_path):
    module = load_auto_module()
    bot = module.HHAutoApplicant.__new__(module.HHAutoApplicant)
    old = (module.datetime.now() - module.timedelta(hours=25)).strftime("%Y-%m-%d %H:%M:%S")
    bot.applied_vacancies = {"old": old}
    bot.processed_vacancy_ids = set()
    bot.applied_today = 0
    bot.applied_vacancies_file = str(tmp_path / "history.json")
    removed = []
    bot.remove_from_cache = lambda vacancy_id: removed.append(vacancy_id)

    bot.save_applied_vacancy("old", count_as_new=False)
    bot.save_applied_vacancy("unknown-date", count_as_new=False)

    history = json.loads(Path(bot.applied_vacancies_file).read_text(encoding="utf-8"))
    assert history["old"] == old
    assert history["unknown-date"] is None
    assert module.count_recent_timestamps(list(history.values()), 24) == 0
    assert bot.applied_today == 0
    assert bot.processed_vacancy_ids == {"old", "unknown-date"}
    assert removed == ["old", "unknown-date"]


def test_default_launcher_uses_hh_limit_not_local_count(monkeypatch, capsys):
    from types import SimpleNamespace
    module = load_auto_module()
    bot = module.HHAutoApplicant.__new__(module.HHAutoApplicant)
    bot.applied_vacancies = {}
    bot.selenium_headless = True
    commands = []
    monkeypatch.setattr(module, 'load_config', lambda: {'max_applications': 200})
    monkeypatch.setattr(module.subprocess, 'run', lambda command, **_kwargs:
                        commands.append(command) or SimpleNamespace(returncode=0))
    bot.run_selenium_api_cache()
    assert '--until-hh-limit' in commands[0]
    assert '--limit' not in commands[0]
    assert 'осталось' not in capsys.readouterr().out


def test_launcher_preserves_explicit_cap(monkeypatch):
    from types import SimpleNamespace
    module = load_auto_module()
    bot = module.HHAutoApplicant.__new__(module.HHAutoApplicant)
    bot.applied_vacancies = {}
    commands = []
    monkeypatch.setattr(module, 'load_config', lambda: {'max_applications': 200})
    monkeypatch.setattr(module.subprocess, 'run', lambda command, **_kwargs:
                        commands.append(command) or SimpleNamespace(returncode=0))
    bot.run_selenium_api_cache(20)
    assert commands[0][-2:] == ['--limit', '20']


def test_auto_loader_uses_selenium_history_as_processed_ids(tmp_path):
    module = load_auto_module()
    bot = module.HHAutoApplicant.__new__(module.HHAutoApplicant)
    bot.applied_vacancies_file = str(tmp_path / "applied_vacancies.json")
    bot.selenium_applied_vacancies_file = str(tmp_path / "applied_vacancies_selenium.json")
    recent = (module.datetime.now() - module.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")

    Path(bot.applied_vacancies_file).write_text(
        json.dumps({"111": recent}, ensure_ascii=False),
        encoding="utf-8",
    )
    Path(bot.selenium_applied_vacancies_file).write_text(
        json.dumps(
            {
                "222": {
                    "name": "Менеджер проекта",
                    "date": "2026-06-16 11:00:00",
                    "status": "skipped_filter",
                },
                "333": {
                    "name": "Пентестер",
                    "date": "2026-06-16 11:30:00",
                    "status": "already_applied",
                },
                "444": {
                    "name": "Senior Python Developer",
                    "date": "2026-06-16 12:00:00",
                    "status": "skipped_filter",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    bot.load_applied_vacancies()

    assert bot.applied_vacancies == {"111": recent}
    assert bot.processed_vacancy_ids == {"111", "222", "333"}
    assert bot.applied_today == 1


def test_auto_filter_accepts_technical_fallback_and_rejects_sales():
    module = load_auto_module()
    bot = module.HHAutoApplicant.__new__(module.HHAutoApplicant)
    bot.allow_technical_fallback = True

    assert bot.is_vacancy_suitable({"name": "Senior Python Developer"}) is True
    assert bot.is_vacancy_suitable({"name": "Sales Manager Python"}) is False


def test_auto_cache_excludes_selenium_processed_ids(tmp_path):
    module = load_auto_module()
    bot = module.HHAutoApplicant.__new__(module.HHAutoApplicant)
    bot.vacancies_cache_file = str(tmp_path / "vacancies_cache.json")
    bot.processed_vacancy_ids = {"222"}

    bot.save_vacancies_cache([
        {"id": "111", "name": "Пентестер"},
        {"id": "222", "name": "Уже пройденная фильтром"},
    ])

    cache = json.loads(Path(bot.vacancies_cache_file).read_text(encoding="utf-8"))

    assert cache["total_count"] == 1
    assert [vacancy["id"] for vacancy in cache["vacancies"]] == ["111"]


def test_get_vacancies_keeps_partial_results_after_network_error(tmp_path):
    module = load_auto_module()

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    bot = module.HHAutoApplicant.__new__(module.HHAutoApplicant)
    bot.applied_vacancies = {}
    bot.processed_vacancy_ids = set()
    bot.vacancies_cache_file = str(tmp_path / "vacancies_cache.json")
    bot.cache_lifetime_hours = 6
    bot.min_vacancies_in_cache = 50
    bot.base_url = "https://api.hh.ru"
    bot.sync_cache_with_applied = lambda: None
    bot.load_vacancies_cache = lambda: None
    bot.is_vacancy_suitable = lambda _vacancy: True
    bot.get_vacancy_priority = lambda _vacancy: 1

    calls = {"count": 0}

    def fake_request(_method, _url, params=None):
        calls["count"] += 1
        if calls["count"] == 1:
            return FakeResponse({
                "items": [
                    {
                        "id": "123",
                        "name": "Пентестер",
                        "has_test": False,
                        "employer": {"name": "HH"},
                    },
                ],
                "pages": 2,
            })
        raise requests.exceptions.ReadTimeout("timeout")

    bot.make_application_request = fake_request

    vacancies = bot.get_vacancies({})
    cache = json.loads(Path(bot.vacancies_cache_file).read_text(encoding="utf-8"))

    assert [vacancy["id"] for vacancy in vacancies] == ["123"]
    assert [vacancy["id"] for vacancy in cache["vacancies"]] == ["123"]


def test_process_api_vacancies_stops_on_hh_response_limit():
    module = load_selenium_module()
    bot = module.HHSeleniumBot.__new__(module.HHSeleniumBot)
    bot.config = {
        "max_applications": 200,
        "skip_applied": True,
        "skip_with_tests": True,
    }
    bot.applied_today = 0
    bot.applied_vacancies = {}
    bot.skipped = 0
    bot.errors = 0
    bot.response_limit_reached = False
    bot.api_cache_file = ""
    bot.is_api_vacancy_suitable = lambda _vacancy: (True, "")
    bot.apply_to_vacancy = lambda _url, _name: (False, "Лимит откликов")
    bot.random_delay = lambda _delay_range: None
    bot.save_applied = lambda *_args: (_ for _ in ()).throw(AssertionError("save_applied called"))

    processed = bot.process_api_vacancies([
        {
            "id": "123",
            "name": "Пентестер",
            "alternate_url": "https://hh.ru/vacancy/123",
            "employer": {"name": "HH"},
        },
        {
            "id": "456",
            "name": "SOC analyst",
            "alternate_url": "https://hh.ru/vacancy/456",
            "employer": {"name": "HH"},
        },
    ])

    assert processed == 0
    assert bot.response_limit_reached is True
    assert bot.errors == 0


def test_process_api_vacancies_removes_deterministic_skips_from_cache(tmp_path):
    module = load_selenium_module()
    bot = module.HHSeleniumBot.__new__(module.HHSeleniumBot)
    bot.config = {
        "max_applications": 200,
        "skip_applied": True,
        "skip_with_tests": True,
    }
    bot.applied_today = 0
    bot.applied_vacancies = {"111": {"status": "sent"}}
    bot.skipped = 0
    bot.errors = 0
    bot.response_limit_reached = False
    bot.api_cache_file = str(tmp_path / "vacancies_cache.json")
    bot.applied_file = str(tmp_path / "applied_vacancies_selenium.json")
    bot.shared_applied_file = str(tmp_path / "applied_vacancies.json")
    bot.is_api_vacancy_suitable = lambda _vacancy: (False, "Исключено тестом")
    bot.apply_to_vacancy = lambda *_args: (_ for _ in ()).throw(AssertionError("apply_to_vacancy called"))
    bot.random_delay = lambda _delay_range: None
    Path(bot.shared_applied_file).write_text("{}", encoding="utf-8")

    vacancies = [
        {"id": "111", "name": "Уже в истории", "alternate_url": "https://hh.ru/vacancy/111"},
        {"id": "222", "name": "Нет ссылки"},
        {"id": "333", "name": "С тестом", "alternate_url": "https://hh.ru/vacancy/333", "has_test": True},
        {"id": "444", "name": "Не подходит", "alternate_url": "https://hh.ru/vacancy/444"},
    ]
    Path(bot.api_cache_file).write_text(
        json.dumps({"total_count": len(vacancies), "vacancies": vacancies}, ensure_ascii=False),
        encoding="utf-8",
    )

    processed = bot.process_api_vacancies(vacancies)
    updated_cache = json.loads(Path(bot.api_cache_file).read_text(encoding="utf-8"))
    selenium_history = json.loads(Path(bot.applied_file).read_text(encoding="utf-8"))
    shared_history = json.loads(Path(bot.shared_applied_file).read_text(encoding="utf-8"))

    assert processed == 0
    assert bot.skipped == 4
    assert bot.errors == 0
    assert updated_cache["total_count"] == 0
    assert updated_cache["vacancies"] == []
    assert selenium_history["222"]["status"] == "skipped_no_safe_url"
    assert selenium_history["333"]["status"] == "skipped_test"
    assert selenium_history["444"]["status"] == "skipped_filter"
    assert shared_history == {}


def test_fill_cover_letter_writes_visible_letter_textarea():
    module = load_selenium_module()

    class FakeTextArea:
        id = "letter-field"
        tag_name = "textarea"

        def __init__(self):
            self.value = ""

        def is_displayed(self):
            return True

        def get_attribute(self, name):
            if name == "value":
                return self.value
            if name == "placeholder":
                return "Сопроводительное письмо"
            return ""

        @property
        def text(self):
            return self.value

        def click(self):
            return None

        def send_keys(self, *keys):
            for key in keys:
                if key == module.Keys.DELETE:
                    self.value = ""
                elif key in (module.Keys.CONTROL, "a"):
                    continue
                else:
                    self.value += str(key)

    class FakeBody:
        text = "Отклик на вакансию\nСопроводительное письмо"

    class FakeDriver:
        page_source = ""

        def __init__(self):
            self.field = FakeTextArea()

        def find_elements(self, *_args):
            return [self.field]

        def find_element(self, *_args):
            return FakeBody()

        def execute_script(self, script, *args):
            if "parent.innerText" in script:
                return "сопроводительное письмо"
            return None

    bot = module.HHSeleniumBot.__new__(module.HHSeleniumBot)
    bot.driver = FakeDriver()

    assert bot.fill_cover_letter("Добрый день") is True
    assert bot.driver.field.value == "Добрый день"


def test_handle_warning_popups_clicks_relocation_confirm():
    module = load_selenium_module()

    class FakeButton:
        def __init__(self, text):
            self.text = text
            self.clicked = False

        def is_displayed(self):
            return True

    class FakeDriver:
        def __init__(self, btn):
            self.btn = btn

        def find_elements(self, *_args):
            return [self.btn]

        def execute_script(self, *_args):
            return False

    btn = FakeButton("Все равно откликнуться")
    bot = module.HHSeleniumBot.__new__(module.HHSeleniumBot)
    bot.driver = FakeDriver(btn)
    bot.get_visible_page_text = lambda: "Вы откликаетесь на вакансию в другой стране. Все равно откликнуться"
    bot.is_disabled_element = lambda _el: False
    bot.click_element_with_mouse = lambda el: setattr(el, 'clicked', True) or True

    assert bot.handle_warning_popups() is True
    assert btn.clicked is True


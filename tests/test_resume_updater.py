import sys
import pytest
from unittest.mock import MagicMock, patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from resume_updater import HHResumeUpdater, ADVANCED_SKILLS


def test_resume_updater_init():
    updater = HHResumeUpdater(resume_id="test_id_123", headless=True)
    assert updater.resume_id == "test_id_123"
    assert updater.headless is True
    assert updater.driver is None


def test_get_current_resume_status_with_skills_card():
    updater = HHResumeUpdater(headless=True)
    updater.driver = MagicMock()
    updater.driver.current_url = "https://hh.ru/resume/123"

    # Заголовок позиции
    title_elem = MagicMock()
    title_elem.text = "Application Security Engineer"

    # Карточка навыков с уровнями
    card_elem = MagicMock()
    card_elem.text = "Продвинутый уровень\nRed Team\nBurp Suite\nСредний уровень\nPython\nDocker\nРедактировать"

    def mock_find_element(by, selector):
        if "title-position" in selector:
            return title_elem
        if "skills-card" in selector:
            return card_elem
        raise Exception("Not found")

    updater.driver.find_element = mock_find_element
    updater.driver.find_elements = MagicMock(return_value=[])

    status = updater.get_current_resume_status()
    assert status["success"] is True
    assert status["position"] == "Application Security Engineer"
    assert "Red Team" in status["skills"]
    assert "Burp Suite" in status["skills"]
    assert "Python" in status["skills"]
    assert "Docker" in status["skills"]
    assert "Продвинутый уровень" not in status["skills"]
    assert status["skills_count"] == 4


def test_activate_skills_logic():
    updater = HHResumeUpdater(headless=True)
    updater.driver = MagicMock()

    # Уровень засчитывается только при ПОДТВЕРЖДЁННОМ состоянии кнопки
    # (_level_button_state читает aria-checked/aria-pressed/aria-selected,
    # is_selected или изменившийся class). Раньше счётчик считал клики, и
    # промах по кнопке всё равно попадал в «проставлены уровни у 30 навыков».
    clicked = set()

    class FakeActionChains:
        """Клик идёт настоящей мышью, а MagicMock не WebElement — подменяем цепочку."""

        def __init__(self, driver):
            self.target = None

        def move_to_element(self, element):
            self.target = element
            return self

        def pause(self, seconds):
            return self

        def click(self):
            return self

        def perform(self):
            if self.target is not None:
                clicked.add(id(self.target))

    def make_skill(skill_name, confirms):
        """Навык с кнопкой уровня. confirms=False — клик прошёл мимо, hh состояние не сменил."""
        level_btn = MagicMock()

        def get_attribute(attr):
            if attr == 'aria-checked':
                return 'true' if (confirms and id(level_btn) in clicked) else 'false'
            return 'magritte-level-button'

        level_btn.get_attribute = get_attribute

        name_elem = MagicMock()
        name_elem.text = skill_name

        def find_element(by, selector):
            return name_elem if 'skillName' in selector else level_btn

        item = MagicMock()
        item.find_element = find_element
        return item

    # Первому навыку hh подтверждает уровень, второму — нет.
    items = [make_skill("Burp Suite", True), make_skill("API", False)]
    updater.driver.find_elements = MagicMock(return_value=items)
    updater.driver.find_element = MagicMock(return_value=MagicMock())

    with patch('selenium.webdriver.common.action_chains.ActionChains', FakeActionChains):
        success, msg, count = updater.activate_and_save_all_skills()

    assert success is True
    # Кликов было два, подтверждённых уровней — один. В счётчик идёт результат.
    assert count == 1


def test_add_skills_to_resume():
    updater = HHResumeUpdater(headless=True)
    updater.driver = MagicMock()

    # Что уже выбрано в редакторе навыков. Код читает это через execute_script
    # по чипам, а не по тексту страницы.
    chips = {"python", "docker"}
    recommended = ["КриптоПро", "SAST"]

    def mock_execute_script(script, *args):
        if "chips-trigger-chip-" in script:
            return sorted(chips)
        if "querySelectorAll" in script and "skills-recommended-" in script:
            return list(recommended)
        if "skills-recommended-" in script:
            # Клик по чекбоксу рекомендации — навык уходит в чипы.
            chips.add(str(args[0]).strip().lower())
            return None
        if "Количество навыков превышено" in script:
            return None
        if "skills-element" in script:
            # Успех подтверждается ТЕГАМИ навыков со страницы резюме, а не
            # подстрокой по всему тексту: подстрока засчитывала «Go» словом
            # «договор», а «AD» — словом «Град».
            return ["Python", "Docker", "КриптоПро", "SAST"]
        return None

    updater.driver.execute_script = mock_execute_script
    updater.driver.find_element = MagicMock(return_value=MagicMock())

    # Добавляем новые навыки (Python уже есть, добавится только КриптоПро и SAST)
    success, msg, added = updater.add_skills_to_resume(["КриптоПро", "Python", "SAST"])
    assert success is True
    assert "КриптоПро" in added
    assert "SAST" in added
    assert "Python" not in added


def test_update_about_section():
    updater = HHResumeUpdater(headless=True)
    updater.driver = MagicMock()

    about_btn = MagicMock()
    about_btn.is_displayed.return_value = True

    textarea = MagicMock()
    textarea.get_attribute.return_value = "Базовый опыт разработки и тестирования."

    save_btn = MagicMock()

    def mock_find_elem(by, selector):
        if "button-about" in selector:
            return about_btn
        if "editor-about" in selector:
            return textarea
        if "edit-save" in selector:
            return save_btn
        return MagicMock()

    updater.driver.find_element = mock_find_elem

    # Сохранение подтверждается перечитыванием поля «О себе» со страницы
    # редактора, а не фактом клика: JS-клик по «Сохранить» hh молча
    # игнорировал, и лог писал «успешно сохранено» на несохранённой правке.
    # Мок возвращает текст с добавленным блоком — правка реально сохранилась.
    saved_about = (
        "Базовый опыт разработки и тестирования."
        "\n\nДополнительные компетенции и стандарты (ATS / Enterprise):\n"
        "СКЗИ, КриптоПро, ГОСТ, Active Directory, MaxPatrol SIEM."
    )
    updater.driver.execute_script = MagicMock(return_value=saved_about)

    ok, msg = updater.update_about_section(["КриптоПро", "СКЗИ"])
    assert ok is True
    # Слова «успешно» в ответе нет и быть не должно: код сообщает ровно то,
    # что проверил — блок дополнен и сохранение подтверждено чтением.
    assert "подтверждено" in msg.lower()
    textarea.send_keys.assert_called_once()


def test_apply_full_modernization():
    updater = HHResumeUpdater(headless=True)
    updater.sync_adaptive_skills = MagicMock(return_value=(True, "Обновлено 5 навыков", 30))
    updater.update_about_section = MagicMock(return_value=(True, "Раздел дополнен"))

    res = updater.apply_full_modernization(["Kubernetes", "КриптоПро"])
    assert res["skills_updated"] is True
    assert res["about_updated"] is True
    assert res["skills_count"] == 30




def test_next_bump_schedule():
    """Когда пробовать поднять резюме снова: +4 ч после успеха, время от hh, иначе +30 мин."""
    from datetime import datetime
    from resume_updater import next_bump_at
    now = datetime(2026, 9, 23, 18, 10).timestamp()
    at = lambda t: datetime.fromtimestamp(t).strftime('%d %H:%M')
    assert at(next_bump_at(True, '', now)) == '23 22:11'
    assert at(next_bump_at(False, 'следующее бесплатное поднятие — сегодня в 22:03', now)) == '23 22:03'
    # Время уже прошло сегодня — значит, завтра.
    assert at(next_bump_at(False, 'сегодня в 12:46', now)) == '24 12:46'
    assert at(next_bump_at(False, 'Кнопка не найдена', now)) == '23 18:40'


def test_bump_reads_tomorrow_cooldown(tmp_path, monkeypatch):
    """28.09 после 20:00 hh пишет «Можно завтра в 02:31» — это кулдаун, а не «кнопка не найдена»."""
    import resume_updater as ru
    from datetime import datetime
    monkeypatch.setattr(ru, 'SCRIPT_DIR', str(tmp_path))
    updater = HHResumeUpdater(resume_id='abc', headless=True)
    updater.driver = MagicMock()
    updater.driver.find_elements.return_value = []
    updater.driver.execute_script.return_value = 'Поднятие резюме\nМожно завтра в 02:31\nПоднимать автоматически'
    updater._init_driver = lambda: True
    ok, msg = updater.bump_resume()
    assert ok is False
    assert 'завтра в 02:31' in msg and 'не найдена' not in msg
    import json
    nxt = json.load(open(tmp_path / ru.BUMP_SCHEDULE_FILE, encoding='utf-8'))['next_at']
    assert datetime.fromtimestamp(nxt).strftime('%H:%M') == '02:31'


@pytest.mark.parametrize('method', ['promote_resume', 'bump_resume'])
def test_resume_load_timeout_keeps_ready_page(monkeypatch, method):
    from selenium.common.exceptions import TimeoutException
    updater = HHResumeUpdater(resume_id='abc', headless=True)
    updater.driver = MagicMock()
    updater.driver.current_url = 'https://krasnoyarsk.hh.ru/resume/abc'
    updater.driver.get.side_effect = TimeoutException('subresource timeout')
    title = MagicMock()
    title.text = 'AppSec'
    updater.driver.find_elements.side_effect = lambda by, selector: (
        [title] if selector == '[data-qa="resume-block-title-position"]' else [])
    updater.driver.execute_script.return_value = 'Можно завтра в 02:31'
    updater.is_driver_alive = lambda: True
    updater._init_driver = lambda: True
    updater.read_about_section = MagicMock(return_value=None)
    monkeypatch.setattr('resume_updater.time.sleep', lambda _: None)
    result = getattr(updater, method)()
    msg = result['bump_message'] if method == 'promote_resume' else result[1]
    assert 'завтра в 02:31' in msg


@pytest.mark.parametrize('url', ['https://hh.ru.evil.test/resume/abc',
                              'https://hh.ru/resume/other', 'https://hh.ru/login'])
def test_timed_out_resume_load_never_clicks_wrong_page(monkeypatch, url):
    from selenium.common.exceptions import TimeoutException
    updater = HHResumeUpdater(resume_id='abc', headless=True)
    updater.driver = MagicMock()
    updater.driver.current_url = url
    updater.driver.get.side_effect = TimeoutException('subresource timeout')
    updater._init_driver = lambda: True
    monkeypatch.setattr('resume_updater.time.sleep', lambda _: None)
    assert not updater.bump_resume()[0]
    updater.driver.find_elements.assert_not_called()

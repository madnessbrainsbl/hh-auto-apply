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
    title_elem.text = "Backend Engineer"

    # Карточка навыков с уровнями
    card_elem = MagicMock()
    card_elem.text = "Продвинутый уровень\nGo\nDjango\nСредний уровень\nPython\nDocker\nРедактировать"

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
    assert status["position"] == "Backend Engineer"
    assert "Go" in status["skills"]
    assert "Django" in status["skills"]
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
    items = [make_skill("Django", True), make_skill("API", False)]
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
    recommended = ["Kafka", "Redis"]

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
            return ["Python", "Docker", "Kafka", "Redis"]
        return None

    updater.driver.execute_script = mock_execute_script
    updater.driver.find_element = MagicMock(return_value=MagicMock())

    # Добавляем новые навыки (Python уже есть, добавится только Kafka и Redis)
    success, msg, added = updater.add_skills_to_resume(["Kafka", "Python", "Redis"])
    assert success is True
    assert "Kafka" in added
    assert "Redis" in added
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
    addition = "Дополнительно: PostgreSQL, Kafka, нагрузочное тестирование."
    saved_about = "Базовый опыт разработки и тестирования.\n\n" + addition
    updater.driver.execute_script = MagicMock(return_value=saved_about)

    with patch('config_manager.load_config', return_value={'about_addition': addition}):
        ok, msg = updater.update_about_section()
    assert ok is True
    # Слова «успешно» в ответе нет и быть не должно: код сообщает ровно то,
    # что проверил — блок дополнен и сохранение подтверждено чтением.
    assert "подтверждено" in msg.lower()
    textarea.send_keys.assert_called_once()


def test_update_about_section_skips_without_addition():
    """Текст для «О себе» не задан — раздел не трогаем и браузер не открываем."""
    updater = HHResumeUpdater(headless=True)
    updater.driver = MagicMock()
    with patch('config_manager.load_config', return_value={}):
        ok, msg = updater.update_about_section()
    assert ok is True
    assert "не задан" in msg
    updater.driver.get.assert_not_called()


def test_apply_full_modernization():
    updater = HHResumeUpdater(headless=True)
    updater.sync_adaptive_skills = MagicMock(return_value=(True, "Обновлено 5 навыков", 30))
    updater.update_about_section = MagicMock(return_value=(True, "Раздел дополнен"))

    res = updater.apply_full_modernization(["Kubernetes", "Kafka"])
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

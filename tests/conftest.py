"""Общая защита боевых данных проекта от тестов.

Тесты гоняют настоящие классы бота, а те по умолчанию открывают файлы в корне
проекта: hh_data.db, hh_selenium_config.json, applied_vacancies*.json,
vacancies_cache.json. Это не теория: run_analysis в тестах анализатора уже
залил в боевую базу выдуманную запись «Fintech Platform / Backend», а
HHSeleniumBot.__init__ на каждом прогоне перезаписывал hh_selenium_config.json
(load_config всегда вызывает save_config).

Чинить это в каждом тесте по отдельности бессмысленно: путь по умолчанию
берётся из модульных констант, поэтому подменяем сами константы — одна
автоиспользуемая фикстура закрывает все файлы разом, включая те тесты, которые
создают DatabaseManager() неявно (AIAssistant, RejectionAnalyzer, HHSeleniumBot).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Тесты не пишут в журналы проекта (hh_selenium.log и прочие): иначе их строки
# смешиваются с живым прогоном и путают его разбор. Глушим саму запись в файлы
# проекта, и делаем это при импорте conftest — раньше, чем тестовые модули
# импортируют faker и прочее, что логирует уже при загрузке.
import logging as _logging  # noqa: E402

_PROJECT_DIR = str(Path(__file__).resolve().parents[1]).lower()
_original_emit = _logging.FileHandler.emit


def _emit_outside_project(self, record):
    if str(getattr(self, 'baseFilename', '')).lower().startswith(_PROJECT_DIR):
        return
    return _original_emit(self, record)


_logging.FileHandler.emit = _emit_outside_project

import db_manager  # noqa: E402
import hh_selenium  # noqa: E402
import rejection_analyzer  # noqa: E402


@pytest.fixture(autouse=True)
def isolate_project_data(tmp_path, monkeypatch):
    """Направляет все пути по умолчанию во временную папку теста."""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()

    # База откликов и отказов: DatabaseManager() без аргумента берёт этот путь.
    monkeypatch.setattr(db_manager, "DEFAULT_DB_PATH", str(sandbox / "hh_data.db"))
    # Конфиг, история откликов, кеш вакансий и флаги паузы/стопа бота.
    monkeypatch.setattr(hh_selenium, "SCRIPT_DIR", str(sandbox))
    # Отчёты и кеш отказов анализатора.
    monkeypatch.setattr(rejection_analyzer, "SCRIPT_DIR", str(sandbox))
    # Расписание поднятия резюме и геометрия окна. Без этого тест поднятия на
    # заглушке записал боевое расписание, и живой прогон не поднял резюме.
    import resume_updater
    monkeypatch.setattr(resume_updater, "SCRIPT_DIR", str(sandbox))
    # Claude и Codex на этой машине настоящие: тест, где Gemini и Groq
    # «отказали», вызвал бы их и потратил подписку пользователя.
    import ai_assistant
    monkeypatch.setattr(ai_assistant.AIAssistant, "find_cli", staticmethod(lambda name: None))
    # Antigravity у пользователя запущен локально: тест с «отказавшими» ИИ
    # сходил бы в него по-настоящему.
    monkeypatch.setattr(ai_assistant.AIAssistant, "_compat_providers", lambda self: [])
    # Статистика ИИ для режима «Авто» — в песочницу, не в рабочий ai_stats.json.
    monkeypatch.setattr(ai_assistant.AIAssistant, "_stats_path",
                        lambda self: str(sandbox / "ai_stats.json"))
    yield

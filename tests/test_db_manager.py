import os
import sys
import tempfile
import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db_manager import DatabaseManager
from ai_assistant import AIAssistant


@pytest.fixture
def temp_db():
    temp_dir = tempfile.mkdtemp()
    db_path = os.path.join(temp_dir, "test_hh_data.db")
    db = DatabaseManager(db_path=db_path)
    yield db
    if os.path.exists(db_path):
        os.remove(db_path)


def test_record_application_and_stats(temp_db):
    success = temp_db.record_application(
        vacancy_id="123456",
        title="Pentester / Анализ защищенности",
        company="Банк ТОП-10",
        url="https://hh.ru/vacancy/123456",
        cover_letter="Добрый день! Заинтересовала позиция...",
        questions_count=2,
        ats_score=75,
        detected_skills=["Burp Suite", "OWASP"],
        status="sent"
    )
    assert success is True

    stats = temp_db.get_stats()
    assert stats["total_applications"] == 1
    assert stats["pending_applications"] == 1
    assert stats["invitations"] == 0
    assert stats["discards"] == 0


def test_rejection_analysis_and_auto_learning(temp_db):
    # 1. Записываем отклик
    temp_db.record_application(
        vacancy_id="999888",
        title="AppSec Engineer",
        company="Fintech Co",
        url="https://hh.ru/vacancy/999888",
        cover_letter="Добрый день...",
        status="sent"
    )

    # 2. Фиксируем отказ с дефицитом ключевых навыков
    rej_id = temp_db.record_rejection_analysis(
        vacancy_id="999888",
        title="AppSec Engineer",
        company="Fintech Co",
        url="https://hh.ru/vacancy/999888",
        rejection_reason="Отказ автоматического скрининга",
        ats_score=35,
        missing_keywords=["Kubernetes", "Active Directory"],
        knockout_filters=["Опыт работы менее 5 лет"],
        remediation_advice=["Добавить Kubernetes и Active Directory в блок ключевых навыков"]
    )
    assert rej_id > 0

    # 3. Проверяем обновление статуса отклика на 'discarded'
    stats = temp_db.get_stats()
    assert stats["discards"] == 1
    assert stats["pending_applications"] == 0
    assert stats["total_rejections_audited"] == 1
    # Причину назвал не работодатель, а сам бот («автоматический скрининг»),
    # реплик работодателя в вызов не передавали — значит строка идёт как
    # догадка и в среднее по ats не попадает. Среднее по догадкам было бы
    # красивой, но пустой цифрой, поэтому его и убрали из подсчёта.
    assert stats["rejections_confirmed"] == 0
    assert stats["rejections_guessed"] == 1
    assert stats["average_ats_score"] == 0

    # 4. Проверяем, что адаптивные навыки выучены
    # Навык кладётся в базу в канонической форме (canonical_skill_name):
    # иначе «AppSec», «appsec» и «Application Security» живут тремя строками
    # с раздробленными счётчиками, а порядок навыков в письмах и резюме
    # отражает раздробленность, а не спрос.
    learned_skills = [s.lower() for s in temp_db.get_adaptive_skills()]
    assert "kubernetes" in learned_skills
    assert "active directory" in learned_skills


def test_auto_fix_closed_loop(temp_db):
    """Проверяет полный цикл: отказ -> фиксация причины -> авто-исправление следующего письма."""
    # 1. Обучаем базу на отказе, где не хватило навыка 'Kubernetes'
    temp_db.record_rejection_analysis(
        vacancy_id="555444",
        title="DevSecOps Engineer",
        company="Cloud Services",
        rejection_reason="Недостаточный стек",
        ats_score=40,
        missing_keywords=["Kubernetes"]
    )

    # 2. Инициализируем ИИ-ассистента с этой базой данных
    assistant = AIAssistant()
    assistant.db = temp_db

    # 3. Генерируем письмо для новой вакансии, требующей Kubernetes
    cover_letter = assistant.generate_cover_letter(
        vacancy_title="Инженер DevSecOps / AppSec",
        company_name="MegaCorp",
        vacancy_description="Ищем эксперта по безопасности контейнеров Kubernetes, Docker и CI/CD.",
        skills_list=["Kubernetes", "Docker", "Python"]
    )

    # 4. Навык из отказа в письмо не попадает, пока его нет в профиле кандидата:
    # 05.10 письма уходили со «стек: CI/CD, Ansible, SAST, Terraform», которых у
    # кандидата нет. Чему учит отказ, решает правка профиля, а не шаблон письма.
    assert "kubernetes" not in cover_letter.lower()


def test_guessed_reason_is_not_counted_as_confirmed(temp_db):
    """«Подтверждено работодателем» = в чате были его реплики, а не текст причины.

    Раньше признак выводился из ТЕКСТА причины, и свободная формулировка ИИ
    («Работодатель ищет специалиста по КИИ») проходила как подтверждённая,
    хотя работодатель не написал ни слова. Теперь признак считается ровно по
    одному правилу: остались ли непустые реплики работодателя.
    """
    # шаблон fallback-ветки — слов работодателя за ним нет
    temp_db.record_rejection_analysis(
        vacancy_id="1", title="Инженер ИБ", company="Альфа",
        rejection_reason="Недостаточный стаж или несоответствие требуемому грейду",
        ats_score=85
    )
    # причина взята из настоящей реплики работодателя — её и передаём
    temp_db.record_rejection_analysis(
        vacancy_id="2", title="Пентестер", company="Бета",
        rejection_reason="Нам нужен специалист по КИИ и 187-ФЗ, а не пентестер",
        ats_score=70,
        employer_messages=["Нам нужен специалист по КИИ и 187-ФЗ, а не пентестер"]
    )
    # чат открыли, но реплик работодателя в нём не нашлось: правдоподобный
    # текст причины сам по себе подтверждением не является
    temp_db.record_rejection_analysis(
        vacancy_id="3", title="AppSec", company="Гамма",
        rejection_reason="Текст из чата без ответа работодателя",
        ats_score=70, employer_messages=[]
    )

    stats = temp_db.get_stats()
    assert stats["total_rejections_audited"] == 3
    assert stats["rejections_confirmed"] == 1
    assert stats["rejections_guessed"] == 2
    # среднее считается только по подтверждённой строке
    assert stats["average_ats_score"] == 70.0

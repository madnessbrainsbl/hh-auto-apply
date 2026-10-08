import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import os
import json
import pytest
from rejection_analyzer import RejectionAnalyzer


@pytest.fixture
def analyzer(tmp_path):
    config_file = str(tmp_path / "hh_selenium_config.json")
    with open(config_file, "w", encoding="utf-8") as f:
        json.dump({
            "ai_config": {"enabled": False},
            "candidate_profile": {
                "name": "Иван",
                "specialization": "Информационная безопасность",
                "experience_years": 3,
                "skills": ["Python", "OWASP", "Burp Suite", "Linux"],
                "expected_salary": "180 000 руб."
            }
        }, f)
    return RejectionAnalyzer(config_file=config_file, headless=True)


def test_analyzer_sample_vacancies(analyzer):
    samples = analyzer._load_sample_vacancies(limit=3)
    assert len(samples) > 0
    assert "vacancy_title" in samples[0]
    assert "company_name" in samples[0]


def test_analyzer_run_analysis_mock(analyzer, tmp_path):
    summary = analyzer.run_analysis(limit=2, use_mock_if_empty=True, fetch_live=False)
    assert summary["total_analyzed"] > 0
    assert "detailed_analyses" in summary
    assert summary["top_missing_keywords"]
    # Показатель ats_score признан фикцией: его никто не измерял, при нехватке
    # данных подставлялась константа. Ключа в отчёте больше нет, и это часть
    # контракта — проверяем именно отсутствие, чтобы он не вернулся.
    assert "average_ats_score" not in summary

    # Проверяем сохранение отчетов
    md_path = str(tmp_path / "report.md")
    html_path = str(tmp_path / "report.html")
    analyzer._generate_markdown_report(summary, filepath=md_path)
    analyzer._generate_html_report(summary, filepath=html_path)

    assert os.path.exists(md_path)
    assert os.path.exists(html_path)

    with open(md_path, 'r', encoding='utf-8') as f:
        content = f.read()
        assert "Отчёт о разборе отказов" in content
        assert "Топ-10 навыков, которых не хватило" in content
        assert "Детальный разбор по каждой вакансии" in content
        assert "ATS Match Score" not in content

    with open(html_path, 'r', encoding='utf-8') as f:
        html = f.read()
        assert "<!DOCTYPE html>" in html
        assert "Разбор отказов на hh.ru" in html
        assert "Топ навыков, которых не хватило" in html
        assert "Аудит отказов и ATS-фильтров" not in html


def test_analyzer_sample_chats(analyzer):
    sample_chats = analyzer._load_sample_chats(limit=3)
    assert len(sample_chats) > 0
    first = sample_chats[0]
    assert "vacancy_title" in first
    assert "company_name" in first
    assert "chat_history" in first
    assert "cover_letter" in first
    assert "employer_messages" in first
    assert len(first["chat_history"]) >= 2


def test_ai_assistant_analyze_chat_rejection(analyzer):
    res = analyzer.ai_assistant.analyze_chat_rejection(
        vacancy_title="AppSec Engineer",
        company_name="Security Corp",
        vacancy_description="Требуется опыт работы с Docker, Kubernetes, SAST, DAST, CI/CD",
        chat_history=[
            {"sender": "Соискатель", "text": "Добрый день! Хочу работать в ИБ."},
            {"sender": "Работодатель", "text": "К сожалению, мы ищем кандидата со стажем в Kubernetes и SAST."}
        ],
        cover_letter="Добрый день! Хочу работать в ИБ.",
        employer_messages=["К сожалению, мы ищем кандидата со стажем в Kubernetes и SAST."]
    )
    assert "rejection_root_cause" in res
    assert "cover_letter_critique" in res
    assert "improved_cover_letter" in res
    assert "missing_skills" in res
    assert "about_me_recommendation" in res
    assert "Kubernetes" in " ".join(res["missing_skills"]) or "kubernetes" in " ".join(res["missing_skills"]) or len(res["missing_skills"]) > 0


def test_analyzer_run_chat_analysis_mock(analyzer, tmp_path):
    # Выдуманные образцы переписки больше не подставляются в боевом режиме:
    # пользователь получал «разбор» вакансий, по которым отказа не было.
    # Тесту они нужны, поэтому разрешаем их явным флагом.
    analyzer.config['allow_sample_chats'] = True
    # ИИ в фикстуре выключен, а шаблонный «разбор» больше не засчитывается.
    # Подставляем ответ, как от работающего ИИ.
    analyzer.ai_assistant.analyze_chat_rejection = lambda **kw: {
        "vacancy_title": kw.get("vacancy_title", ""),
        "company_name": kw.get("company_name", ""),
        "rejection_root_cause": "Нужен опыт с Kubernetes",
        "cover_letter_critique": "Не упомянут Kubernetes",
        "improved_cover_letter": "Здравствуйте!",
        "missing_skills": ["Kubernetes"],
        "about_me_recommendation": "",
        "experience_advice": "",
        "actionable_takeaway": "",
    }
    summary = analyzer.run_chat_analysis(limit=2, use_mock_if_empty=True, fetch_live=False, auto_apply=False)
    assert summary["total_analyzed"] > 0
    assert "detailed_analyses" in summary
    assert "top_missing_skills" in summary

    # Проверяем отчеты
    md_path = str(tmp_path / "chat_report.md")
    html_path = str(tmp_path / "chat_report.html")
    analyzer._generate_chat_markdown_report(summary, filepath=md_path)
    analyzer._generate_chat_html_report(summary, filepath=html_path)

    assert os.path.exists(md_path)
    assert os.path.exists(html_path)

    with open(md_path, 'r', encoding='utf-8') as f:
        md = f.read()
        assert "Глубокий разбор чатов и отказов" in md
        assert "Рекомендуемое улучшенное сопроводительное письмо" in md

    with open(html_path, 'r', encoding='utf-8') as f:
        html = f.read()
        assert "<!DOCTYPE html>" in html
        assert "Разбор чатов и отказов HeadHunter" in html


def test_template_analysis_is_deferred_not_recorded(analyzer, capsys):
    """Без ИИ отказ не «разбирается» шаблоном, а откладывается.

    Раньше шаблонная причина («без письма при высокой конкуренции») выводилась
    как настоящая и писалась в базу — после чего отказ считался разобранным и
    к ИИ больше не попадал.
    """
    analyzer.config['allow_sample_chats'] = True
    recorded = []
    if getattr(analyzer, 'db', None):
        analyzer.db.record_rejection_analysis = lambda **kw: recorded.append(kw)
    # ИИ выключен в фикстуре: analyze_chat_rejection отдаёт шаблон с меткой.
    summary = analyzer.run_chat_analysis(limit=2, use_mock_if_empty=True,
                                         fetch_live=False, auto_apply=False)
    assert summary["total_analyzed"] == 0
    assert summary['total_deferred'] == 2
    assert summary['status'] == 'deferred'
    assert '[OK] РАЗБОР ПЕРЕПИСКИ ЗАВЕРШЁН' not in capsys.readouterr().out
    assert recorded == []


def test_chat_reply_system_prompt_is_a_string():
    """Подсказка для ответа в чат уходит в ИИ строкой, а не кортежем.

    23.09 лишняя запятая в конце строки превратила подсказку в кортеж: Groq
    отвечал 400 («content must be a string»), и бот не ответил работодателям
    в шести диалогах.
    """
    import rejection_analyzer as r
    seen = {}

    class StubAI:
        enabled = True
        candidate_profile = {'name': 'Иван', 'skills': ['Python']}

        def _call_llm(self, prompt, system_prompt=None, validate=None):
            seen['system_prompt'] = system_prompt
            return 'Здравствуйте! Готов обсудить.'

    analyzer = r.RejectionAnalyzer.__new__(r.RejectionAnalyzer)
    analyzer.ai_assistant = StubAI()
    analyzer.compose_chat_reply('Какой у вас опыт с SIEM?', 'Аналитик SOC', 'Тест')
    assert isinstance(seen.get('system_prompt'), str)
    assert '\\n' not in seen['system_prompt']  # буквальный «\n» — след той же порчи



def test_browser_failure_is_not_reported_as_no_new_rejections(analyzer, capsys):
    """24.09: браузер не открылся, а бот написал «новых отказов нет» — hh.ru не проверяли."""
    analyzer._init_driver = lambda: False
    analyzer.is_driver_alive = lambda: False
    analyzer._load_cached_chats = lambda limit=0: [{'vacancy_title': 'AppSec', 'company_name': 'X'}]
    analyzer.seen_rejection_keys = lambda: {'appsec_x'}
    res = analyzer.run_chat_analysis(limit=0, use_mock_if_empty=False, fetch_live=True)
    out = capsys.readouterr().out
    assert res == {'status': 'browser_failed'}
    assert 'не проверены' in out
    assert 'новых отказов нет' not in out

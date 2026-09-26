"""Автоправка резюме после разбора отказов: без вопроса, но только правдой из профиля.

Разбор отказов называет навыки, которых НЕ хватило под вакансию, а ИИ ещё и
сочинял для «О себе» опыт, которого у кандидата нет (Terraform, Basel III).
Эти тесты держат границу: без вопроса пользователю в резюме уходят только
навыки, подтверждённые candidate_profile, а «О себе» и уровни не трогаются.
Браузер нигде не запускается — всё на заглушках.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from rejection_analyzer import RejectionAnalyzer, auto_apply_resume_enabled
from resume_updater import HHResumeUpdater, profile_grounded_skills

PROFILE = {
    "specialization": "Backend-разработчик",
    "skills": ["CI / CD", "Django", "Kubernetes", "Linux"],
    "about": "Пишу бэкенд и API.",
    "certificates": ["Google Cloud Certificate (Google, 2026)"],
    "experience_highlights": [
        {"company": "X", "position": "Backend-разработчик",
         "what": "Проектировал REST и GraphQL API, писал документацию."},
    ],
}


def test_grounding_splits_profile_skills_from_advice():
    grounded, advice = profile_grounded_skills(
        ["CI/CD", "GraphQL", "django", "Go", "Terraform", "Kafka", "Arch Linux", "K8s"],
        PROFILE)
    # CI/CD — то же, что «CI / CD», GraphQL — из текста опыта, регистр не важен,
    # K8s — синоним Kubernetes.
    assert grounded == ["CI/CD", "GraphQL", "django", "K8s"]
    # «Go» не засчитывается за счёт «Google», «Arch Linux» — за счёт «Linux».
    assert advice == ["Go", "Terraform", "Kafka", "Arch Linux"]


def test_grounding_with_empty_profile_adds_nothing():
    assert profile_grounded_skills(["Python"], {}) == ([], ["Python"])
    assert profile_grounded_skills(["Python"], None) == ([], ["Python"])


def _updater():
    u = HHResumeUpdater(headless=True)
    u.add_skills_to_resume = MagicMock(return_value=(True, "Добавлено навыков: 2", ["CI/CD", "GraphQL"]))
    u.update_about_section = MagicMock()
    u.sync_adaptive_skills = MagicMock()
    u.activate_and_save_all_skills = MagicMock()
    return u


def test_profile_only_writes_only_grounded_skills():
    u = _updater()
    res = u.apply_full_modernization(["CI/CD", "GraphQL", "Terraform"], profile_only=True, profile=PROFILE)

    u.add_skills_to_resume.assert_called_once_with(["CI/CD", "GraphQL"])
    # «О себе», уровни и база адаптивных навыков в этом режиме не трогаются.
    u.update_about_section.assert_not_called()
    u.activate_and_save_all_skills.assert_not_called()
    u.sync_adaptive_skills.assert_not_called()
    assert res["skills_added"] == ["CI/CD", "GraphQL"]
    assert res["skills_added_count"] == 2
    assert res["recommended_only"] == ["Terraform"]
    assert res["about_changed"] is False


def test_profile_only_without_grounded_skills_does_not_open_editor():
    u = _updater()
    res = u.apply_full_modernization(["Terraform", "Basel III"], profile_only=True, profile=PROFILE)
    # Пустой список не должен проваливаться в базу адаптивных навыков.
    u.add_skills_to_resume.assert_not_called()
    u.sync_adaptive_skills.assert_not_called()
    assert res["skills_added_count"] == 0
    assert res["recommended_only"] == ["Terraform", "Basel III"]


def test_auto_apply_resume_setting():
    assert auto_apply_resume_enabled({}, []) is True                      # по умолчанию включено
    assert auto_apply_resume_enabled({"auto_apply_resume": False}, []) is False
    assert auto_apply_resume_enabled({"auto_apply_resume": False}, ["--auto-apply"]) is True
    assert auto_apply_resume_enabled(None, ["x", "--apply"]) is True


@pytest.fixture
def analyzer(tmp_path):
    config_file = str(tmp_path / "hh_selenium_config.json")
    with open(config_file, "w", encoding="utf-8") as f:
        json.dump({"ai_config": {"enabled": False}, "candidate_profile": PROFILE}, f)
    return RejectionAnalyzer(config_file=config_file, headless=True)


def test_auto_modernize_asks_nothing_and_passes_only_grounded(analyzer, capsys):
    fake = MagicMock()
    fake.apply_full_modernization.return_value = {
        "skills_added_count": 1, "skills_added": ["GraphQL"],
        "skills_message": "Добавлено навыков: 1", "about_message": "не менялся",
        "about_changed": False,
    }
    with patch("resume_updater.HHResumeUpdater", return_value=fake), \
         patch("config_manager.get_active_resume", return_value=("rid", "t")), \
         patch("builtins.input", side_effect=AssertionError("вопрос задавать нельзя")):
        analyzer._modernize_resume(["GraphQL", "Terraform"], auto=True)

    fake.apply_full_modernization.assert_called_once_with(["GraphQL"], profile_only=True, profile=PROFILE)
    out = capsys.readouterr().out
    assert "внесено в резюме — GraphQL" in out
    assert "только рекомендация — Terraform" in out


def test_auto_modernize_skips_browser_when_nothing_grounded(analyzer):
    with patch("resume_updater.HHResumeUpdater") as cls:
        res = analyzer._modernize_resume(["Terraform"], auto=True)
    cls.assert_not_called()
    assert res["recommended_only"] == ["Terraform"]


def test_profile_from_resume_page():
    """25.09: новый пользователь получал выдуманный профиль из примера настроек."""
    from resume_updater import merge_resume_into_profile, years_from_experience_title
    assert years_from_experience_title('Опыт работы: 6\xa0лет 9\xa0месяцев') == 6.75
    assert years_from_experience_title('Опыт работы: 1 год') == 1
    page = {'title': 'Python-разработчик', 'skills': ['Python', 'Django', 'Python'],
            'jobs': [['Ромашка', '2 года', 'Backend', 'Март 2023 — сейчас', 'Пишу API', 'на Django']],
            'experience_title': 'Опыт работы: 2 года', 'about': 'Люблю бэкенд',
            'salary': 'Уровень дохода не\xa0указан', 'work_formats': 'Формат работы: Удалённо',
            'phone': '+7 900 000-00-00', 'email': '', 'education': ['МГУ, 2019'],
            'certificates': 'Добавить'}
    old = {'name': 'Иван', 'contacts': {'telegram': '@username', 'github': ''}}
    p = merge_resume_into_profile(old, page)
    assert p['specialization'] == 'Python-разработчик' and p['skills'] == ['Python', 'Django']
    assert p['experience_highlights'][0] == {'company': 'Ромашка', 'position': 'Backend',
                                             'period': 'Март 2023 — сейчас', 'what': 'Пишу API на Django'}
    assert p['experience_years'] == 2 and p['remote_preferred'] is True and p['name'] == 'Иван'
    assert 'expected_salary' not in p and 'certificates' not in p
    assert p['contacts'] == {'phone': '+7 900 000-00-00'}   # заглушки и пустые убраны

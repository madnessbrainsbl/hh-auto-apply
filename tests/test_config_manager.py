import os
import sys
import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config_manager
from config_manager import (
    load_config,
    save_config,
    get_active_resume,
    set_active_resume,
    get_active_preset,
    set_active_preset,
    SEARCH_PRESETS,
    DEFAULT_RESUME_ID,
    DEFAULT_RESUME_TITLE
)


def test_load_and_save_config(tmp_path):
    temp_config_path = str(tmp_path / "test_config.json")
    with patch('config_manager.CONFIG_FILE', temp_config_path):
        cfg = load_config()
        assert cfg is not None
        assert "resume_id" in cfg
        assert "resume_title" in cfg
        # 25.09: у нового пользователя направление не выбрано (раньше молча ИБ).
        assert cfg.get("search_preset", "security") in SEARCH_PRESETS
        assert not cfg.get("candidate_profile"), "в примере не должно быть чужого профиля"

        cfg["resume_title"] = "Test Senior Python Developer"
        save_config(cfg)

        cfg_reloaded = load_config()
        assert cfg_reloaded["resume_title"] == "Test Senior Python Developer"


def test_behavior_menu_does_not_offer_salary_override(monkeypatch, capsys):
    cfg = {'chat_autoreply': {'salary_answer': 'От 350 000 руб.'}}
    monkeypatch.setattr(config_manager, 'load_config', lambda: cfg)
    monkeypatch.setattr(config_manager, 'get_active_preset', lambda: {'id': 'custom'})
    monkeypatch.setattr('builtins.input', lambda _: '0')
    save = MagicMock()
    monkeypatch.setattr(config_manager, 'save_config', save)

    config_manager.edit_bot_behavior()
    output = capsys.readouterr().out
    assert '350 000' not in output
    assert 'Ответ на прямой вопрос о зарплате в чате' not in output
    assert cfg['chat_autoreply']['salary_answer'] == 'От 350 000 руб.'
    save.assert_not_called()


def test_get_and_set_active_resume(tmp_path):
    temp_config_path = str(tmp_path / "test_config.json")
    with patch('config_manager.CONFIG_FILE', temp_config_path):
        rid, title = get_active_resume()
        assert rid is not None
        assert title is not None

        new_id = "12345678901234567890123456789012345678"
        new_title = "Lead DevOps Engineer"
        set_active_resume(new_id, new_title)

        rid_after, title_after = get_active_resume()
        assert rid_after == new_id
        assert title_after == new_title


def test_presets_switching(tmp_path):
    temp_config_path = str(tmp_path / "test_config.json")
    temp_cache_path = str(tmp_path / "test_vacancies_cache.json")
    with patch('config_manager.CONFIG_FILE', temp_config_path), \
         patch('config_manager.VACANCIES_CACHE_FILE', temp_cache_path):
        # 1. Switch to python
        set_active_preset('python')
        p = get_active_preset()
        assert p['id'] == 'python'
        assert any('python' in q.lower() for q in p['queries'])

        # 2. Switch to devops
        set_active_preset('devops')
        p = get_active_preset()
        assert p['id'] == 'devops'
        assert any('kubernetes' in k.lower() or 'devops' in k.lower() for k in p['keywords_include'])

        # 3. Switch to custom query
        set_active_preset('custom', custom_query="Frontend React")
        p = get_active_preset()
        assert p['id'] == 'custom'
        assert "React" in p['name']
        assert any("react" in q.lower() for q in p['queries'])


def test_validate_apply_title_with_presets(tmp_path):
    temp_config_path = str(tmp_path / "test_config.json")
    temp_cache_path = str(tmp_path / "test_vacancies_cache.json")
    from test import validate_apply_title

    with patch('config_manager.CONFIG_FILE', temp_config_path), \
         patch('config_manager.VACANCIES_CACHE_FILE', temp_cache_path):
        # When preset is security:
        set_active_preset('security')
        ok_sec, _ = validate_apply_title("Инженер по безопасности приложений (AppSec)")
        assert ok_sec is True

        # When preset is python:
        set_active_preset('python')
        ok_py, _ = validate_apply_title("Senior Python Backend Developer (FastAPI)")
        assert ok_py is True

        ok_bad, reason = validate_apply_title("Менеджер по прямым продажам")
        assert ok_bad is False
        assert "Исключено" in reason

        # When preset is devops:
        set_active_preset('devops')
        ok_devops, _ = validate_apply_title("DevOps / SRE инженер Kubernetes")
        assert ok_devops is True

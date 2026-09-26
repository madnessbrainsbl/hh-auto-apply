import importlib.util
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "test.py"


def load_hh_module():
    spec = importlib.util.spec_from_file_location("hh_auto_apply_module", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_build_user_agent_without_contact_uses_app_name():
    module = load_hh_module()

    assert module.build_user_agent(None) == "AutoJobApplyBot/1.0"


def test_build_user_agent_rejects_hh_contact_domain():
    module = load_hh_module()

    with pytest.raises(ValueError):
        module.build_user_agent("api@hh.ru")


def test_build_hh_headers_sets_public_and_hh_user_agents():
    module = load_hh_module()

    headers = module.build_hh_headers("AutoJobApplyBot/1.0 (owner@mail.ru)", "token-value")

    assert headers["User-Agent"] == "AutoJobApplyBot/1.0 (owner@mail.ru)"
    assert headers["HH-User-Agent"] == "AutoJobApplyBot/1.0 (owner@mail.ru)"
    assert headers["Accept"] == "application/json"
    assert headers["Authorization"] == "Bearer token-value"


def test_build_client_credentials_payload_uses_hh_grant_type():
    module = load_hh_module()

    payload = module.build_client_credentials_payload("client-id", "client-secret")

    assert payload == {
        "grant_type": "client_credentials",
        "client_id": "client-id",
        "client_secret": "client-secret",
    }


def test_get_vacancy_apply_url_prefers_apply_link():
    module = load_hh_module()

    url = module.get_vacancy_apply_url({
        "apply_alternate_url": "https://hh.ru/applicant/vacancy_response?vacancyId=123",
        "alternate_url": "https://hh.ru/vacancy/123",
    })

    assert url == "https://hh.ru/applicant/vacancy_response?vacancyId=123"


def test_get_vacancy_apply_url_rejects_non_hh_url():
    module = load_hh_module()

    url = module.get_vacancy_apply_url({
        "apply_alternate_url": "https://evil.example/apply",
        "alternate_url": "http://hh.ru/vacancy/123",
    })

    assert url is None

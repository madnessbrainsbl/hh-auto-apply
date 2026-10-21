import copy
from unittest.mock import Mock

import pytest
import requests

from ai_assistant import AIAssistant


def make_ai():
    ai = AIAssistant({'ai_config': {'enabled': False}, 'captcha': {'enabled': True},
                      'candidate_profile': {'contacts': {'email': 'private@example.test'}}})
    ai.ai_config['openai_compatible'] = [
        {'name': 'First', 'base_url': 'https://first.example/v1', 'api_key': 'first-private',
         'models': ['gemini-new-flash']},
        {'name': 'Second', 'base_url': 'https://second.example/v1', 'api_key': 'second-private',
         'models': ['vision-free:free']}]
    return ai


def reply(body, status=200):
    result = Mock(status_code=status)
    result.json.return_value = body
    if status >= 400:
        result.raise_for_status.side_effect = requests.HTTPError('private-error', response=result)
    return result


def install_catalogs(monkeypatch):
    def get(url, **kwargs):
        model = 'gemini-new-flash' if 'first.' in url else 'vision-free:free'
        return reply({'data': [{'id': model, 'input_modalities': ['text', 'image']}]})
    monkeypatch.setattr(requests, 'get', get)


def test_default_vision_uses_existing_compatible_provider_not_invalid_gemini(monkeypatch):
    ai = make_ai()
    install_catalogs(monkeypatch)
    post = Mock(return_value=reply({'choices': [{'message': {'content': 'Ab123'}}]}))
    monkeypatch.setattr(requests, 'post', post)
    before = copy.deepcopy(ai.config)
    assert ai.recognize_captcha(b'cropped-image') == 'Ab123'
    assert post.call_args.args[0] == 'https://second.example/v1/chat/completions'
    assert post.call_args.kwargs['headers']['Authorization'] == 'Bearer second-private'
    assert ai.last_captcha_source['provider'] == 'Second'
    assert ai.config == before


def test_failed_explicit_gemini_falls_back_and_credentials_do_not_cross_services(monkeypatch, caplog):
    ai = make_ai()
    ai.config['captcha'].update(provider='gemini', model='vision-test', api_key='invalid-google-private')
    install_catalogs(monkeypatch)
    calls = []
    def post(url, **kwargs):
        calls.append((url, kwargs))
        if 'googleapis.com' in url:
            return reply({'error': {'details': [{'reason': 'API_KEY_INVALID'}]}}, 400)
        assert kwargs['headers']['Authorization'] == 'Bearer second-private'
        return reply({'choices': [{'message': {'content': 'Ab123'}}]})
    monkeypatch.setattr(requests, 'post', post)
    assert ai.recognize_captcha(b'crop') == 'Ab123'
    assert len(calls) == 2
    assert 'API_KEY_INVALID' in caplog.text
    for secret in ('invalid-google-private', 'second-private', 'private-error'):
        assert secret not in caplog.text
    calls.clear()
    assert ai.recognize_captcha(b'new-crop') == 'Ab123'
    assert len(calls) == 1 and 'googleapis.com' not in calls[0][0]


def test_text_only_model_is_not_sent_an_image_and_disabled_provider_is_not_called(monkeypatch):
    ai = make_ai()
    ai.ai_config['openai_compatible'][1]['enabled'] = False
    monkeypatch.setattr(requests, 'get', Mock(return_value=reply({'data': [
        {'id': 'gemini-new-flash', 'input_modalities': ['text']}]})))
    ai.api_key = ''
    post = Mock()
    monkeypatch.setattr(requests, 'post', post)
    assert ai.recognize_captcha(b'crop') is None
    post.assert_not_called()


def test_rejected_model_yields_to_next_service_and_is_resting(monkeypatch):
    ai = make_ai()
    install_catalogs(monkeypatch)
    calls = []
    def post(url, **kwargs):
        calls.append(url)
        if 'second.' in url:
            return reply({'error': {'message': 'private-image-error'}}, 400)
        return reply({'choices': [{'message': {'content': 'Ab123'}}]})
    monkeypatch.setattr(requests, 'post', post)
    assert ai.recognize_captcha(b'crop') == 'Ab123'
    assert len(calls) == 2
    calls.clear()
    assert ai.recognize_captcha(b'new-crop') == 'Ab123'
    assert len(calls) == 1 and 'first.' in calls[0]


@pytest.mark.parametrize('content', [None, [], {'text': 'Ab123'}, 'Here is your result: Ab123'])
def test_invalid_answer_never_becomes_captcha_text(monkeypatch, content):
    ai = make_ai()
    ai.api_key = ''
    install_catalogs(monkeypatch)
    monkeypatch.setattr(requests, 'post', Mock(return_value=reply({'choices': [{'message': {'content': content}}]})))
    assert ai.recognize_captcha(b'crop') is None


def test_total_budget_stops_attempts_and_keeps_payload_private(monkeypatch):
    ai = make_ai()
    ai.config['captcha']['total_timeout_seconds'] = 5
    clock = [100.0]
    monkeypatch.setattr('vision_provider.time.monotonic', lambda: clock[0])
    install_catalogs(monkeypatch)
    def post(url, **kwargs):
        assert kwargs['timeout'] == 5
        assert 'private@example.test' not in str(kwargs)
        assert 'data:image/png;base64,Y3JvcA==' in str(kwargs)
        clock[0] += kwargs['timeout']
        raise requests.Timeout('private-error')
    call = Mock(side_effect=post)
    monkeypatch.setattr(requests, 'post', call)
    assert ai.recognize_captcha(b'crop') is None
    call.assert_called_once()


def test_catalog_refreshes_and_selects_new_available_vision_model(monkeypatch):
    ai = make_ai()
    ai.ai_config['openai_compatible'] = [ai.ai_config['openai_compatible'][1]]
    clock = [1_000_000.0]
    monkeypatch.setattr('vision_provider.time.time', lambda: clock[0])
    get = Mock(side_effect=[reply({'data': [{'id': 'old:free', 'supports_vision': True}]}),
                            reply({'data': [{'id': 'new:free', 'supports_vision': True}]})])
    monkeypatch.setattr(requests, 'get', get)
    post = Mock(return_value=reply({'choices': [{'message': {'content': 'Ab123'}}]}))
    monkeypatch.setattr(requests, 'post', post)
    assert ai.recognize_captcha(b'crop') == 'Ab123'
    assert ai.last_captcha_source['model'] == 'old:free'
    clock[0] += 601
    assert ai.recognize_captcha(b'new-crop') == 'Ab123'
    assert ai.last_captcha_source['model'] == 'new:free'
    assert get.call_count == 2


def test_hh_rejection_changes_the_next_vision_route_without_leaking_answer(monkeypatch):
    ai = make_ai()
    install_catalogs(monkeypatch)
    post = Mock(return_value=reply({'choices': [{'message': {'content': 'Ab123'}}]}))
    monkeypatch.setattr(requests, 'post', post)
    assert ai.recognize_captcha(b'crop') == 'Ab123'
    assert 'second.' in post.call_args.args[0]
    ai.report_captcha_result('server_rejected')
    assert ai.recognize_captcha(b'new-crop') == 'Ab123'
    assert 'first.' in post.call_args.args[0]
    assert 'Ab123' not in str(ai._load_stats())
    assert 'second-private' not in str(ai._load_stats())


def test_disabled_vision_does_not_discover_or_call_any_provider(monkeypatch):
    ai = make_ai()
    ai.config['captcha']['enabled'] = False
    get, post = Mock(), Mock()
    monkeypatch.setattr(requests, 'get', get)
    monkeypatch.setattr(requests, 'post', post)
    assert ai.recognize_captcha(b'crop') is None
    get.assert_not_called()
    post.assert_not_called()


def test_feedback_persistence_error_does_not_override_hh_result(monkeypatch, caplog):
    ai = make_ai()
    monkeypatch.setattr('vision_provider.VisionRouter.feedback', Mock(side_effect=OSError('private-key')))
    ai.report_captcha_result('accepted')
    assert 'OSError' in caplog.text and 'private-key' not in caplog.text


@pytest.mark.parametrize('failure', [requests.ConnectionError, requests.Timeout])
def test_offline_service_is_not_retried_for_every_model(monkeypatch, failure):
    ai = make_ai()
    def get(url, **kwargs):
        return reply({'data': [{'id': mid, 'supports_vision': True} for mid in (
            ['one:free', 'two:free', 'three:free'] if 'second.' in url else ['gemini-new-flash'])]})
    monkeypatch.setattr(requests, 'get', get)
    calls = []
    def post(url, **kwargs):
        calls.append(url)
        if 'second.' in url:
            raise failure('private-error')
        return reply({'choices': [{'message': {'content': 'Ab123'}}]})
    monkeypatch.setattr(requests, 'post', post)
    assert ai.recognize_captcha(b'crop') == 'Ab123'
    assert len(calls) == 2 and 'first.' in calls[-1]


def test_menu_vision_check_is_explicit_and_does_not_save_configuration(monkeypatch):
    import test as menu
    import vision_provider
    config = {'ai_config': {}}
    monkeypatch.setattr(menu, 'load_config', lambda: config)
    monkeypatch.setattr('ai_assistant.describe_ai_chain', lambda _: [])
    monkeypatch.setattr('builtins.input', lambda _: 'V')
    check = Mock(return_value=True)
    save = Mock()
    monkeypatch.setattr(vision_provider, 'check_vision_provider', check)
    monkeypatch.setattr(menu, 'save_config', save)
    menu.choose_ai_provider()
    check.assert_called_once_with(config)
    save.assert_not_called()

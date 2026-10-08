import subprocess
from unittest.mock import Mock

from ai_assistant import AIAssistant


REAL_FIND_CLI = AIAssistant.find_cli


def test_codex_bundled_install_is_found_without_path(tmp_path, monkeypatch):
    local = tmp_path / 'local'
    executable = local / 'OpenAI' / 'Codex' / 'bin' / 'synthetic-build' / 'codex.exe'
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b'synthetic executable fixture')
    monkeypatch.setenv('LOCALAPPDATA', str(local))
    monkeypatch.setattr('shutil.which', lambda _name: None)

    assert REAL_FIND_CLI('codex') == str(executable)


def test_codex_path_executable_has_priority(monkeypatch):
    monkeypatch.setattr('shutil.which', lambda _name: 'synthetic-path-codex')
    assert REAL_FIND_CLI('codex') == 'synthetic-path-codex'


def test_missing_codex_does_not_invent_executable(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    monkeypatch.setattr('shutil.which', lambda _name: None)
    assert REAL_FIND_CLI('codex') is None


def test_cli_weekly_quota_is_reported_even_with_stderr_warning(tmp_path, monkeypatch, caplog):
    assistant = AIAssistant.__new__(AIAssistant)
    assistant.ai_config = {'cli_providers': ['claude']}
    assistant.find_cli = lambda _name: 'synthetic-cli'
    result = subprocess.CompletedProcess(
        [], 1, b"You've hit your weekly limit", b'synthetic startup warning')
    monkeypatch.setattr('subprocess.run', Mock(return_value=result))

    assert assistant._call_cli_providers('synthetic prompt', None) is None
    assert 'недельная квота' in caplog.text
    assert 'claude' in assistant._cli_dead


def test_gemini_project_denial_is_not_reported_as_missing_key(caplog):
    assistant = AIAssistant.__new__(AIAssistant)
    assistant.enabled = True
    assistant._gemini_client = Mock()
    assistant._gemini_client.generate_content.side_effect = RuntimeError(
        '403 PERMISSION_DENIED: Your project has been denied access. Please contact support.')
    assistant._openai_client = None
    assistant.ensure_model_resolved = lambda: None
    assistant.gemini_request_options = lambda: {}
    assistant._call_backup_provider = Mock(return_value=None)

    assert assistant._call_step('gemini', 'synthetic prompt', None) is None
    assert 'API-проекта' in caplog.text
    assert 'поддержк' in caplog.text

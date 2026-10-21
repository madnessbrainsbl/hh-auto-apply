import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from db_manager import DatabaseManager
from rejection_analyzer import RejectionAnalyzer


def test_standard_rejection_does_not_confirm_ai_reason(tmp_path):
    db = DatabaseManager(db_path=str(tmp_path / 'test.db'))
    db.record_rejection_analysis('1', 'Role', rejection_reason='Недостаточно опыта с Kubernetes',
                                 employer_messages=['К сожалению, сейчас не готовы пригласить вас.'])
    assert db.get_stats()['rejections_confirmed'] == 0


def test_explicitly_missing_reason_quote_cannot_be_replaced_by_standard_refusal(tmp_path):
    db = DatabaseManager(db_path=str(tmp_path / 'test.db'))
    text = 'К сожалению, сейчас не готовы пригласить вас.'
    db.record_rejection_analysis('1', 'Role', rejection_reason=text,
                                 employer_reason_quote='', employer_messages=[text])
    assert db.get_stats()['rejections_confirmed'] == 0
    assert db.get_recent_rejections()[0]['employer_reason_quote'] == ''


def test_legacy_grouping_checks_use_explicit_sources():
    from test_rejection_fixes import test_rejection_reasons_are_grouped, test_fix_plan_uses_real_rejections_only
    test_rejection_reasons_are_grouped()
    test_fix_plan_uses_real_rejections_only()


def test_verified_quote_persists_separately_from_ai_hypothesis(tmp_path):
    db = DatabaseManager(db_path=str(tmp_path / 'test.db'))
    quote = 'Нужен опыт с Kubernetes.'
    db.record_rejection_analysis('1', 'Role', rejection_reason='Вероятно, завышенный грейд',
                                 employer_reason_quote=quote, employer_messages=[quote])
    row = db.get_recent_rejections()[0]
    assert row['employer_reason_quote'] == quote
    assert row['rejection_reason'] == 'Вероятно, завышенный грейд'
    assert db.get_stats()['rejections_confirmed'] == 1


def test_legacy_evidence_flag_without_quote_is_not_proof(tmp_path):
    db = DatabaseManager(db_path=str(tmp_path / 'test.db'))
    db.record_rejection_analysis('1', 'Role', rejection_reason='ИИ предполагает другой стек')
    with db._get_connection() as conn:
        conn.execute('UPDATE rejections SET evidence_based=1')
    analyzer = RejectionAnalyzer.__new__(RejectionAnalyzer)
    analyzer.db = db
    plan = analyzer.build_resume_fix_plan()
    assert plan['evidence_based'] == 0
    assert db.get_stats()['rejections_confirmed'] == 0


@pytest.mark.parametrize('quote', ['', 'Нужен опыт с Kubernetes.'])
def test_reports_label_ai_cause_as_hypothesis_even_with_employer_messages(tmp_path, quote):
    analyzer = RejectionAnalyzer.__new__(RejectionAnalyzer)
    summary = {'detailed_analyses': [{'vacancy_title': 'Role', 'company_name': 'Test',
                'rejection_root_cause': 'ИИ предполагает другой стек',
                'employer_reason_quote': quote, 'employer_messages': ['К сожалению, отказ.']}]}
    md, html = tmp_path / 'report.md', tmp_path / 'report.html'
    analyzer._generate_chat_markdown_report(summary, filepath=str(md))
    analyzer._generate_chat_html_report(summary, filepath=str(html))
    for text in (md.read_text(encoding='utf-8'), html.read_text(encoding='utf-8')):
        assert 'Гипотеза ИИ' in text
        assert 'Причина отказа:</strong>' not in text
        assert 'Нужен опыт с Kubernetes.' not in text


def test_plan_does_not_treat_free_form_or_stale_flags_as_employer_words():
    analyzer = RejectionAnalyzer.__new__(RejectionAnalyzer)
    analyzer.db = SimpleNamespace(get_recent_rejections=lambda **kw: [
        {'title': 'Role', 'company': 'Test', 'rejection_reason': 'Нужно другое позиционирование',
         'evidence_based': True}])
    assert analyzer.build_resume_fix_plan()['evidence_based'] == 0


@pytest.mark.parametrize('quote, messages, expected', [
    ('Нужен опыт с Kubernetes.', ['Нужен опыт с Kubernetes.'], 'Нужен опыт с Kubernetes.'),
    ('Нужен опыт с Kubernetes.', ['К сожалению, сейчас не готовы пригласить вас.'], ''),
    ('   Нужен   опыт с Kubernetes. ', ['Нужен опыт с Kubernetes.'], 'Нужен   опыт с Kubernetes.'),
    (None, ['Нужен опыт с Kubernetes.'], ''),
])
def test_ai_quote_must_come_from_employer_not_candidate(quote, messages, expected):
    from ai_assistant import AIAssistant
    ai = AIAssistant.__new__(AIAssistant)
    ai.enabled = True
    ai.candidate_profile = {'skills': []}
    ai._call_llm = Mock(return_value=json.dumps({'rejection_root_cause': 'Возможно, не тот стек',
                         'employer_reason_quote': quote, 'improved_cover_letter': 'Готов обсудить задачи.'}, ensure_ascii=False))
    result = ai.analyze_chat_rejection('Role', 'Test', '', cover_letter='Нужен опыт с Kubernetes.',
                                       employer_messages=messages)
    assert result['employer_reason_quote'] == expected
    assert result['employer_messages'] == messages


def test_legacy_database_adds_quote_column_without_rewriting_history(tmp_path):
    path = str(tmp_path / 'legacy.db')
    db = DatabaseManager(db_path=path)
    db.record_rejection_analysis('1', 'Role', rejection_reason='Старая гипотеза', ats_score=70)
    with sqlite3.connect(path) as conn:
        conn.execute('ALTER TABLE rejections DROP COLUMN employer_reason_quote')
        conn.execute('UPDATE rejections SET evidence_based=1')
        before = conn.execute('SELECT * FROM rejections').fetchall()
    migrated = DatabaseManager(db_path=path)
    DatabaseManager(db_path=path)
    with sqlite3.connect(path) as conn:
        after = conn.execute('SELECT * FROM rejections').fetchall()
    assert [row[:-1] for row in after] == before
    assert after[0][-1] == ''
    assert migrated.get_stats()['rejections_confirmed'] == 0


def test_fresh_quote_categories_use_employer_words_not_ai_cause():
    analyzer = RejectionAnalyzer.__new__(RejectionAnalyzer)
    analyzer.db = None
    quote = 'Бюджет позиции ниже ваших ожиданий по зарплате.'
    plan = analyzer.build_resume_fix_plan([{'vacancy_title': 'Role', 'company_name': 'Test',
        'rejection_root_cause': 'Вероятно, не тот стек', 'employer_reason_quote': quote,
        'employer_messages': [quote]}])
    assert plan['evidence_based'] == 1
    assert plan['categories'][0]['key'] == 'salary'
    assert plan['categories'][0]['example'] == quote

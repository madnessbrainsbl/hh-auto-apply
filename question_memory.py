"""Profile-scoped questionnaire drafts and answers explicitly approved by the user."""
import hashlib
import json
import os
import re
import unicodedata


def normalize(text):
    return ' '.join(unicodedata.normalize('NFKC', str(text)).casefold().replace('ё', 'е').split())


def question_signature(question, kind='text', options=()):
    if kind not in ('text', 'single', 'multi') or not isinstance(question, str):
        raise ValueError('Неизвестный тип вопроса')
    labels = [normalize(option) for option in options]
    if (len(question) > 10000 or len(labels) > 100 or any(not label or len(label) > 2000 for label in labels)
            or len(set(labels)) != len(labels) or (kind == 'text') != (not labels)):
        raise ValueError('Не удалось однозначно определить варианты ответа')
    text = normalize(question)
    if labels:
        suffix = r'\s*'.join(re.escape(label) for label in labels)
        text = re.sub(suffix + r'$', '', text).rstrip()
    if len(text) < 8 or text in ('ваш ответ', 'ваш вариант', 'укажите ответ', 'your answer'):
        raise ValueError('Недостаточно контекста вопроса')
    payload = [text, kind, sorted(labels)]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


def reusable_question(question):
    from ai_assistant import is_restricted_question
    return not is_restricted_question(question) and not re.search(
        r'\b(?:у нас|наш\w* (?:компан|команд)|эт\w* ваканс|данн\w* ваканс|'
        r'почему.{0,50}(?:компан|ваканс|работать|выбрали)|why.{0,50}(?:company|join|role)|'
        r'(?:this|our) (?:company|team|role)|персональн\w* данн|privacy|'
        r'(?:описанн|указанн|предложенн)\w* услов|условия выше)',
        normalize(question))


def profile_scope(config):
    from app_paths import PROFILE_ID
    context = [PROFILE_ID, os.environ.get('HH_RESUME_ID') or config.get('resume_id'),
               config.get('candidate_profile'), config.get('question_answers'), config.get('answer_policy')]
    return hashlib.sha256(json.dumps(context, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class QuestionMemory:
    def __init__(self, db, config):
        self.db, self.config = db, config
        self.scope = profile_scope(config)
        with db._get_connection() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS questionnaire_answers (
                scope TEXT NOT NULL, signature TEXT NOT NULL, payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', source_url TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (scope, signature))''')

    def _validate(self, question, kind, options, answers):
        from ai_assistant import clean_public_text, service_message_problem
        signature = question_signature(question, kind, options)
        if not reusable_question(question):
            raise ValueError('Вопрос зависит от работодателя или ограничен политикой профиля')
        if (not isinstance(answers, list) or not answers or (kind != 'multi' and len(answers) != 1)
                or len({normalize(a) for a in answers}) != len(answers)):
            raise ValueError('Нужно указать допустимый ответ')
        for answer in answers:
            if not isinstance(answer, str) or not answer.strip() or len(answer) > 10000:
                raise ValueError('Ответ пустой или слишком длинный')
            if (service_message_problem(answer) or re.search(
                    r'как (?:ии|ai)[ -]ассистент|как языковая модель', normalize(answer)) or clean_public_text(
                    answer, self.config.get('candidate_profile') or {}) != answer.strip()):
                raise ValueError('Ответ не прошёл действующие проверки текста')
        if kind != 'text' and any(normalize(a) not in {normalize(o) for o in options} for a in answers):
            raise ValueError('Ответ отсутствует среди вариантов')
        return signature

    def stage(self, question, kind, options, answers, source_url=''):
        signature = self._validate(question, kind, options, answers)
        payload = json.dumps(dict(question=question, kind=kind, options=list(options), answers=answers),
                             ensure_ascii=False)
        with self.db._get_connection() as conn:
            conn.execute('''INSERT INTO questionnaire_answers (scope, signature, payload, source_url)
                VALUES (?, ?, ?, ?) ON CONFLICT(scope, signature) DO UPDATE SET
                payload=excluded.payload, source_url=excluded.source_url, updated_at=CURRENT_TIMESTAMP
                WHERE questionnaire_answers.status='pending' ''',
                         (self.scope, signature, payload, str(source_url)[:2000]))
        return signature

    def records(self):
        with self.db._get_connection() as conn:
            rows = conn.execute('''SELECT * FROM questionnaire_answers WHERE scope=? AND status!='rejected'
                ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, updated_at DESC, signature''',
                                (self.scope,)).fetchall()
        return [dict(row, **json.loads(row['payload'])) for row in rows]

    def find(self, question, kind='text', options=()):
        signature = question_signature(question, kind, options)
        with self.db._get_connection() as conn:
            row = conn.execute('''SELECT payload FROM questionnaire_answers
                WHERE scope=? AND signature=? AND status='confirmed' ''', (self.scope, signature)).fetchone()
        if row is None:
            return None
        saved = json.loads(row['payload'])
        if (self._validate(**saved) != signature
                or self._validate(question, kind, options, saved['answers']) != signature):
            return None
        return saved['answers']

    def confirm(self, signature, answers=None):
        with self.db._get_connection() as conn:
            row = conn.execute("SELECT payload FROM questionnaire_answers WHERE scope=? AND signature=? AND status!='rejected'",
                               (self.scope, signature)).fetchone()
            if row is None:
                raise ValueError('Запись больше не существует')
            payload = json.loads(row['payload'])
            if answers is not None:
                payload['answers'] = answers
            if self._validate(**payload) != signature:
                raise ValueError('Данные вопроса изменились')
            conn.execute('''UPDATE questionnaire_answers SET status='confirmed', payload=?,
                updated_at=CURRENT_TIMESTAMP WHERE scope=? AND signature=?''',
                         (json.dumps(payload, ensure_ascii=False), self.scope, signature))

    def forget(self, signature):
        with self.db._get_connection() as conn:
            conn.execute('''UPDATE questionnaire_answers SET status='rejected', payload='{}', source_url='',
                updated_at=CURRENT_TIMESTAMP WHERE scope=? AND signature=?''', (self.scope, signature))

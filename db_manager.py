import os
import sys
import json
import sqlite3
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

logger = logging.getLogger('db_manager')

from app_paths import CODE_DIR, DATA_DIR, PROFILE_ID

SCRIPT_DIR = DATA_DIR
DEFAULT_DB_PATH = os.path.join(SCRIPT_DIR, 'hh_data.db')

if CODE_DIR not in sys.path:
    sys.path.insert(0, CODE_DIR)

from terminal_ui import explain_error  # noqa: E402  (нужен SCRIPT_DIR в sys.path)

# Единственный словарь перевода внутренних кодов статусов на человеческий язык.
# Используется везде, где статус попадает на экран пользователю (см. human_status).
# Ключи здесь — те же строки, что лежат в базе и в истории откликов, менять их нельзя.
STATUS_LABELS = {
    # исходы отклика
    'sent': 'отклик отправлен',
    'sent_wrong_filter': 'отклик отправлен (вакансия не по вашему профилю)',
    'invited': 'приглашение на собеседование',
    'discarded': 'отказ работодателя',
    'denied': 'работодатель не принимает отклики',
    'unknown': 'ответа пока нет',
    # причины пропуска вакансии
    'already_applied': 'уже откликались',
    'excluded_filter': 'не подошла по вашим настройкам поиска',
    'skipped_filter': 'отсеяна фильтрами поиска',
    'archived': 'вакансия в архиве',
    'ai_rejected': 'помощник счёл вакансию неподходящей',
    'skipped_test': 'работодатель требует тестовое задание',
    'skipped_no_safe_url': 'нет прямой ссылки на отклик',
    # ошибки при отправке отклика
    'already_applied_error': 'уже откликались ранее',
    'daily_limit_exceeded': 'достигнут дневной лимит откликов',
    'test_required': 'требуется тестовое задание',
    'application_denied': 'работодатель не принимает отклики',
    'forbidden': 'доступ к вакансии закрыт',
    'bad_request': 'откликнуться можно только на сайте работодателя',
    'network_error': 'нет связи с сайтом',
    'has_test=True': 'работодатель требует тестовое задание',
}


# Формулировки, которые бот пишет сам, когда разбирать нечего: fallback-ветка
# ai_assistant.analyze_rejection_ats и эвристика rejection_analyzer. Слов
# работодателя в них нет, поэтому в статистике это ОЦЕНКА БОТА, а не причина
# отказа. Копия набора rejection_analyzer.GENERIC_REASONS: импортировать оттуда
# нельзя — rejection_analyzer сам импортирует db_manager (циклический импорт).
# ponytail: дубль из 11 строк, а не общий модуль. Когда rejection_analyzer
# станет можно править, там ставится `from db_manager import GENERIC_REASONS`
# и копия исчезает.
GENERIC_REASONS = frozenset({
    'автоматический скрининг или отбор более опытного кандидата',
    'автоматический отсев по ключевым словам (ats) или несоответствие грейда',
    'высокая конкуренция среди откликов или ручной отсев рекрутером',
    'недостаточный стаж или несоответствие требуемому грейду',
    'несоответствие формату работы (требуется присутствие в офисе/регионе)',
    'отклик отправлен без сопроводительного письма при высокой конкуренции',
    'недостаточное совпадение по ключевым словам и профилю стека',
    'отказ работодателя по результатам рассмотрения',
    'отказ работодателя',
    'отказ',
    # пишется, когда чат открыли, но реплик работодателя в нём не нашлось
    'отказ в переписке',
})


# Коды, которые бот пишет сам и которые раньше протекали на экран латиницей.
STATUS_LABELS.setdefault('skipped_questions', 'не нашлось осмысленного ответа на вопрос работодателя')
STATUS_LABELS.setdefault('toxic_filter', 'отсеяна по описанию вакансии')
STATUS_LABELS.setdefault('server_error', 'сайт hh.ru ответил ошибкой')
STATUS_LABELS.setdefault('questions_unfilled', 'анкета работодателя осталась незаполненной')


def canonical_skill_name(skill) -> str:
    """Приводит навык к одному написанию перед записью в базу.

    Без этого один навык живёт несколькими строками: Kubernetes (42 отказа),
    kubernetes (12) и K8s (10) — это 64 отказа в трёх счётчиках.
    А get_adaptive_skills сортирует по счётчику, то есть порядок навыков,
    уходящих в резюме и в сопроводительные, отражал раздробленность, а не спрос.
    """
    name = ' '.join(str(skill or '').split()).strip(' .,;:')
    if not name:
        return ''
    try:
        from ai_assistant import normalize_skill
        canon = normalize_skill(name)
        if canon:
            return canon
    except Exception:
        pass
    return name


def is_generic_reason(text) -> bool:
    """True, если причина отказа — шаблон самого бота, а не слова работодателя.

    Пустая причина тоже шаблон: подтверждать в ней нечего.
    """
    flat = ' '.join(str(text or '').split()).lower().rstrip('.')
    if not flat or flat in GENERIC_REASONS:
        return True
    # Формулировка с оговоркой — это предположение бота, а не слова работодателя.
    # Такие причины попадали в колонку «подтверждено перепиской» и завышали её.
    hedges = ('вероятно', 'скорее всего', 'возможно', 'похоже', 'предположительно',
              'судя по всему', 'если таковое', 'не исключено')
    return any(h in flat for h in hedges)


def human_status(code) -> str:
    """Переводит внутренний код статуса в понятную пользователю фразу.

    Неизвестный код НЕ показывается пользователю: раньше он возвращался как есть,
    и на экран статистики протекали внутренние коды вида `toxic_filter: 1`.
    Новый код вместо этого попадает в лог, чтобы недостающий перевод нашёлся.
    """
    if code is None:
        return 'без статуса'
    key = str(code).strip()
    label = STATUS_LABELS.get(key) or STATUS_LABELS.get(key.lower())
    if label:
        return label
    logger.debug(f'Нет перевода для статуса: {key}')
    return 'причина не указана' 


class DatabaseManager:
    """Управление базой данных откликов, отказов и адаптивных навыков."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        """Создает подключение к SQLite с поддержкой многопоточности и row_factory."""
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        """Создает таблицы базы данных, если они отсутствуют."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            # 1. Таблица всех отправленных откликов
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS applications (
                    vacancy_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    company TEXT,
                    url TEXT,
                    applied_at TEXT NOT NULL,
                    cover_letter TEXT,
                    questions_count INTEGER DEFAULT 0,
                    ats_score INTEGER DEFAULT 0,
                    detected_skills TEXT, -- JSON список
                    status TEXT DEFAULT 'sent', -- 'sent', 'invited', 'discarded', 'unknown'
                    last_updated TEXT
                )
            """)

            # 2. Таблица детального анализа отказов
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS rejections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vacancy_id TEXT,
                    title TEXT,
                    company TEXT,
                    url TEXT,
                    analyzed_at TEXT NOT NULL,
                    rejection_reason TEXT,
                    ats_score INTEGER DEFAULT 0,
                    missing_keywords TEXT, -- JSON список
                    knockout_filters TEXT, -- JSON список
                    remediation_advice TEXT, -- JSON список
                    auto_fixed INTEGER DEFAULT 0 -- 1 если навык добавлен в авто-инъекцию
                )
            """)

            # 2.1. Признак «причина подтверждена словами работодателя».
            # ALTER под try/except: на уже существующей базе колонка есть, и
            # повторный ALTER падает — ронять из-за этого запуск незачем.
            # Backfill идёт только в момент реального добавления колонки:
            # 245 накопленных строк иначе молча стали бы «не подтверждено».
            try:
                cursor.execute(
                    "ALTER TABLE rejections ADD COLUMN evidence_based INTEGER DEFAULT 0"
                )
                filled = 0
                for row in cursor.execute(
                        "SELECT id, rejection_reason FROM rejections").fetchall():
                    if not is_generic_reason(row['rejection_reason']):
                        cursor.execute(
                            "UPDATE rejections SET evidence_based = 1 WHERE id = ?",
                            (row['id'],))
                        filled += 1
                logger.info(f"Отмечено отказов, подтверждённых ответом работодателя: {filled}")
            except sqlite3.OperationalError:
                pass  # колонка уже есть

            # 3. Таблица адаптивных навыков (багаж опыта и авто-исправлений)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS adaptive_skills (
                    skill_name TEXT PRIMARY KEY,
                    rejection_count INTEGER DEFAULT 1,
                    is_learned INTEGER DEFAULT 1,
                    last_seen TEXT
                )
            """)

            # 4. Таблица пропущенных вакансий (фильтры, тесты, уже откликались, черные списки)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS skipped_vacancies (
                    vacancy_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    company TEXT,
                    url TEXT,
                    reason TEXT NOT NULL, -- 'excluded_filter', 'ai_rejected', 'skipped_test', 'already_applied', etc.
                    details TEXT,
                    created_at TEXT NOT NULL
                )
            """)

            # Индексы для быстрой выборки
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_app_status ON applications(status)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_rej_vid ON rejections(vacancy_id)")
            # Без этого индекса INSERT OR REPLACE в record_rejection_analysis не на что
            # опереться и вырождается в обычный INSERT: на чистой БД дубли отказов
            # начали бы копиться заново. На таблице с уже накопленными дублями
            # создание упадет — это не повод ронять запуск, чистка делается отдельно.
            try:
                cursor.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_rej_unique "
                    "ON rejections (lower(trim(title)), lower(trim(company)))"
                )
            except Exception as e:
                logger.warning("Не удалось включить защиту от повторов отказов: в списке уже есть одинаковые записи. Разбор это не ломает — дубли чистятся отдельно.")
                logger.debug(f"Индекс отказов не создан: {e}")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_skip_reason ON skipped_vacancies(reason)")
            conn.commit()

    def record_application(
        self,
        vacancy_id: str,
        title: str,
        company: str = '',
        url: str = '',
        cover_letter: str = '',
        questions_count: int = 0,
        ats_score: int = 0,
        detected_skills: Optional[List[str]] = None,
        status: str = 'sent'
    ) -> bool:
        """Сохраняет или обновляет запись об отклике."""
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        skills_json = json.dumps(detected_skills or [], ensure_ascii=False)

        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO applications (
                        vacancy_id, title, company, url, applied_at,
                        cover_letter, questions_count, ats_score, detected_skills, status, last_updated
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(vacancy_id) DO UPDATE SET
                        title = excluded.title,
                        company = COALESCE(NULLIF(excluded.company, ''), applications.company),
                        url = COALESCE(NULLIF(excluded.url, ''), applications.url),
                        cover_letter = COALESCE(NULLIF(excluded.cover_letter, ''), applications.cover_letter),
                        questions_count = MAX(applications.questions_count, excluded.questions_count),
                        ats_score = MAX(applications.ats_score, excluded.ats_score),
                        detected_skills = COALESCE(NULLIF(excluded.detected_skills, '[]'), applications.detected_skills),
                        status = excluded.status,
                        last_updated = excluded.last_updated
                """, (
                    str(vacancy_id), title, company, url, now,
                    cover_letter, questions_count, ats_score, skills_json, status, now
                ))
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Не удалось сохранить отклик на вакансию {vacancy_id}: {explain_error(e)}")
            return False

    def record_rejection_analysis(
        self,
        vacancy_id: str,
        title: str,
        company: str = '',
        url: str = '',
        rejection_reason: str = '',
        ats_score: int = 0,
        missing_keywords: Optional[List[str]] = None,
        knockout_filters: Optional[List[str]] = None,
        remediation_advice: Optional[List[str]] = None,
        evidence_based: Optional[bool] = None,
        employer_messages: Optional[List[str]] = None
    ) -> int:
        """Сохраняет разбор отказа и регистрирует навыки для исправления резюме.

        Признак «причину назвал работодатель» считается ровно по одному правилу:
        подтверждено = остались непустые реплики работодателя. Раньше признак в
        части путей выводился из ТЕКСТА причины, и свободная формулировка ИИ
        («Работодатель ищет специалиста по Kafka») проходила как подтверждённая,
        хотя работодатель в чате не написал ни слова.

        employer_messages сюда передаются уже вычищенными
        (rejection_analyzer.clean_employer_messages): в сыром виде туда попадает
        вёрстка страницы («Отклик на вакансию», «Без сопроводительного письма»,
        время), и такие строки помечались подтверждёнными.

        evidence_based оставлен для случая, когда вызывающий код знает ответ сам;
        текст причины на него больше не влияет.
        """
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        if evidence_based is None:
            evidence_based = any(str(m).strip() for m in (employer_messages or []))
        missing = missing_keywords or []
        knockouts = knockout_filters or []
        remediation = remediation_advice or []

        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()

                # Был ли уже такой отказ — выясняем ДО записи: после OR REPLACE
                # отличить новый отказ от повторного разбора уже нельзя.
                # Настоящий номер вакансии учитываем наравне с парой
                # «название+компания»: иначе повторный разбор той же вакансии,
                # пришедшей с другим написанием компании, считался новым отказом
                # и накручивал счётчик навыков.
                real_vid = str(vacancy_id or '').strip()
                has_real_vid = bool(real_vid) and not real_vid.startswith('chat_')
                cursor.execute(
                    "SELECT 1 FROM rejections WHERE (lower(trim(title)) = ? AND lower(trim(company)) = ?)"
                    " OR (? = 1 AND trim(vacancy_id) = ?)",
                    ((title or '').strip().lower(), (company or '').strip().lower(),
                     1 if has_real_vid else 0, real_vid)
                )
                is_new_rejection = cursor.fetchone() is None

                # Дубли по номеру вакансии. Уникальный индекс стоит на паре
                # «название+компания», а компания со страницы приходит нестабильно
                # (то «Ромашка», то «Ромашка, IT и Digital», то пусто) —
                # и один и тот же отказ ложился двумя строками. Синтетический
                # chat_-id так чистить нельзя: он и построен из названия с компанией.
                if has_real_vid:
                    cursor.execute("DELETE FROM rejections WHERE trim(vacancy_id) = ?", (real_vid,))

                # Записываем отказ. OR REPLACE, а не INSERT: на таблице стоит
                # уникальный индекс по паре «вакансия+компания», и повторный разбор
                # того же отказа должен обновлять строку, а не плодить копии
                # (так в базе накопилось 237 дублей, до 17 на одну вакансию).
                cursor.execute("""
                    INSERT OR REPLACE INTO rejections (
                        vacancy_id, title, company, url, analyzed_at,
                        rejection_reason, ats_score, missing_keywords,
                        knockout_filters, remediation_advice, auto_fixed,
                        evidence_based
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """, (
                    str(vacancy_id), title, company, url, now,
                    rejection_reason, ats_score,
                    json.dumps(missing, ensure_ascii=False),
                    json.dumps(knockouts, ensure_ascii=False),
                    json.dumps(remediation, ensure_ascii=False),
                    1 if evidence_based else 0
                ))
                rej_id = cursor.lastrowid

                # Обновляем статус в таблице откликов на 'discarded'
                cursor.execute("""
                    UPDATE applications 
                    SET status = 'discarded', last_updated = ? 
                    WHERE vacancy_id = ?
                """, (now, str(vacancy_id)))

                # Авто-исправление: заносим все недостающие навыки в таблицу адаптивных навыков.
                # rejection_count наращивается только для НОВОГО отказа: повторный разбор
                # той же вакансии схлопывается в rejections через OR REPLACE, а счётчик
                # навыка откатить нечем. Так он и разъехался вчетверо (932 против 234),
                # а get_adaptive_skills сортирует по нему — то есть порядок навыков,
                # уходящих в сопроводительные и в резюме, отражал удалённые дубли.
                if is_new_rejection:
                    for kw in missing:
                        kw_clean = canonical_skill_name(kw)
                        if kw_clean:
                            cursor.execute("""
                                INSERT INTO adaptive_skills (skill_name, rejection_count, is_learned, last_seen)
                                VALUES (?, 1, 1, ?)
                                ON CONFLICT(skill_name) DO UPDATE SET
                                    rejection_count = adaptive_skills.rejection_count + 1,
                                    last_seen = excluded.last_seen
                            """, (kw_clean, now))
                else:
                    for kw in missing:
                        kw_clean = canonical_skill_name(kw)
                        if kw_clean:
                            cursor.execute("""
                                INSERT INTO adaptive_skills (skill_name, rejection_count, is_learned, last_seen)
                                VALUES (?, 1, 1, ?)
                                ON CONFLICT(skill_name) DO UPDATE SET last_seen = excluded.last_seen
                            """, (kw_clean, now))

                conn.commit()
                return rej_id
        except Exception as e:
            logger.error(f"Не удалось сохранить разбор отказа по вакансии {vacancy_id}: {explain_error(e)}")
            return 0

    def get_analyzed_rejection_identifiers(self) -> set:
        """Возвращает множество всех уже проанализированных ID вакансий и ключей (title_company)."""
        identifiers = set()
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT vacancy_id, title, company FROM rejections")
                for row in cursor.fetchall():
                    vid = str(row['vacancy_id'] or '').strip()
                    title = str(row['title'] or '').strip().lower()
                    company = str(row['company'] or '').strip().lower()
                    if vid:
                        identifiers.add(vid)
                    if title and company:
                        identifiers.add(f"{title}_{company}")
        except Exception as e:
            logger.error(f"Не удалось прочитать список уже разобранных отказов: {explain_error(e)}")
        return identifiers

    def record_adaptive_skills(self, skills: List[str]):
        """Добавляет или обновляет навыки в таблице адаптивных навыков."""
        if not skills:
            return
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                for skill in skills:
                    sk = str(skill).strip()
                    if not sk:
                        continue
                    cursor.execute("""
                        INSERT INTO adaptive_skills (skill_name, rejection_count, is_learned, last_seen)
                        VALUES (?, 1, 1, ?)
                        ON CONFLICT(skill_name) DO UPDATE SET
                            rejection_count = adaptive_skills.rejection_count + 1,
                            is_learned = 1,
                            last_seen = excluded.last_seen
                    """, (sk, now))
                conn.commit()
        except Exception as e:
            logger.error(f"Не удалось сохранить выученные навыки: {explain_error(e)}")

    def get_adaptive_skills(self) -> List[str]:
        """Возвращает список всех адаптивно изученных навыков из отказов для авто-инъекции в письма."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT skill_name FROM adaptive_skills WHERE is_learned = 1 ORDER BY rejection_count DESC")
                return [row['skill_name'] for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Не удалось загрузить выученные навыки: {explain_error(e)}")
            return []

    def get_stats(self) -> Dict[str, Any]:
        """Возвращает сводную статистику откликов, отказов и прогресса конверсии к цели 8/10."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()

                cursor.execute("SELECT COUNT(*) as total FROM applications")
                total_apps = cursor.fetchone()['total']

                cursor.execute("SELECT COUNT(*) as sent FROM applications WHERE status = 'sent'")
                sent_apps = cursor.fetchone()['sent']

                cursor.execute("SELECT COUNT(*) as invited FROM applications WHERE status = 'invited'")
                invited_apps = cursor.fetchone()['invited']

                cursor.execute("SELECT COUNT(*) as discards FROM applications WHERE status = 'discarded'")
                discarded_apps = cursor.fetchone()['discards']

                cursor.execute("SELECT COUNT(*) as total_rej FROM rejections")
                total_rejections_audited = cursor.fetchone()['total_rej']

                # Сколько причин названы самим работодателем, а сколько бот
                # дописал за него. Без этого деления шаблон «Опыт и грейд»
                # выглядит как установленный факт и забивает реальный сигнал.
                cursor.execute(
                    "SELECT COUNT(*) as cnt FROM rejections WHERE evidence_based = 1")
                rejections_confirmed = cursor.fetchone()['cnt']
                rejections_guessed = total_rejections_audited - rejections_confirmed

                # Среднее только по подтверждённым: строки-заглушки пишутся с
                # ats_score 0/70/85/100 без единого измерения, и в общем среднем
                # они дают красивое, но пустое число. На экран показатель всё
                # равно не идёт (см. print_stats_cli) — у подтверждённых строк
                # score тоже проставлен константой.
                cursor.execute(
                    "SELECT AVG(ats_score) as avg_score FROM rejections WHERE evidence_based = 1")
                avg_ats_row = cursor.fetchone()
                avg_ats = round(avg_ats_row['avg_score'] or 0, 1)

                cursor.execute("SELECT COUNT(*) as skills_cnt FROM adaptive_skills WHERE is_learned = 1")
                learned_skills_count = cursor.fetchone()['skills_cnt']

                # Расчет пропущенных вакансий
                cursor.execute("SELECT COUNT(*) as total_skip FROM skipped_vacancies")
                total_skipped = cursor.fetchone()['total_skip']

                cursor.execute("SELECT reason, COUNT(*) as cnt FROM skipped_vacancies GROUP BY reason")
                skipped_by_reason = {row['reason']: row['cnt'] for row in cursor.fetchall()}

                # Расчет текущей конверсии (приглашения / закрытые исходы)
                closed_negotiations = invited_apps + discarded_apps
                conversion_rate = round((invited_apps / closed_negotiations * 100), 1) if closed_negotiations > 0 else 0.0

                return {
                    "total_applications": total_apps,
                    "pending_applications": sent_apps,
                    "invitations": invited_apps,
                    "discards": discarded_apps,
                    "conversion_rate": conversion_rate,
                    "target_conversion_rate": 80.0,
                    "total_rejections_audited": total_rejections_audited,
                    "rejections_confirmed": rejections_confirmed,
                    "rejections_guessed": rejections_guessed,
                    "average_ats_score": avg_ats,
                    "adaptive_skills_learned": learned_skills_count,
                    "total_skipped": total_skipped,
                    "skipped_by_reason": skipped_by_reason
                }
        except Exception as e:
            logger.error(f"Не удалось собрать статистику: {explain_error(e)}")
            return {}

    def record_skipped_vacancy(
        self,
        vacancy_id: str,
        title: str,
        company: str = '',
        url: str = '',
        reason: str = 'excluded_filter',
        details: str = ''
    ) -> bool:
        """Сохраняет запись о пропущенной вакансии с указанием причины."""
        if not vacancy_id:
            return False
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT OR REPLACE INTO skipped_vacancies (
                        vacancy_id, title, company, url, reason, details, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (str(vacancy_id), title, company, url, reason, details, now))
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Не удалось сохранить пропущенную вакансию {vacancy_id}: {explain_error(e)}")
            return False

    def get_skipped_stats(self) -> Dict[str, Any]:
        """Возвращает статистику пропущенных вакансий по причинам."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) as total FROM skipped_vacancies")
                total = cursor.fetchone()['total']

                cursor.execute("SELECT reason, COUNT(*) as cnt FROM skipped_vacancies GROUP BY reason ORDER BY cnt DESC")
                by_reason = {row['reason']: row['cnt'] for row in cursor.fetchall()}

                cursor.execute("SELECT vacancy_id, title, company, reason, details, created_at FROM skipped_vacancies ORDER BY created_at DESC LIMIT 10")
                recent = [dict(row) for row in cursor.fetchall()]

                return {
                    "total_skipped": total,
                    "by_reason": by_reason,
                    "recent": recent
                }
        except Exception as e:
            logger.error(f"Не удалось собрать статистику пропущенных вакансий: {explain_error(e)}")
            return {"total_skipped": 0, "by_reason": {}, "recent": []}

    def clear_skipped(self, reason: Optional[str] = None) -> int:
        """Очищает пропущенные вакансии (все или по конкретной причине)."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                if reason:
                    cursor.execute("DELETE FROM skipped_vacancies WHERE reason = ?", (reason,))
                else:
                    cursor.execute("DELETE FROM skipped_vacancies")
                deleted = cursor.rowcount
                conn.commit()
                return deleted
        except Exception as e:
            logger.error(f"Не удалось очистить список пропущенных вакансий: {explain_error(e)}")
            return 0

    def get_recent_rejections(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Возвращает список последних проанализированных отказов с рекомендациями."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT vacancy_id, title, company, analyzed_at, rejection_reason,
                           ats_score, missing_keywords, knockout_filters, remediation_advice
                    FROM rejections
                    ORDER BY id DESC LIMIT ?
                """, (limit,))
                rows = cursor.fetchall()
                results = []
                for row in rows:
                    results.append({
                        "vacancy_id": row['vacancy_id'],
                        "title": row['title'],
                        "company": row['company'],
                        "analyzed_at": row['analyzed_at'],
                        "rejection_reason": row['rejection_reason'],
                        "ats_score": row['ats_score'],
                        "missing_keywords": json.loads(row['missing_keywords'] or '[]'),
                        "knockout_filters": json.loads(row['knockout_filters'] or '[]'),
                        "remediation_advice": json.loads(row['remediation_advice'] or '[]')
                    })
                return results
        except Exception as e:
            logger.error(f"Не удалось прочитать список отказов: {explain_error(e)}")
            return []

    def sync_from_json_history(self, json_path: Optional[str] = None) -> int:
        """Импортирует исторические отклики из applied_vacancies_selenium.json в SQLite."""
        path = json_path or os.path.join(SCRIPT_DIR, 'applied_vacancies_selenium.json')
        if not os.path.exists(path):
            return 0

        imported = 0
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            with self._get_connection() as conn:
                cursor = conn.cursor()
                records = []
                for vid, item in data.items():
                    if not isinstance(item, dict):
                        continue
                    name = item.get('name', 'Вакансия')
                    applied_at = item.get('date', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
                    status = item.get('status', 'sent')
                    url = f"https://hh.ru/vacancy/{vid}"
                    records.append((str(vid), name, url, applied_at, status, applied_at))

                cursor.executemany("""
                    INSERT OR IGNORE INTO applications (
                        vacancy_id, title, url, applied_at, status, last_updated
                    ) VALUES (?, ?, ?, ?, ?, ?)
                """, records)
                conn.commit()
                imported = len(records)
        except Exception as e:
            logger.error(f"Не удалось перенести историю откликов из файла: {explain_error(e)}")

        return imported


def print_stats_cli():
    """Выводит красивую статистику базы данных в консоль."""
    db = DatabaseManager()
    if '--clear-skipped' in sys.argv:
        reason_arg = None
        for i, a in enumerate(sys.argv):
            if a == '--reason' and i + 1 < len(sys.argv):
                reason_arg = sys.argv[i + 1]
        deleted_count = db.clear_skipped(reason=reason_arg)
        filter_str = f" (причина: {human_status(reason_arg)})" if reason_arg else ""
        print(f"[OK] Очищено {deleted_count} пропущенных вакансий{filter_str}")
        return

    if '--sync' in sys.argv or db.get_stats().get('total_applications', 0) == 0:
        db.sync_from_json_history()

    stats = db.get_stats()
    adaptive_skills = db.get_adaptive_skills()

    print("\n" + "="*70)
    print("ВАШИ ОТКЛИКИ, ОТКАЗЫ И СОВПАДЕНИЕ С ВАКАНСИЯМИ")
    print("="*70)
    print(f"Всего откликов:                {stats.get('total_applications', 0)}")
    print(f"На рассмотрении:               {stats.get('pending_applications', 0)}")
    print(f"Приглашений:                   {stats.get('invitations', 0)}")
    print(f"Отказов:                       {stats.get('discards', 0)}")
    print(f"Доля приглашений:              {stats.get('conversion_rate', 0)}% (цель: 80%)")
    print(f"Проанализировано отказов:      {stats.get('total_rejections_audited', 0)}")
    print(f"   со слов работодателя:       {stats.get('rejections_confirmed', 0)}")
    print(f"   догадка бота, не факт:      {stats.get('rejections_guessed', 0)}")
    # Показателя «Резюме подходило вакансиям» здесь больше нет: ats_score в
    # rejections на три четверти проставлен константами (0/70/85/100) из
    # fallback-веток, среднее по ним ничего не измеряет. Вернуть строку можно,
    # когда score начнёт приходить из реального разбора.

    print(f"Навыков подобрано из отказов:  {stats.get('adaptive_skills_learned', 0)}")
    total_skipped = stats.get('total_skipped', 0)
    if total_skipped > 0:
        print(f"Пропущено вакансий:            {total_skipped}")
        reasons = stats.get('skipped_by_reason', {})
        if reasons:
            reasons_str = ", ".join([f"{human_status(k)}: {v}" for k, v in reasons.items()])
            print(f"   (по причинам: {reasons_str})")
    print("-"*70)
    if adaptive_skills:
        print("Навыки, которые бот добавляет в сопроводительные письма:")
        print("   " + ", ".join(adaptive_skills[:15]))
    print("="*70 + "\n")


if __name__ == '__main__':
    print_stats_cli()

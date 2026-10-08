"""
Модуль автоматического обновления и оптимизации резюме на HeadHunter (hh.ru) через Selenium.
Активирует ключевые навыки, проставляет уровни владения, заменяет нерелевантные теги
при достижении лимита 30 навыков и обогащает раздел «Обо мне» стандартами безопасности.
"""

import os
import sys
import time
import json
import logging
import hashlib
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
from urllib.parse import urlsplit

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

from app_paths import CODE_DIR, DATA_DIR, PROFILE_ID

SCRIPT_DIR = DATA_DIR
try:
    from config_manager import get_active_resume
    DEFAULT_RESUME_ID, _ = get_active_resume()
except Exception:
    DEFAULT_RESUME_ID = ""

from terminal_ui import ColoredConsoleFormatter, explain_error, RED, BOLD, RESET

# Перевод видимости резюме на человеческий. Лежит на уровне модуля, потому что
# нужен и в логе, и в консоли: раньше словарь был только в CLI, а лог печатал
# питоновские «None» и «True» посреди русской фразы.
VISIBILITY_TEXT = {
    True: "видно всем работодателям",
    False: "СКРЫТО — поднятие не сработает",
    None: "определить не удалось",
}

logger = logging.getLogger('resume_updater')
console_handler = logging.StreamHandler()
console_handler.setFormatter(ColoredConsoleFormatter('%(levelname)s - %(message)s'))
file_handler = logging.FileHandler(os.path.join(SCRIPT_DIR, 'resume_updater.log'), encoding='utf-8')
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))

# Уровень DEBUG у логгера, INFO у консоли, DEBUG у файла.
#
# Раньше логгер стоял на INFO, и logging.debug отсекался ЕЩЁ НА ЛОГГЕРЕ — то есть
# не доходил даже до файла журнала. Каждое «подробности записаны в журнал» было
# неправдой: технические детали исчезали бесследно, и разбирать сбои приходилось
# вслепую. Теперь пользователь по-прежнему видит только INFO и выше, а подробности
# реально ложатся в файл.
console_handler.setLevel(logging.INFO)
# Служебные строки библиотек (адреса запросов, коды ответов) — только в файл.
try:
    from terminal_ui import hide_technical_from_console
    hide_technical_from_console(console_handler)
except Exception:
    pass
file_handler.setLevel(logging.DEBUG)
logger.setLevel(logging.DEBUG)
logger.addHandler(file_handler)
logger.addHandler(console_handler)
logger.propagate = False

# Навыки с наивысшим приоритетом (уровень: Продвинутый)
ADVANCED_SKILLS = {
    'burp suite', 'owasp top 10', 'penetration testing', 'kali linux',
    'metasploit', 'nmap', 'hydra', 'devsecops', 'red team', 'sqlmap',
    'linux', 'python', 'docker', 'gitlab ci', 'sast', 'dast',
    'криптопро', 'скзи', 'maxpatrol', 'active directory', 'ad', 'kubernetes',
    'k8s', 'siem', 'soc', 'гост', 'kuma', 'reverse engineering', 'go'
}

# Низкоприоритетные навыки, замещаемые при достижении жесткого лимита 30 навыков на HH
LOW_PRIORITY_REPLACE_CANDIDATES = [
    'html', 'ручное тестирование', 'функциональное тестирование',
    'алгоритмы и структуры данных', 'api', 'ооп', 'машинное обучение',
    'john the ripper', 'hydra', 'css', 'javascript'
]


def profile_grounded_skills(skills: Optional[List[str]],
                            profile: Optional[Dict[str, Any]]) -> Tuple[List[str], List[str]]:
    """Делит навыки на подтверждённые профилем кандидата и остальные.

    Навык из разбора отказов — это то, чего не хватило под вакансию, а не то,
    чем кандидат владеет. Раньше такие навыки уходили в резюме как есть, и
    автоматическая правка превращалась бы в приписывание чужого опыта.
    Подтверждением считаем только candidate_profile: список навыков, текст
    опыта, сертификаты, «О себе» и специализацию. Сверка по целому слову:
    подстрока засчитала бы «Go» за счёт «Google», а «AD» за счёт «Град».

    Возвращает (подтверждённые, только_рекомендация) в исходном написании.
    """
    import re as _re
    try:
        from ai_assistant import normalize_skill
    except Exception:
        def normalize_skill(s):
            return ' '.join(str(s or '').lower().split())

    def flat(s: Any) -> str:
        # «SAST / DAST» и «SAST/DAST» — одно написание.
        return _re.sub(r'\s*([/&+])\s*', r'\g<1>', ' '.join(str(s or '').lower().split()))

    profile = profile if isinstance(profile, dict) else {}
    own_skills = [s for s in (profile.get('skills') or []) if str(s or '').strip()]
    # Нормализованные навыки профиля ловят синонимы: «AppSec» и «Application Security».
    own = {normalize_skill(s) for s in own_skills} - {''}

    texts: List[Any] = list(own_skills) + [profile.get('about'), profile.get('specialization')]
    texts += list(profile.get('certificates') or [])
    for item in profile.get('experience_highlights') or []:
        if isinstance(item, dict):
            texts += [item.get('position'), item.get('what')]
        else:
            texts.append(item)
    text = ' \n '.join(flat(t) for t in texts if t)

    grounded: List[str] = []
    advice: List[str] = []
    seen = set()
    for raw in skills or []:
        name = ' '.join(str(raw or '').split())
        key = normalize_skill(name)
        if not key or key in seen:
            continue
        seen.add(key)
        variants = {v for v in (key, flat(name)) if v}
        found = key in own or any(
            _re.search(r'(?<!\w)' + _re.escape(v) + r'(?!\w)', text) for v in variants)
        (grounded if found else advice).append(name)
    return grounded, advice


def resume_revision_path(resume_id: str) -> str:
    key = hashlib.sha256(str(resume_id).encode('utf-8')).hexdigest()[:16]
    return os.path.join(SCRIPT_DIR, f'resume_adaptation_{key}.json')


def load_resume_revision(resume_id: str) -> Dict[str, Any]:
    try:
        with open(resume_revision_path(resume_id), encoding='utf-8') as f:
            row = json.load(f)
    except FileNotFoundError:
        return {}
    if not isinstance(row, dict) or row.get('resume_id') != resume_id:
        raise ValueError('Повреждена запись правки целевого резюме')
    if (row.get('status') not in ('pending', 'verified', 'not_sent')
            or not isinstance(row.get('after'), str) or not isinstance(row.get('before'), str)
            or not isinstance(row.get('created_at'), str)):
        raise ValueError('В записи правки резюме нет исходного или нового текста')
    datetime.fromisoformat(row['created_at'])
    return row


def save_resume_revision(row: Dict[str, Any]) -> None:
    path = resume_revision_path(row['resume_id'])
    with open(path + '.tmp', 'w', encoding='utf-8') as f:
        json.dump(row, f, ensure_ascii=False, indent=2)
    os.replace(path + '.tmp', path)


def is_dead_session_message(message: Any) -> bool:
    lower_message = str(message).lower()
    dead_markers = (
        'invalid session id',
        'no such window',
        'target window already closed',
        'web view not found',
        'disconnected: unable to connect',
        'disconnected: unable to send message',
        'chrome not reachable',
        'session deleted',
        'session not created',
        'tab crashed',
        'окно браузера закрыто',
    )
    return any(marker in lower_message for marker in dead_markers)



def read_experience_years_from_resume(driver, resume_id: str):
    """Читает стаж прямо со страницы резюме на hh.ru. Возвращает годы (float) или None.

    Поле `candidate_profile.experience_years` заполняется руками и устаревает:
    в конфиге стояло 3, а в резюме — 6 лет 9 месяцев. Фильтр по грейду резал
    вакансии, на которые кандидат вполне может претендовать, и об этом никто
    не узнавал, потому что цифру никто не сверял.
    """
    import re as _re
    try:
        driver.get(f"https://hh.ru/resume/{resume_id}")
        time.sleep(3.0)
        text = driver.execute_script("return document.body.innerText") or ''
    except Exception as e:
        logger.debug(f"Не удалось прочитать стаж с резюме: {e}")
        return None

    m = _re.search(r'Опыт работы[:\s—-]*(?:(\d+)\s*(?:год|года|лет))?\s*(?:(\d+)\s*мес)?', text)
    if not m or not (m.group(1) or m.group(2)):
        return None
    years = int(m.group(1) or 0)
    months = int(m.group(2) or 0)
    return round(years + months / 12.0, 2)


BUMP_SCHEDULE_FILE = 'bump_schedule.json'
BUMP_INTERVAL_SECONDS = 4 * 3600


RESUME_PAGE_SCRIPT = r"""
const text = el => el ? el.innerText.trim() : '';
const one = sel => text(document.querySelector(sel));
const titles = [...document.querySelectorAll('[data-qa="title"]')];
function section(re) {
  const t = titles.find(e => re.test(e.innerText));
  if (!t) return '';
  const head = t.innerText.trim();
  let box = t;
  for (let i = 0; i < 6 && box && box.innerText.trim().length <= head.length + 5; i++) box = box.parentElement;
  return box ? box.innerText.trim().slice(head.length).trim() : '';
}
return {
  title: one('[data-qa="resume-block-title-position"]'),
  salary: one('[data-qa="resume-block-salary"]'),
  work_formats: one('[data-qa="resume-position-field-workFormats"]'),
  phone: one('[data-qa="resume-contact-phone-value"]'),
  email: text(document.querySelector('[data-qa^="resume-contact-email-value"]')),
  experience_title: (titles.map(e => e.innerText).find(t => /Опыт работы/.test(t)) || ''),
  jobs: [...document.querySelectorAll('[data-qa="profile-experience-company-card"]')]
          .map(c => c.innerText.split('\n').map(x => x.trim()).filter(Boolean)),
  skills: [...document.querySelectorAll('[data-qa^="skill-tag-"]')].map(e => e.innerText.trim()).filter(Boolean),
  education: [...document.querySelectorAll('[data-qa^="resume-list-card-education-item-"]')]
               .map(e => e.innerText.replace(/\s+/g, ' ').trim()),
  about: section(/о\s*себе/i),
  certificates: section(/Сертификаты/),
};
"""


def years_from_experience_title(title: str) -> Optional[float]:
    """«Опыт работы: 6 лет 9 месяцев» -> 6.75. Нет чисел — None."""
    import re
    t = (title or '').replace('\xa0', ' ').lower()
    years = re.search(r'(\d+)\s*(?:год|лет)', t)
    months = re.search(r'(\d+)\s*месяц', t)
    if not years and not months:
        return None
    return round(int(years.group(1) if years else 0) + int(months.group(1) if months else 0) / 12, 2)


def merge_resume_into_profile(profile: Dict[str, Any], page: Dict[str, Any]) -> Dict[str, Any]:
    """Профиль, дополненный данными со страницы резюме. Telegram и имя не трогаем:
    на странице их нет. Пустые поля страницы профиль не затирают."""
    out = dict(profile or {})
    if page.get('title'):
        out['specialization'] = page['title']
    if page.get('skills'):
        out['skills'] = list(dict.fromkeys(page['skills']))
    jobs = []
    for lines in page.get('jobs') or []:
        if not lines:
            continue
        # Карточка: компания, стаж, должность, период, описание.
        jobs.append({'company': lines[0],
                     'position': lines[2] if len(lines) > 2 else '',
                     'period': lines[3] if len(lines) > 3 else '',
                     'what': ' '.join(lines[4:])[:1500]})
    if jobs:
        out['experience_highlights'] = jobs
    years = years_from_experience_title(page.get('experience_title', ''))
    if years is not None:
        out['experience_years'] = years
    if page.get('about'):
        out['about'] = page['about'][:3000]
    if page.get('education'):
        out['education'] = '; '.join(page['education'])
    salary = (page.get('salary') or '').replace('\xa0', ' ').strip()
    if salary and 'не указан' not in salary.lower():
        out['expected_salary'] = salary
    formats = (page.get('work_formats') or '').lower()
    if formats:
        out['remote_preferred'] = 'удал' in formats
    ui_words = {'добавить', 'редактировать', 'показать ещё', 'показать еще'}
    certs = [c.strip() for c in (page.get('certificates') or '').split('\n')
             if c.strip() and c.strip().lower() not in ui_words]
    if certs:
        out['certificates'] = certs[:20]
    contacts = dict(out.get('contacts') or {})
    for key in ('phone', 'email'):
        if page.get(key):
            contacts[key] = page[key]
    # Заглушки из старого примера настроек работодателю уходить не должны.
    for key, fake in (('telegram', '@username'), ('phone', '+79991234567'),
                      ('email', 'user@example.com'), ('github', 'https://github.com/username')):
        if contacts.get(key) == fake:
            contacts.pop(key)
    out['contacts'] = {k: v for k, v in contacts.items() if v}
    return out


def next_bump_at(ok: bool, msg: str, now: float = None) -> float:
    """Когда пробовать поднять снова, по итогу попытки.

    Успех — через 4 часа. hh написал время («сегодня в 22:03») — в это время.
    Иначе через полчаса: кнопка не нашлась, сеть, кулдаун без времени.
    """
    import re as _re
    from datetime import datetime as _dt, timedelta as _td
    now = time.time() if now is None else now
    if ok:
        return now + BUMP_INTERVAL_SECONDS + 60
    m = _re.search(r'в\s*(\d{1,2}):(\d{2})', msg or '')
    if m:
        base = _dt.fromtimestamp(now)
        at = base.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=30, microsecond=0)
        if at.timestamp() <= now:
            at += _td(days=1)
        return at.timestamp()
    return now + 30 * 60


def record_next_bump(ok: bool, msg: str) -> None:
    path = os.path.join(SCRIPT_DIR, BUMP_SCHEDULE_FILE)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({'next_at': next_bump_at(ok, msg), 'last_ok': bool(ok),
                   'last_message': msg}, f, ensure_ascii=False)


def bump_is_due() -> bool:
    """Пора ли пробовать поднять резюме. Нет расписания — пора."""
    try:
        with open(os.path.join(SCRIPT_DIR, BUMP_SCHEDULE_FILE), encoding='utf-8') as f:
            return time.time() >= float(json.load(f).get('next_at', 0))
    except Exception:
        return True


class HHResumeUpdater:
    """Автоматическое обновление навыков и разделов резюме на HH.ru."""

    def __init__(self, resume_id: Optional[str] = None, headless: bool = False, driver: Any = None):
        self.resume_id = resume_id or DEFAULT_RESUME_ID
        self.headless = headless
        self.driver = driver
        self._owns_driver = (driver is None)
        self._user_closed = False

    def _save_window_geometry(self):
        """Сохраняет текущие координаты и размер окна браузера."""
        if not self.driver or self.headless:
            return
        try:
            geom_file = os.path.join(SCRIPT_DIR, 'chrome_window_geometry.json')
            rect = self.driver.get_window_rect()
            if rect and rect.get('width', 0) > 200:
                with open(geom_file, 'w', encoding='utf-8') as f:
                    json.dump(rect, f)
        except Exception:
            pass

    def _restore_window_geometry(self):
        """Восстанавливает предыдущие координаты и размер окна браузера."""
        if not self.driver or self.headless:
            return
        try:
            geom_file = os.path.join(SCRIPT_DIR, 'chrome_window_geometry.json')
            if os.path.exists(geom_file):
                with open(geom_file, 'r', encoding='utf-8') as f:
                    rect = json.load(f)
                if rect and 'x' in rect and 'y' in rect and 'width' in rect and 'height' in rect:
                    self.driver.set_window_rect(
                        x=rect['x'],
                        y=rect['y'],
                        width=rect['width'],
                        height=rect['height']
                    )
        except Exception:
            pass

    def _cleanup_profile_processes(self, profile_dir: str):
        """Завершает зависшие процессы Chrome, блокирующие chrome_profile."""
        from terminal_ui import kill_profile_chrome
        kill_profile_chrome(profile_dir)

    def is_driver_alive(self) -> bool:
        """Проверяет, жив ли сеанс браузера."""
        if getattr(self, '_user_closed', False) or not self.driver:
            return False
        try:
            handles = self.driver.window_handles
            if not handles:
                self._user_closed = True
                logger.info("[СТОП] Все окна браузера закрыты пользователем. Работа завершена.")
                return False
            _ = self.driver.current_url
            return True
        except Exception as e:
            if is_dead_session_message(e):
                self._user_closed = True
                logger.info("[СТОП] Браузер закрыт пользователем (сессия завершена). Работа завершена.")
            return False

    def _init_driver(self) -> bool:
        if getattr(self, '_user_closed', False):
            return False
        if self.driver and self.is_driver_alive():
            return True

        self.driver = None
        self._owns_driver = True

        import subprocess
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service

        options = Options()

        if os.environ.get('CHROME_BINARY'):

            options.binary_location = os.environ['CHROME_BINARY']
        if self.headless:
            options.add_argument('--headless=new')
        # HH может не завершать load даже при уже доступной форме.
        options.page_load_strategy = 'eager'
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--disable-background-networking')
        options.add_argument('--disable-sync')
        options.add_argument('--disable-default-apps')
        options.add_argument('--disable-extensions')
        options.add_argument('--disable-infobars')
        options.add_argument('--log-level=3')
        options.add_argument('--silent')
        options.add_argument('--disable-logging')
        options.add_experimental_option('excludeSwitches', ['enable-automation', 'enable-logging'])
        options.add_experimental_option('useAutomationExtension', False)

        profile_dir = os.path.join(SCRIPT_DIR, 'chrome_profile')
        if os.path.exists(profile_dir):
            options.add_argument(f'--user-data-dir={profile_dir}')

        if not self.headless:
            geom_file = os.path.join(SCRIPT_DIR, 'chrome_window_geometry.json')
            if os.path.exists(geom_file):
                try:
                    with open(geom_file, 'r', encoding='utf-8') as f:
                        rect = json.load(f)
                    if rect and 'x' in rect and 'y' in rect and 'width' in rect and 'height' in rect:
                        options.add_argument(f"--window-position={rect['x']},{rect['y']}")
                        options.add_argument(f"--window-size={rect['width']},{rect['height']}")
                except Exception:
                    pass

        # Служебный вывод браузера — в browser.log, а не на экран.
        service = None
        try:
            from terminal_ui import chrome_service
            service = chrome_service(SCRIPT_DIR)
        except Exception:
            pass

        try:
            if service:
                self.driver = webdriver.Chrome(service=service, options=options)
            else:
                self.driver = webdriver.Chrome(options=options)

            self.driver.set_page_load_timeout(30)
            self._restore_window_geometry()
            try:
                from terminal_ui import ensure_hh_login, ensure_russian_interface
                if not ensure_hh_login(self.driver, self.headless, log=logger):
                    self.is_driver_alive()
                    return False
                ensure_russian_interface(self.driver, logger)
            except Exception as error:
                logger.warning(f"Не удалось подготовить вход в hh.ru: {explain_error(error)}")
                return False
            return True
        except Exception as e:
            err_s = str(e).lower()
            if 'instance exited' in err_s or 'session not created' in err_s:
                self._cleanup_profile_processes(profile_dir)
                # Неудачный запуск закрывает browser.log внутри Service, и
                # повтор с тем же Service падал «I/O operation on closed file».
                service = chrome_service(SCRIPT_DIR) if service else None
                try:
                    if service:
                        self.driver = webdriver.Chrome(service=service, options=options)
                    else:
                        self.driver = webdriver.Chrome(options=options)
                    self.driver.set_page_load_timeout(30)
                    self._restore_window_geometry()
                    try:
                        from terminal_ui import ensure_hh_login, ensure_russian_interface
                        if not ensure_hh_login(self.driver, self.headless, log=logger):
                            self.is_driver_alive()
                            return False
                        ensure_russian_interface(self.driver, logger)
                    except Exception as error:
                        logger.warning(f"Не удалось подготовить вход в hh.ru: {explain_error(error)}")
                        return False
                    return True
                except Exception as e2:
                    # Текст ошибки Chrome содержит полные пути к профилю и драйверу —
                    # в консоль они не нужны, в журнал попадут.
                    logger.error(f"Браузер не запустился даже после очистки: {explain_error(e2)}")
                    logger.debug(f"Техническая причина: {e2}")
                    return False
            logger.error(f"Браузер не запустился: {explain_error(e)}")
            logger.debug(f"Техническая причина: {e}")
            return False

    def close(self):
        if self.driver and self._owns_driver:
            try:
                self._save_window_geometry()
                self.driver.quit()
            except Exception:
                pass
            self.driver = None

    def get_current_resume_status(self) -> Dict[str, Any]:
        """Считывает текущее состояние резюме (навыки, позиция)."""
        from selenium.webdriver.common.by import By

        if not self.driver and not self._init_driver():
            return {"success": False, "error": "Не удалось запустить браузер"}

        url = f"https://hh.ru/resume/{self.resume_id}"
        self.driver.get(url)
        time.sleep(2)

        # Читаем позицию
        position = "Неизвестно"
        try:
            position = self.driver.find_element(By.CSS_SELECTOR, '[data-qa="resume-block-title-position"]').text.strip()
        except Exception:
            pass

        # Пробуем развернуть блок навыков, если он свернут ("Показать еще N навыков")
        try:
            expand_btns = self.driver.find_elements(
                By.XPATH,
                '//button[contains(., "еще") or contains(., "ещё") or contains(., "Показать")] | //*[@data-qa="skills-card-expand"] | //*[contains(@class, "expand")]'
            )
            for eb in expand_btns:
                if eb.is_displayed():
                    self.real_click(eb)
                    time.sleep(0.5)
                    break
        except Exception:
            pass

        # Читаем теги опубликованных навыков (классический UI)
        skills = self.driver.find_elements(By.CSS_SELECTOR, '[data-qa="bloko-tag__text"], .bloko-tag__text, [data-qa="skill"]')
        skill_names = [s.text.strip() for s in skills if s.text.strip() and len(s.text.strip()) > 1]

        # Если на странице используется современная карточка навыков с уровнями [data-qa="skills-card"]
        if not skill_names:
            try:
                card = self.driver.find_element(By.CSS_SELECTOR, '[data-qa="skills-card"]')
                raw_lines = [line.strip() for line in card.text.split('\n') if line.strip()]
                ignore_markers = {'продвинутый уровень', 'средний уровень', 'базовый уровень', 'указать уровни', 'редактировать', 'показать еще', 'показать ещё'}
                skill_names = [l for l in raw_lines if l.lower() not in ignore_markers and len(l) > 1]
            except Exception:
                pass

        # Убираем дубликаты с сохранением порядка
        seen = set()
        unique_skills = []
        for sn in skill_names:
            if sn.lower() not in seen:
                seen.add(sn.lower())
                unique_skills.append(sn)

        return {
            "success": True,
            "resume_id": self.resume_id,
            "position": position,
            "skills_count": len(unique_skills),
            "skills": unique_skills,
            "url": self.driver.current_url
        }

    def activate_and_save_all_skills(self) -> Tuple[bool, str, int]:
        """Проставляет уровни владения навыками и сохраняет.

        Страница уровней живёт по адресу /resume/edit/<id>/skillsLevels — именно
        так, camelCase с «s» в skills. Варианты /skills, /skillLevels,
        /skill-levels отдают 404.

        Клики — только настоящей мышью (ActionChains). JS-клик на вёрстке Magritte
        React не принимает: раньше из 30 навыков «активировалось» 11, потому что
        часть кликов уходила в пустоту.
        """
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.action_chains import ActionChains

        if getattr(self, '_user_closed', False):
            return False, "[СТОП] Браузер закрыт пользователем", 0
        if not self.is_driver_alive() and not self._init_driver():
            return False, "Не удалось инициализировать браузер", 0

        url = f"https://hh.ru/resume/edit/{self.resume_id}/skillsLevels"
        logger.info("Открываю редактор уровней владения навыками...")
        self.driver.get(url)
        time.sleep(3.5)

        if 'login' in (self.driver.current_url or '').lower():
            return False, "Требуется авторизация на hh.ru", 0

        skill_items = self.driver.find_elements(By.CSS_SELECTOR, '[data-qa="skill"]')
        if not skill_items:
            return False, "Навыки в редакторе уровней не обнаружены", 0

        logger.info(f"Навыков в редакторе уровней: {len(skill_items)}")
        # Раньше счётчик считал КЛИКИ, а не результат: клик мог промахнуться без
        # исключения (список пересобирается, кнопка уезжает, её перекрывает шапка),
        # и пользователь видел «проставлены уровни у 30 навыков» на любом исходе.
        # Теперь после клика перечитываем состояние кнопки.
        confirmed = 0      # состояние подтверждено разметкой
        no_signal = 0      # кликнули, но hh состояние не показывает
        clicked = 0

        for idx in range(len(skill_items)):
            try:
                # Элементы пересоздаются после каждого клика — берём заново по индексу.
                items = self.driver.find_elements(By.CSS_SELECTOR, '[data-qa="skill"]')
                if idx >= len(items):
                    break
                s = items[idx]

                try:
                    name = s.find_element(By.CSS_SELECTOR, '[data-qa="skillName"]').text.strip()
                except Exception:
                    name = ''
                if not name:
                    continue

                # Профильным — «Продвинутый», остальным «Средний»: завышать уровень
                # по всему списку не стоит, это проверяют на собеседовании.
                target_qa = ('skill-level-3' if name.lower() in ADVANCED_SKILLS
                             else 'skill-level-2')
                try:
                    btn = s.find_element(By.CSS_SELECTOR, f'[data-qa="{target_qa}"]')
                except Exception:
                    continue

                try:
                    class_before = btn.get_attribute('class')
                except Exception:
                    class_before = None

                self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
                time.sleep(0.2)
                ActionChains(self.driver).move_to_element(btn).pause(0.15).click().perform()
                time.sleep(0.35)
                clicked += 1

                # Перечитываем кнопку заново: после клика React пересобирает
                # список и прежняя ссылка на элемент уже протухла.
                state = None
                try:
                    items2 = self.driver.find_elements(By.CSS_SELECTOR, '[data-qa="skill"]')
                    if idx < len(items2):
                        state = self._level_button_state(
                            items2[idx].find_element(By.CSS_SELECTOR, f'[data-qa="{target_qa}"]'),
                            class_before)
                except Exception:
                    state = None

                if state is True:
                    confirmed += 1
                elif state is None:
                    no_signal += 1
                else:
                    logger.debug(f"Уровень не закрепился за навыком «{name}» — клик прошёл мимо")
            except Exception as e:
                logger.debug(f"Уровень не проставлен для навыка #{idx}: {e}")
                continue

        if confirmed:
            activated = confirmed
            level_note = ''
            if confirmed < clicked:
                logger.warning(
                    f"Нажатий по уровням: {clicked}, закрепилось: {confirmed}. "
                    "Остальные клики не дошли до страницы — повторите обновление резюме.")
        elif no_signal:
            # Разметка состояния не отдаёт вообще — врать про «проставлено» нельзя,
            # но и объявлять провал при прошедших кликах тоже неверно.
            activated = no_signal
            level_note = ' (hh не показал состояние кнопок, проверьте уровни глазами)'
            logger.warning(
                "Уровни нажаты, но hh не показывает их состояние в разметке — "
                "число ниже это количество нажатий, а не подтверждённых уровней")
        else:
            return False, (f"Ни один уровень не закрепился: из {clicked} нажатий "
                           "ни одно не дошло до страницы"), 0

        try:
            save = self.driver.find_element(By.CSS_SELECTOR, '[data-qa="resume-partial-edit-save"]')
        except Exception:
            return False, "Кнопка сохранения уровней не найдена", activated

        # Клик настоящей мышью: JS-клик React не принимал, страница оставалась
        # прежней, ошибок не показывала — и это засчитывалось за успех.
        if not self.real_click(save):
            return False, "Не удалось нажать «Сохранить» на странице уровней навыков", activated
        time.sleep(4.0)

        still_on_form = 'skillsLevels' in (self.driver.current_url or '')
        if still_on_form:
            err = (self.driver.execute_script("return document.body.innerText") or '')
            low = err.lower()
            if 'превышен' in low or 'ошибк' in low:
                return False, "hh не принял сохранение уровней", activated
            # Со страницы формы не ушли и ошибки нет — сохранение не подтверждено.
            return False, "hh не подтвердил сохранение уровней навыков", activated

        return True, f"Проставлены уровни у {activated} навыков{level_note}", activated

    @staticmethod
    def _level_button_state(btn, class_before: Optional[str] = None) -> Optional[bool]:
        """Выбран ли уровень: True/False, либо None — если разметка молчит.

        Три состояния, а не два: отличить «кнопка говорит, что не выбрана» от
        «кнопка ничего не говорит» нужно, чтобы не выдавать нажатия за результат.

        По именам классов Magritte не гадаем — сравниваем класс до и после клика.
        Угадывание маркера («active», «selected») вернуло бы ту же ложь: класс,
        который стоит всегда, засчитал бы промах за успех.
        """
        for attr in ('aria-checked', 'aria-pressed', 'aria-selected'):
            try:
                v = btn.get_attribute(attr)
            except Exception:
                continue
            if isinstance(v, str) and v.strip().lower() in ('true', 'false'):
                return v.strip().lower() == 'true'
        try:
            if btn.is_selected():
                return True
        except Exception:
            pass
        if isinstance(class_before, str) and class_before:
            try:
                cls_now = btn.get_attribute('class')
            except Exception:
                return None
            if isinstance(cls_now, str) and cls_now:
                return cls_now != class_before
        return None

    def add_skills_to_resume(self, new_skills: List[str]) -> Tuple[bool, str, List[str]]:
        """Добавляет навыки в резюме, отмечая чекбоксы рекомендаций hh.

        Почему только чекбоксы, а не ручной ввод: поле ввода — это combobox,
        Enter в нём вариант не выбирает, JS-клик по [role="option"] React не
        принимает, а после первого добавленного чипа оверлей списка перехватывает
        клики (ElementClickInterceptedException). Чекбоксы рекомендаций лишены
        всех этих проблем.

        Список рекомендаций hh пересобирает по содержимому резюме, поэтому идём
        раундами: отметить совпадения -> сохранить -> перезагрузить -> hh предложит
        новые. Останавливаемся, когда раунд не дал ничего нового.
        """
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.action_chains import ActionChains
        try:
            from ai_assistant import skill_is_covered
        except Exception:
            def skill_is_covered(kw, have):
                k = (kw or '').strip().lower()
                return any(k == (h or '').strip().lower() for h in have)

        if not new_skills:
            return True, "Список навыков для добавления пуст", []
        if not self.is_driver_alive() and not self._init_driver():
            return False, "Не удалось инициализировать браузер", []

        url = f"https://hh.ru/resume/edit/{self.resume_id}/keySkills"
        wanted = {}
        for s in new_skills:
            s = (s or '').strip()
            if s:
                wanted[s.lower()] = s

        added: List[str] = []
        MAX_ROUNDS = 6
        # Жёсткий лимит hh. Превышение — не предупреждение, а отказ сохранить:
        # форма показывает «Количество навыков превышено на N элементов» и
        # откатывается к последнему сохранённому состоянию, то есть теряется ВСЁ
        # добавленное за проход. Поэтому считаем заранее и не переступаем.
        HH_SKILL_LIMIT = 30

        # Порядок важен: при нехватке мест первыми идут профильные навыки.
        def priority(name: str) -> int:
            return 0 if (name or '').strip().lower() in ADVANCED_SKILLS else 1

        def selected_now():
            try:
                return {c.strip().lower() for c in self.driver.execute_script(
                    "return Array.from(document.querySelectorAll('[data-qa^=\"chips-trigger-chip-\"]'))"
                    ".map(e=>(e.innerText||'').trim()).filter(Boolean)"
                ) or []}
            except Exception:
                return set()

        try:
            for rnd in range(1, MAX_ROUNDS + 1):
                self.driver.get(url)
                time.sleep(3.0)

                if 'login' in (self.driver.current_url or '').lower():
                    return False, "Требуется авторизация на hh.ru", added

                try:
                    trigger = self.driver.find_element(By.CSS_SELECTOR, '[data-qa="chips-trigger-input"]')
                    trigger.click()
                    time.sleep(1.5)
                except Exception:
                    return False, "Редактор навыков не открылся", added

                already = selected_now()
                # Сравнение по нормализованному написанию: «SAST/DAST» и «SAST / DAST»,
                # «AppSec» и «Application Security», «Пентест» и «Penetration Testing» —
                # один и тот же навык. Без этого бот упирался в лимит 30, пытаясь
                # добавить то, что уже есть: 6 из 10 предложенных были дублями.
                already_list = list(already)
                try:
                    recommended = self.driver.execute_script(
                        "return Array.from(document.querySelectorAll("
                        "'[data-qa^=\"resume-editor-skills-recommended-\"]'))"
                        ".map(e=>e.getAttribute('data-qa')"
                        ".replace('resume-editor-skills-recommended-',''))"
                    ) or []
                except Exception:
                    recommended = []

                free_slots = HH_SKILL_LIMIT - len(already)
                if free_slots <= 0:
                    logger.warning(
                        f"В резюме уже {len(already)} навыков — это лимит hh ({HH_SKILL_LIMIT}). "
                        "Чтобы добавить новые, сначала удалите лишние."
                    )
                    break

                matches = [n for n in recommended
                           if n.strip().lower() in wanted
                           and not skill_is_covered(n, already_list)]
                matches.sort(key=priority)
                matches = matches[:free_slots]

                picked = []

                # То, чего hh не предлагает, вводим руками. Клик по варианту списка
                # обязан быть НАСТОЯЩИМ (ActionChains): JS-клик React не принимает,
                # а Enter в этом combobox вариант не выбирает.
                to_type = [v for k, v in wanted.items()
                           if not skill_is_covered(v, already_list)
                           and k not in {m.strip().lower() for m in matches}]
                to_type.sort(key=priority)
                to_type = to_type[:max(0, free_slots - len(matches))]
                for skill in to_type:
                    if len(selected_now()) >= HH_SKILL_LIMIT:
                        logger.info(f"Достигнут лимит hh в {HH_SKILL_LIMIT} навыков, остальные пропускаю")
                        break
                    try:
                        trg = self.driver.find_element(By.CSS_SELECTOR, '[data-qa="chips-trigger-input"]')
                        ActionChains(self.driver).move_to_element(trg).pause(0.2).click().perform()
                        time.sleep(1.0)
                        box = self.driver.find_element(By.CSS_SELECTOR, '[data-qa="chips-input-suggest-search"]')
                        box.send_keys(skill)
                        time.sleep(1.6)
                        opts = self.driver.find_elements(By.CSS_SELECTOR, '[role="option"]')
                        if not opts:
                            logger.debug(f"Нет вариантов для навыка: {skill}")
                            continue
                        ActionChains(self.driver).move_to_element(opts[0]).pause(0.2).click().perform()
                        time.sleep(1.0)
                        if skill.strip().lower() in selected_now():
                            picked.append(skill)
                    except Exception as e:
                        logger.debug(f"Не удалось ввести навык {skill}: {e}")
                        continue

                if not matches and not picked:
                    logger.info(f"Раунд {rnd}: добавить больше нечего, останавливаюсь")
                    break

                for name in matches:
                    try:
                        self.driver.execute_script(
                            "const e=document.querySelector("
                            "'[data-qa=\"resume-editor-skills-recommended-'+arguments[0]+'\"]');"
                            "if(e){(e.closest('label')||e.parentElement||e).click();}",
                            name
                        )
                        time.sleep(0.6)
                        if name.strip().lower() in selected_now():
                            picked.append(name)
                    except Exception as e:
                        logger.debug(f"Не удалось отметить {name}: {e}")

                if not picked:
                    logger.info(f"Раунд {rnd}: ни один чекбокс не принялся")
                    break

                try:
                    save = self.driver.find_element(By.CSS_SELECTOR, '[data-qa="resume-partial-edit-save"]')
                    # Настоящий клик: JS-клик React на вёрстке hh не принимает.
                    self.real_click(save)
                    time.sleep(3.5)
                except Exception as e:
                    return False, f"Навыки отмечены, но сохранить не удалось: {e}", added

                # hh отвергает сохранение молча, оставаясь на форме с красной ошибкой.
                # Без этой проверки бот рапортовал бы об успехе, потеряв все правки.
                try:
                    err = self.driver.execute_script(
                        "const t=document.body.innerText||'';"
                        "const m=t.match(/Количество навыков превышено[^\n]*/);"
                        "return m?m[0]:null;"
                    )
                except Exception:
                    err = None
                if err:
                    logger.error(f"hh не принял сохранение: {err}")
                    return False, f"hh не принял сохранение: {err}", added

                added.extend(picked)
                logger.info(f"Раунд {rnd}: добавлено {len(picked)} ({', '.join(picked)})")

            if not added:
                missing = ', '.join(sorted(wanted.values())[:5])
                return False, (f"Не удалось добавить ни один навык "
                               f"(например: {missing})"), []

            # Успех подтверждаем по самому резюме, а не по факту кликов.
            self.driver.get(f"https://hh.ru/resume/{self.resume_id}")
            time.sleep(3.0)
            # Сверяем с тегами навыков, а не с текстом всей страницы: подстрока
            # подтверждала «Go» словом «договор», а «AD» — словом «Град».
            tags = self.driver.execute_script(
                "return Array.from(document.querySelectorAll("
                "'[data-qa=\"skills-element\"], [data-qa=\"bloko-tag__text\"], "
                "[class*=\"bloko-tag__section\"]')).map(e => (e.innerText||'').trim());"
            ) or []
            tags_low = {' '.join(str(t).split()).lower() for t in tags if str(t).strip()}
            if tags_low:
                confirmed = [s for s in added if ' '.join(s.split()).lower() in tags_low]
            else:
                page = (self.driver.execute_script("return document.body.innerText") or '').lower()
                confirmed = [s for s in added if s.lower() in page]
            if not confirmed:
                return False, "Сохранение не подтвердилось: навыки в резюме не видны", added

            not_offered = [v for k, v in wanted.items()
                           if not skill_is_covered(v, confirmed)]
            msg = f"Добавлено навыков: {len(confirmed)}"
            if not_offered:
                msg += f". Не удалось добавить: {len(not_offered)}"
                logger.info(f"Не добавлены (hh не дал варианта): {', '.join(not_offered)}")
            return True, msg, confirmed

        except Exception as e:
            logger.error(f"Не удалось добавить навыки: {explain_error(e)}")
            logger.debug(f"Техническая причина: {e}")
            return False, f"Ошибка добавления навыков: {e}", added

    def _is_about_editor(self) -> bool:
        if not self.driver:
            return False
        try:
            url = urlsplit(str(self.driver.current_url))
            host = url.hostname or ''
            return (url.scheme == 'https' and url.port in (None, 443)
                    and (host == 'hh.ru' or host.endswith('.hh.ru'))
                    and url.path.rstrip('/') == f'/resume/edit/{self.resume_id}/about')
        except ValueError:
            return False

    def read_about_section(self) -> Optional[str]:
        """None означает, что редактор не прочитан, а не пустое «О себе»."""
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.common.exceptions import TimeoutException
        if not self.resume_id or getattr(self, '_user_closed', False):
            return None
        try:
            if not self.is_driver_alive() and not self._init_driver():
                return None
            url = f'https://hh.ru/resume/edit/{self.resume_id}/about'
            self.driver.set_page_load_timeout(30)
            try:
                self.driver.get(url)
            except TimeoutException:
                logger.info('Загрузка страницы не завершилась за 30 с; проверяю, доступна ли форма «О себе».')
            field = WebDriverWait(self.driver, 15).until(
                lambda d: next((e for e in d.find_elements(
                    By.CSS_SELECTOR, 'textarea[data-qa="resume-editor-about"]')
                    if e.is_displayed() and e.is_enabled()), None))
            if not self._is_about_editor():
                raise ValueError('Открылась не страница редактора выбранного резюме')
            return field.get_attribute('value') or ''
        except Exception as e:
            logger.warning('Не прочитан редактор «О себе»: %s. Проверка без сохранения: [P] → [V].', explain_error(e))
            try:
                directory = os.path.join(SCRIPT_DIR, '.resume_revisions')
                os.makedirs(directory, exist_ok=True)
                path = os.path.join(directory, datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '-editor')
                metadata = self.driver.execute_script("""
                    return {host: location.hostname, path: location.pathname, hasFragment: !!location.hash,
                        fields: [...document.querySelectorAll('textarea,input,[contenteditable]')]
                        .map(e => ({tag:e.tagName,qa:e.getAttribute('data-qa'),name:e.getAttribute('name')})),
                        editLinks:[...document.querySelectorAll('a[href*="/resume/"]')]
                        .map(e => new URL(e.href).pathname)};
                """)
                with open(path + '.json', 'w', encoding='utf-8') as f:
                    json.dump(metadata, f, ensure_ascii=False, indent=2)
                self.driver.save_screenshot(path + '.png')
                logger.warning('Диагностика редактора сохранена: %s', path)
            except Exception:
                logger.debug('Снимок редактора недоступен', exc_info=True)
            logger.debug('Сбой чтения редактора', exc_info=True)
            return None

    def about_draft_matches(self, expected: str) -> bool:
        from selenium.webdriver.common.by import By
        if not self._is_about_editor():
            return True
        fields = [e for e in self.driver.find_elements(By.CSS_SELECTOR, 'textarea[data-qa="resume-editor-about"]')
                  if e.is_displayed()]
        return len(fields) == 1 and fields[0].get_attribute('value') == expected

    def replace_about_section(self, text: str, expected_current: str,
                              feedback: Dict[str, Any]) -> Tuple[bool, str]:
        """Одна правка, резервная копия до ввода, успех только после чтения HH."""
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        from ai_assistant import clean_public_text
        self._about_changed = False
        if not isinstance(text, str) or not 100 <= len(text.strip()) <= 5000:
            return False, 'Новый текст «О себе» пустой или неподходящей длины'
        text = text.strip()
        if clean_public_text(text, feedback.get('profile')) != text:
            return False, 'В тексте остались зарплата или хронология работы'
        if not self.about_draft_matches(expected_current):
            return False, 'В редакторе появился другой текст; ручной черновик сохранён, запись отменена'
        current = self.read_about_section()
        if current is None:
            return False, 'Редактор целевого резюме не прочитан; ничего не записано'
        previous = load_resume_revision(self.resume_id)
        if previous.get('status') == 'pending':
            if current != previous.get('after'):
                return False, 'Предыдущее сохранение не подтверждено: проверьте «О себе» на HH и резервную копию'
            previous.update(status='verified', verified_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            save_resume_revision(previous)
            return True, 'Предыдущее сохранение подтверждено; новая правка отложена'
        if current != expected_current:
            return False, '«О себе» изменилось после анализа; свежий текст не перезаписываю'
        if current == text and previous.get('status') == 'verified':
            previous.update(selection_guidance=feedback.get('selection_guidance', previous.get('selection_guidance', [])),
                            reason=feedback.get('reason', previous.get('reason', '')))
            save_resume_revision(previous)
            return True, '«О себе» уже соответствует плану, менять не потребовалось'
        row = {
            'resume_id': self.resume_id, 'status': 'pending',
            'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'before': current, 'after': text,
            'target_title': feedback.get('target_title', ''),
            'selection_guidance': feedback.get('selection_guidance', []),
            'analysis_count': feedback.get('analysis_count', 0),
            'reason': feedback.get('reason', ''),
        }
        try:
            backup_dir = os.path.join(SCRIPT_DIR, '.resume_revisions')
            os.makedirs(backup_dir, exist_ok=True)
            backup = os.path.join(backup_dir, datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.json')
            with open(backup, 'x', encoding='utf-8') as f:
                json.dump(row, f, ensure_ascii=False, indent=2)
            row['backup'] = backup
            save_resume_revision(row)
            if current != text:
                field = self.driver.find_element(By.CSS_SELECTOR, 'textarea[data-qa="resume-editor-about"]')
                field.send_keys(Keys.CONTROL, 'a')
                field.send_keys(text)
                if field.get_attribute('value') != text:
                    row['status'] = 'not_sent'
                    save_resume_revision(row)
                    return False, 'В поле другой текст; сохранение отменено, резервная копия сохранена'
                button = self.driver.find_element(By.CSS_SELECTOR, '[data-qa="resume-partial-edit-save"]')
                if not self.real_click(button):
                    return False, 'Сохранение не подтверждено; повторный клик автоматически запрещён'
                time.sleep(2)
                if self.read_about_section() != text:
                    return False, 'HH не подтвердил точный текст после сохранения; резервная копия сохранена'
            row.update(status='verified', verified_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            save_resume_revision(row)
            self._about_changed = current != text
            return True, 'Текст «О себе» сохранён и перечитан на HH' if self._about_changed else 'Текст уже соответствует плану'
        except Exception as e:
            logger.debug('Правка «О себе» не подтверждена: %s', explain_error(e))
            return False, 'Правка «О себе» не подтверждена; проверьте редактор HH и локальную резервную копию'

    def update_about_section(self, deficit_skills: Optional[List[str]] = None) -> Tuple[bool, str]:
        """
        Ручной устаревший режим дополнения ключевыми словами.
        Автоматический разбор использует replace_about_section с текстом из профиля.
        """
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC

        if getattr(self, '_user_closed', False):
            return False, "[СТОП] Браузер закрыт пользователем"

        if not self.is_driver_alive() and not self._init_driver():
            return False, "Не удалось инициализировать браузер"

        url = f"https://hh.ru/resume/{self.resume_id}"
        logger.info("Открываю целевое резюме для обновления блока «Обо мне»...")
        self.driver.get(url)
        time.sleep(2.5)

        # Прокрутка страницы для загрузки динамических блоков
        try:
            self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight / 2);")
            time.sleep(0.8)
            self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(0.8)
            self.driver.execute_script("window.scrollTo(0, 0);")
            time.sleep(0.5)
        except Exception:
            pass

        # 1. Поиск кнопки редактирования "О себе"
        about_btn = None
        selectors = [
            '[data-qa="resume-edit-button-about"]',
            '[data-qa="resume-block-about"] [data-qa*="edit"]',
            '[data-qa="resume-block-about"] button',
            'div[data-qa*="about"] button',
            '[data-qa*="about-edit"]',
            'button[data-qa*="about"]',
            '//div[contains(@data-qa, "about")]//button',
            '//div[contains(@class, "resume-block") and .//*[contains(text(), "О себе") or contains(text(), "Обо мне")]]//button',
            '//span[contains(text(), "О себе") or contains(text(), "Обо мне")]/ancestor::div[contains(@class, "resume-block") or @data-qa]//button',
            '//button[contains(., "О себе") or contains(., "Добавить информацию о себе")]',
            '//a[contains(@href, "about") and contains(@href, "edit")]'
        ]
        for sel in selectors:
            try:
                if sel.startswith('//'):
                    found = self.driver.find_elements(By.XPATH, sel)
                else:
                    found = self.driver.find_elements(By.CSS_SELECTOR, sel)
                for f in found:
                    if f.is_displayed():
                        about_btn = f
                        break
                if about_btn:
                    break
            except Exception:
                continue

        if not about_btn:
            try:
                headers = self.driver.find_elements(By.XPATH, "//*[contains(text(), 'О себе') or contains(text(), 'Обо мне')]")
                for h in headers:
                    if h.is_displayed():
                        self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", h)
                        time.sleep(0.5)
                        parent = h.find_element(By.XPATH, "./ancestor::div[contains(@class, 'resume-block') or contains(@data-qa, 'resume')]")
                        btns = parent.find_elements(By.TAG_NAME, "button")
                        for b in btns:
                            if b.is_displayed():
                                about_btn = b
                                break
                    if about_btn:
                        break
            except Exception:
                pass

        # Резервный поиск: нажать главную кнопку "Редактировать" под заголовком резюме
        if not about_btn:
            try:
                for sel in [
                    '[data-qa="resume-edit-button"]',
                    '//a[contains(., "Редактировать")]',
                    '//button[contains(., "Редактировать")]'
                ]:
                    if sel.startswith('//'):
                        found = self.driver.find_elements(By.XPATH, sel)
                    else:
                        found = self.driver.find_elements(By.CSS_SELECTOR, sel)
                    for f in found:
                        if f.is_displayed():
                            about_btn = f
                            break
                    if about_btn:
                        break
            except Exception:
                pass

        if not about_btn:
            for sel in selectors:
                try:
                    if not sel.startswith('//'):
                        el = self.driver.find_element(By.CSS_SELECTOR, sel)
                        if el and getattr(el, 'is_displayed', lambda: True)():
                            about_btn = el
                            break
                except Exception:
                    continue

        if not about_btn:
            try:
                body_text = self.driver.find_element(By.TAG_NAME, "body").text.lower()
                if all(m in body_text for m in ['криптопро', 'скзи', 'гост', 'active directory', 'maxpatrol']):
                    return True, "Блок «Обо мне» уже содержит все ключевые стандарты"
            except Exception:
                pass
            return False, "Кнопка редактирования раздела «О себе» не найдена"

        if not self.real_click(about_btn):
            return False, "Не удалось открыть раздел «О себе» на странице резюме"
        time.sleep(2)

        # 2. Поиск текстового поля
        textarea = None
        # Фолбэка на «любую textarea» здесь быть не должно: если модалка «О себе»
        # не открылась, текст уехал бы в первое попавшееся поле резюме.
        ta_selectors = [
            'textarea[data-qa="resume-editor-about"]',
            'textarea[data-qa*="about"]',
            'textarea[name*="about"]',
        ]
        for sel in ta_selectors:
            try:
                found = self.driver.find_elements(By.CSS_SELECTOR, sel)
                for f in found:
                    if f.is_displayed():
                        textarea = f
                        break
                if textarea:
                    break
            except Exception:
                continue

        if not textarea:
            for sel in ta_selectors:
                try:
                    el = self.driver.find_element(By.CSS_SELECTOR, sel)
                    if el:
                        textarea = el
                        break
                except Exception:
                    continue

        if not textarea:
            return False, "Текстовое поле раздела «О себе» не найдено"

        try:
            curr_text = textarea.get_attribute("value") or ""

            # Проверяем наличие ключевых Enterprise/ATS терминов
            needed_markers = ['криптопро', 'скзи', 'гост', 'active directory', 'maxpatrol']
            has_all = all(m in curr_text.lower() for m in needed_markers)
            if has_all:
                # Ничего не меняли — так и говорим. Раньше это возвращалось как успех
                # обновления, и цикл рапортовал «резюме успешно обновлено».
                self._about_changed = False
                logger.info("Блок «О себе» уже содержит нужные ключевые слова, правка не требуется")
                try:
                    cancel_btn = self.driver.find_element(By.CSS_SELECTOR, '[data-qa="resume-partial-edit-cancel"]')
                    self.real_click(cancel_btn)
                except Exception:
                    pass
                return True, "Блок «О себе» в порядке, менять не потребовалось"

            ats_block = (
                "\n\nДополнительные компетенции и стандарты (ATS / Enterprise):\n"
                "• Требования регуляторов и криптозащита: СКЗИ, КриптоПро, соответствие ГОСТ и стандартам информационной безопасности.\n"
                "• Безопасность инфраструктуры: Active Directory, MaxPatrol SIEM, KUMA, аудит инцидентов и сетевой безопасности."
            )

            textarea.send_keys(ats_block)
            time.sleep(1)

            save_btn = self.driver.find_element(
                By.CSS_SELECTOR, '[data-qa="resume-partial-edit-save"], button[type="submit"]')
            if not self.real_click(save_btn):
                return False, "Не удалось нажать «Сохранить» в разделе «О себе»"
            time.sleep(3)

            # Успех подтверждаем чтением поля, а не фактом клика: JS-клик по
            # «Сохранить» React не принимал, а лог писал «успешно сохранено».
            marker = 'Дополнительные компетенции и стандарты'
            try:
                self.driver.get(f"https://hh.ru/resume/edit/{self.resume_id}/about")
                time.sleep(2.5)
                saved = self.driver.execute_script(
                    "const e=document.querySelector('[data-qa=\"resume-editor-about\"]');"
                    "return e ? (e.value || '') : null;")
            except Exception as e:
                logger.debug(f"Не удалось перечитать «О себе»: {e}")
                saved = None

            if saved is None:
                return False, "Сохранение не подтвердилось: раздел «О себе» не перечитался"
            if marker not in saved:
                return False, "hh не сохранил изменения в разделе «О себе»"

            logger.info("Раздел «О себе» дополнен и сохранение подтверждено")
            return True, "Раздел «О себе» дополнен, сохранение подтверждено"
        except Exception as e:
            logger.debug(f"Техническая причина сбоя блока «О себе»: {e}")
            return False, f"Не удалось обновить блок «О себе»: {explain_error(e)}"

    def sync_adaptive_skills(self, skills: Optional[List[str]] = None) -> Tuple[bool, str, int]:
        """
        Синхронизирует адаптивные навыки из базы отказов с резюме:
        добавляет новые недостающие навыки, выставляет им уровни и сохраняет резюме.
        """
        from db_manager import DatabaseManager
        db = DatabaseManager()

        target_skills = list(skills) if skills else []
        if not target_skills:
            target_skills = db.get_adaptive_skills()

        # Результаты двух разных шагов складывались в один флаг, и вызывающий не
        # мог отличить «ничего не добавилось» от «добавилось». Кладём их отдельно:
        # _skills_added_count — сколько тегов реально добавлено,
        # _levels_ok — проставились ли уровни владения.
        self._skills_added_count = 0
        self._levels_ok = False

        if not target_skills:
            logger.info("В базе нет адаптивных навыков для добавления")
            ok_only, msg_only, count_only = self.activate_and_save_all_skills()
            self._levels_ok = ok_only
            return ok_only, msg_only, count_only

        logger.info(f"Начало синхронизации {len(target_skills)} адаптивных навыков с резюме...")
        ok, msg, added = self.add_skills_to_resume(target_skills)
        if not ok:
            logger.warning(f"Добавление навыков: {msg}")
        else:
            self._skills_added_count = len(added)

        # Активируем уровни для всех навыков (старых и новых)
        ok_act, msg_act, count = self.activate_and_save_all_skills()
        self._levels_ok = ok_act
        # Итог берём от ОБОИХ шагов. Раньше он брался только от простановки
        # уровней: «навыки не добавились» плюс «уровни проставились» давало успех.
        if not ok:
            return False, f"Навыки не добавились: {msg}", count
        return ok_act, f"Добавлено новых тегов: {len(added)}. Всего навыков в резюме: {count}", count

    def apply_full_modernization(self, deficit_skills: Optional[List[str]] = None,
                                 profile_only: bool = False,
                                 profile: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Полный комплекс модернизации резюме:
        1. Внедрение дефицитных навыков из анализа отказов (с умной заменой низкоприоритетных при лимите 30).
        2. Активация и подтверждение уровней владения (Advanced/Intermediate).
        3. Обогащение блока «Обо мне» регламентами и Enterprise-стандартами.

        profile_only=True — режим без вопроса пользователю (автоправка после
        разбора отказов). Правку никто не видит до сохранения, поэтому пишется
        только то, что подтверждено профилем кандидата (profile, по умолчанию
        candidate_profile из настроек):
          - навыки — только найденные в профиле, остальные возвращаются в
            recommended_only как «стоит изучить или подтвердить»;
          - база адаптивных навыков не подмешивается: там навыки из отказов,
            то есть как раз то, чего у кандидата может не быть;
          - уровни владения не трогаются: «Продвинутый» по списку ADVANCED_SKILLS —
            утверждение о кандидате, которого в профиле нет, и клики по уровням
            перезаписали бы то, что пользователь выставил сам;
          - «О себе» не трогается: update_about_section дописывает заготовленный
            абзац про СКЗИ, ГОСТ, MaxPatrol и «аудит инцидентов», одинаковый для
            всех, а не текст из профиля.
        """
        if profile_only:
            return self._apply_profile_only(deficit_skills, profile)
        if getattr(self, '_user_closed', False):
            return {
                'skills_updated': False,
                'skills_message': '[СТОП] Браузер закрыт пользователем',
                'skills_count': 0,
                'skills_added_count': 0,
                'levels_ok': False,
                'about_updated': False,
                'about_changed': False,
                'about_message': '[СТОП] Браузер закрыт пользователем'
            }
        results = {}
        logger.info("=== Запуск комплексной модернизации резюме на HeadHunter ===")

        # 1. Синхронизация навыков
        self._skills_added_count = 0
        self._levels_ok = False
        ok_skills, msg_skills, count = self.sync_adaptive_skills(deficit_skills)
        results['skills_updated'] = ok_skills
        results['skills_message'] = msg_skills
        results['skills_count'] = count
        # skills_updated приходил с шага простановки уровней, а вызывающие читали его
        # как «навыки добавлены» и печатали «Резюме обновлено» при нуле добавленных.
        # Держим два отдельных ключа: сколько добавлено и проставились ли уровни.
        results['skills_added_count'] = int(getattr(self, '_skills_added_count', 0) or 0)
        results['levels_ok'] = bool(getattr(self, '_levels_ok', False))

        if getattr(self, '_user_closed', False):
            results['about_updated'] = False
            results['about_changed'] = False
            results['about_message'] = '[СТОП] Браузер закрыт пользователем'
            return results

        # 2. Обновление блока «Обо мне»
        self._about_changed = True   # сбрасывается внутри, если правка не потребовалась
        ok_about, msg_about = self.update_about_section(deficit_skills)
        results['about_updated'] = ok_about
        results['about_message'] = msg_about
        # Отдельно от about_updated: «получилось» и «что-то изменилось» — разные вещи.
        results['about_changed'] = ok_about and getattr(self, '_about_changed', True)

        # Было «Навыки=True (30), Обо мне=False» — питоновские значения в русской
        # строке. Пишем то же самое словами и по фактическим числам.
        added_n = results['skills_added_count']
        logger.info(
            "Модернизация резюме завершена. "
            f"Новых навыков добавлено: {added_n}. "
            f"Уровни владения: {'проставлены' if results['levels_ok'] else 'не проставлены'}. "
            f"Навыков в резюме: {count}. "
            f"Раздел «О себе»: {'дополнен' if results['about_changed'] else 'без изменений'}."
        )
        return results

    def _apply_profile_only(self, deficit_skills: Optional[List[str]],
                            profile: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Автоправка резюме: только навыки, подтверждённые профилем. См. apply_full_modernization."""
        if profile is None:
            try:
                from config_manager import load_config
                profile = (load_config() or {}).get('candidate_profile') or {}
            except Exception as e:
                logger.debug(f"Профиль кандидата не прочитан: {e}")
                profile = {}
        grounded, advice = profile_grounded_skills(deficit_skills, profile)
        results: Dict[str, Any] = {
            'skills_updated': True,
            'skills_message': 'Добавлять нечего: ни один недостающий навык не подтверждён профилем',
            'skills_count': 0,
            'skills_added_count': 0,
            'skills_added': [],
            'recommended_only': advice,
            'levels_ok': False,
            'about_updated': False,
            'about_changed': False,
            'about_message': 'не менялся: в автоматическом режиме этот раздел правите только вы',
        }
        if getattr(self, '_user_closed', False):
            results.update(skills_updated=False, skills_message='[СТОП] Браузер закрыт пользователем')
            return results
        if grounded:
            # Лимит hh в 30 навыков соблюдает сам add_skills_to_resume.
            ok, msg, added = self.add_skills_to_resume(grounded)
            results['skills_updated'] = ok
            results['skills_message'] = msg
            if ok:
                results['skills_added'] = list(added)
                results['skills_added_count'] = len(added)
        logger.info(
            "Автоправка резюме: добавлено навыков из профиля — "
            f"{results['skills_added_count']}, только рекомендация — {len(advice)}. "
            "Уровни и «О себе» не менялись."
        )
        return results

    # Факторы ранжирования резюме в поиске работодателей (feedback.hh.ru/knowledge-base/article/1116):
    # опыт, навыки, заголовок, содержание, зарплата, геолокация и поведение работодателей.
    # Практикой добавляются свежесть, полнота заполнения и статус поиска. Автоматизируемы
    # из них: свежесть (поднятие раз в 4 часа), видимость, статус и полнота блоков.
    def real_click(self, element) -> bool:
        """Клик настоящей мышью с прокруткой. JS-клик React на hh не принимает.

        Возвращает False, если кликнуть не удалось — раньше такие промахи
        тонули в execute_script, который не бросает исключение, и код рапортовал
        об успехе на несохранённой правке резюме.
        """
        from selenium.webdriver.common.action_chains import ActionChains
        try:
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'});", element)
            time.sleep(0.3)
        except Exception:
            pass
        try:
            ActionChains(self.driver).move_to_element(element).pause(0.15).click().perform()
            return True
        except Exception:
            pass
        try:
            element.click()
            return True
        except Exception as e:
            logger.debug(f"Клик не прошёл: {e}")
            return False

    def _open_resume_page(self):
        from selenium.common.exceptions import TimeoutException, StaleElementReferenceException
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        try:
            self.driver.get(f'https://hh.ru/resume/{self.resume_id}')
        except TimeoutException:
            # Тайм-аут ресурсов не означает, что сама карточка резюме недоступна.
            def ready(driver):
                url = urlsplit(str(driver.current_url))
                host = url.hostname or ''
                if not (url.scheme == 'https' and url.port in (None, 443)
                        and (host == 'hh.ru' or host.endswith('.hh.ru'))
                        and url.path.rstrip('/') == f'/resume/{self.resume_id}'):
                    raise ValueError('Открылась не страница выбранного резюме')
                return any(e.is_displayed() and e.text.strip() for e in driver.find_elements(
                    By.CSS_SELECTOR, '[data-qa="resume-block-title-position"]'))
            WebDriverWait(self.driver, 10, poll_frequency=0.2,
                          ignored_exceptions=(StaleElementReferenceException,)).until(ready)
            logger.info('Загрузка ресурсов превысила тайм-аут, но выбранное резюме доступно; продолжаю.')

    def promote_resume(self) -> Dict[str, Any]:
        """Продвижение резюме: видимость -> статус поиска -> поднятие -> отчёт о полноте.

        Поднятие работает ТОЛЬКО для опубликованных резюме с видимостью
        «Видно всем работодателям», поэтому её проверяем до нажатия кнопки:
        иначе бот рапортует об успехе, а позиция не меняется.
        """
        result = {'visible': None, 'status': None, 'bumped': False,
                  'bump_message': '', 'gaps': [], 'next_bump_at': None,
                  'auto_bump_is_paid': False}

        if not self.is_driver_alive() and not self._init_driver():
            result['bump_message'] = 'Не удалось инициализировать браузер'
            return result

        from selenium.webdriver.common.by import By
        try:
            self._open_resume_page()
            time.sleep(3.0)
            page = (self.driver.execute_script("return document.body.innerText") or '')
            low = page.lower()

            # Видимость: скрытое резюме не показывается работодателям вообще.
            if 'виден всем работодателям' in low or 'видно всем работодателям' in low:
                result['visible'] = True
            elif 'скрыт' in low and 'резюме' in low:
                result['visible'] = False
            logger.info(f"Видимость резюме: {VISIBILITY_TEXT[result['visible']]}")

            # Полнота: пустые блоки бьют и по ранжированию, и по отбору у работодателя.
            # Заголовок блока навыков на странице — «Навыки», НЕ «Ключевые навыки»;
            # по неверной строке проверка сообщала о пустом блоке при заполненном.
            for block, human in (('навыки', 'Навыки'),
                                 ('опыт работы', 'Опыт работы')):
                if block not in low:
                    result['gaps'].append(human)

            # «О себе» на публичной странице резюме выводится без заголовка, поэтому
            # искать его там бесполезно — проверка всегда кричала о пустоте при
            # заполненном блоке. Читаем поле прямо в редакторе.
            try:
                about_text = self.read_about_section()
                if about_text is not None and len(about_text.strip()) < 100:
                    result['gaps'].append('О себе')
            except Exception as e:
                logger.debug(f"Не удалось проверить блок «О себе»: {e}")
            if result['gaps']:
                logger.warning(f"Резюме заполнено не полностью, пустые блоки: {', '.join(result['gaps'])}")

            # Сверяем стаж в конфиге с тем, что стоит в резюме. Поле заполняется
            # руками и устаревает: стояло 3 года при реальных 6 годах 9 месяцах,
            # и фильтр по грейду резал вакансии, доступные кандидату.
            try:
                import re as _re2
                m_exp = _re2.search(
                    r'Опыт работы[:\s—-]*(?:(\d+)\s*(?:год|года|лет))?\s*(?:(\d+)\s*мес)?', page)
                if m_exp and (m_exp.group(1) or m_exp.group(2)):
                    real = round(int(m_exp.group(1) or 0) + int(m_exp.group(2) or 0) / 12.0, 2)
                    # У HHResumeUpdater нет атрибута config — из-за hasattr проверка
                    # стажа никогда не выполнялась. Читаем настройки явно.
                    try:
                        from config_manager import load_config as _load_cfg
                        prof = (_load_cfg().get('candidate_profile') or {})
                    except Exception:
                        prof = {}
                    stated = prof.get('experience_years')
                    if stated is not None and abs(float(stated) - real) >= 1:
                        logger.warning(
                            f"Стаж в настройках ({stated}) расходится с резюме ({real}). "
                            "Из-за этого фильтр вакансий работает по неверной планке — "
                            "поправьте стаж в настройках бота."
                        )
                    result['experience_years'] = real
            except Exception as e:
                logger.debug(f"Не удалось сверить стаж: {e}")

            # hh пишет прямо на странице, когда поднятие снова доступно
            # («Можно сегодня в 17:46», после 20:00 — «Можно завтра в 02:31»).
            import re as _re
            m = _re.search(r'Можно (?:сегодня|завтра) в\s*(\d{1,2}:\d{2})', page)
            result['next_bump_at'] = m.group(1) if m else None

            # Кнопка «Поднимать автоматически» ведёт на /applicant-services/hhpro —
            # это платная подписка hh Pro, бесплатно её использовать нельзя.
            result['auto_bump_is_paid'] = 'поднимать автоматически' in low

            ok, msg = self.bump_resume()
            result['bumped'] = ok
            result['bump_message'] = msg
            return result

        except Exception as e:
            # В консоль — человеческая формулировка, техника остаётся в журнале.
            logger.error(f"Не удалось продвинуть резюме: {explain_error(e)}")
            logger.debug(f"Техническая причина: {e}")
            result['bump_message'] = f"Не получилось: {explain_error(e)}"
            return result

    def watch_and_bump(self, every_hours: float = 5.0, max_cycles: int = 0) -> None:
        """Периодически поднимает резюме в поиске.

        hh даёт поднимать бесплатно раз в 4 часа. Кнопка «Поднимать автоматически»
        на странице резюме ведёт на /applicant-services/hhpro — это платная подписка
        hh Pro, поэтому бесплатный путь один: заходить и нажимать самим.

        Свежесть — главный рычаг позиции резюме в поиске работодателей, так что
        смысл гонять цикл есть. Интервал по умолчанию 5 часов: с запасом над
        четырёхчасовым кулдауном, чтобы не упираться в него впустую.
        """
        every_seconds = max(1.0, float(every_hours)) * 3600
        cycle = 0

        print('')
        print(f"{'='*70}")
        print(f"{RED}{BOLD}АВТОПОДНЯТИЕ РЕЗЮМЕ В ПОИСКЕ{RESET}")
        print(f"{'='*70}")
        print(f"  Интервал: каждые {every_hours} ч (кулдаун hh — 4 ч)")
        print("  Остановка: Ctrl+C")
        print(f"{'='*70}")
        print('')

        try:
            while True:
                cycle += 1
                stamp = datetime.now().strftime('%H:%M:%S')
                try:
                    ok, msg = self.bump_resume()
                    tag = "[OK]" if ok else "[i]"
                    print(f"  {stamp}  цикл {cycle}: {tag} {msg}")
                    logger.info(f"Автоподнятие, цикл {cycle}: {msg}")
                except Exception as e:
                    # Раз в несколько часов в консоль летел сырой питоновский
                    # текст исключения — заменено человеческим объяснением.
                    print(f"  {stamp}  цикл {cycle}: [X] {explain_error(e)}")
                    logger.debug(f"Автоподнятие, техническая причина в цикле {cycle}: {e}")

                # Драйвер между циклами держать незачем: браузер, висящий часами,
                # блокирует chrome_profile для остальных пунктов меню.
                try:
                    self.close()
                except Exception:
                    pass

                if max_cycles and cycle >= max_cycles:
                    print(f"\n  Выполнено циклов: {cycle}. Завершаю.")
                    return

                nxt = datetime.now() + timedelta(seconds=every_seconds)
                print(f"           следующее поднятие в {nxt.strftime('%H:%M')}")
                time.sleep(every_seconds)

        except KeyboardInterrupt:
            print(f"\n  Остановлено пользователем после {cycle} циклов.\n")

    BUMP_SELECTORS = [
        '[data-qa="resume-update-button"]',
        '[data-qa*="resume-update"]',
        '//button[contains(., "Поднять в поиске")]',
        '//button[contains(., "Обновить дату")]',
        '//span[contains(., "Поднять в поиске")]/ancestor::button',
        '//span[contains(., "Обновить дату")]/ancestor::button',
    ]

    def read_resume_page(self) -> Dict[str, Any]:
        """Данные со страницы своего резюме для профиля кандидата. {} — не прочиталось."""
        info = self.get_current_resume_status()   # открывает резюме и раскрывает навыки
        if not info.get('success'):
            return {}
        try:
            return self.driver.execute_script(RESUME_PAGE_SCRIPT) or {}
        except Exception as e:
            logger.debug(f"Страница резюме не разобралась: {e}")
            return {}

    def _bump_button_present(self) -> bool:
        """Осталась ли кнопка поднятия на странице (значит, клик не сработал)."""
        from selenium.webdriver.common.by import By
        for sel in self.BUMP_SELECTORS:
            try:
                by = By.XPATH if sel.startswith('//') else By.CSS_SELECTOR
                for el in self.driver.find_elements(by, sel):
                    if el.is_displayed():
                        return True
            except Exception:
                continue
        return False

    def bump_resume(self) -> Tuple[bool, str]:
        """Поднимает резюме и записывает, когда пробовать в следующий раз.

        Расписание общее для полного цикла и прогона откликов: прогон сверяется
        с ним перед каждой вакансией, чтобы поднять резюме, как только hh
        разрешит, а не раз за запуск.
        """
        if not str(self.resume_id or '').strip() or str(self.resume_id).startswith('YOUR_'):
            # Без резюме бот открывал пустую страницу и писал «кнопка не найдена».
            return False, 'Резюме не выбрано — выберите его: [P] → [R]'
        ok, msg = self._bump_resume_once()
        try:
            record_next_bump(ok, msg)
        except Exception as e:
            logger.debug(f"Расписание поднятия не записано: {e}")
        return ok, msg

    def _bump_resume_once(self) -> Tuple[bool, str]:
        """
        Бесплатное поднятие резюме в поиске на HeadHunter («Поднять в поиске» / «Обновить дату»).
        Доступно каждые 4 часа. Удерживает резюме на верхних позициях выдачи у работодателей.
        """
        if getattr(self, '_user_closed', False):
            return False, "[СТОП] Браузер закрыт пользователем"
        if not self._init_driver():
            return False, "Не удалось инициализировать браузер для поднятия резюме"

        from selenium.webdriver.common.by import By
        logger.info("Переход в целевое резюме для поднятия в поиске...")

        try:
            self._open_resume_page()
            time.sleep(2.0)

            # Ищем кнопку "Поднять в поиске" / "Обновить дату"
            # Широких фолбэков вроде 'button[data-qa*="resume"]' здесь быть не должно:
            # на странице резюме им отвечают и resume-delete, и resume-hide. Цикл берет
            # первый видимый элемент, так что промах первых селекторов (hh переименовал
            # кнопку) означал бы клик по удалению или снятию анкеты с публикации.
            bump_selectors = self.BUMP_SELECTORS
            # Подстраховка на случай, если селектор всё же зацепит не ту кнопку.
            FORBIDDEN = ('удалить', 'скрыть', 'снять с публикации', 'delete', 'remove')
            EXPECTED = ('поднять', 'обновить')

            bump_btn = None
            for sel in bump_selectors:
                try:
                    if sel.startswith('//'):
                        elements = self.driver.find_elements(By.XPATH, sel)
                    else:
                        elements = self.driver.find_elements(By.CSS_SELECTOR, sel)
                    for el in elements:
                        if not el.is_displayed():
                            continue
                        label = (el.text or '').strip().lower()
                        if any(bad in label for bad in FORBIDDEN):
                            logger.warning(f"Пропускаю кнопку «{el.text.strip()}» — это не поднятие резюме")
                            continue
                        if label and not any(ok in label for ok in EXPECTED):
                            logger.debug(f"Кнопка «{label[:40]}» не похожа на поднятие, пропускаю")
                            continue
                        bump_btn = el
                        break
                    if bump_btn:
                        break
                except Exception:
                    continue

            # Проверяем таймер кулдауна
            cooldown_selectors = [
                '[data-qa="resume-update-button-cooldown"]',
                '[class*="cooldown"]',
                '//*[contains(text(), "Поднять можно через")]',
                '//*[contains(text(), "можно поднять через")]',
                '//*[contains(text(), "Обновить можно через")]'
            ]
            cooldown_text = ""
            for c_sel in cooldown_selectors:
                try:
                    if c_sel.startswith('//'):
                        c_elems = self.driver.find_elements(By.XPATH, c_sel)
                    else:
                        c_elems = self.driver.find_elements(By.CSS_SELECTOR, c_sel)
                    for ce in c_elems:
                        if ce.is_displayed() and ce.text.strip():
                            cooldown_text = ce.text.strip()
                            break
                    if cooldown_text:
                        break
                except Exception:
                    continue

            if bump_btn:
                btn_text = bump_btn.text.strip()
                is_disabled = bump_btn.get_attribute('disabled') or 'disabled' in (bump_btn.get_attribute('class') or '')
                if is_disabled:
                    msg = f"Кнопка поднятия резюме неактивна. {cooldown_text or 'Повторите попытку позже.'}"
                    logger.info(f" [i] {msg}")
                    return False, msg

                # Кликаем на кнопку. Фолбэком идёт ActionChains, а не JS-клик:
                # React на вёрстке hh синтетический клик не принимает, и прежний
                # фолбэк молча ничего не делал, а лог рапортовал об успехе.
                try:
                    self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", bump_btn)
                    time.sleep(0.5)
                    bump_btn.click()
                except Exception:
                    from selenium.webdriver.common.action_chains import ActionChains
                    ActionChains(self.driver).move_to_element(bump_btn).pause(0.15).click().perform()

                time.sleep(2.5)

                # Проверяем, что поднятие действительно засчиталось: hh убирает
                # кнопку и пишет, когда поднять можно снова.
                after = (self.driver.execute_script("return document.body.innerText") or '')
                confirmed = ('Можно сегодня в' in after or 'Можно завтра в' in after
                             or 'можно поднять через' in after.lower()
                             or not self._bump_button_present())
                if not confirmed:
                    msg = "Кнопка нажата, но hh не подтвердил поднятие — позиция резюме не изменилась"
                    logger.warning(msg)
                    return False, msg

                logger.info(f" [OK] Резюме успешно поднято в поиске («{btn_text}»)")
                return True, f"Резюме успешно поднято в поиске: {btn_text}"

            if cooldown_text:
                msg = f"Резюме уже поднято недавно: {cooldown_text}"
                logger.info(f" [i] {msg}")
                return False, msg

            # Раньше здесь стояла догадка «возможно, резюме не опубликовано или ещё
            # на кулдауне». hh пишет причину прямо на странице — читаем её, а не гадаем.
            page_text = ''
            try:
                page_text = self.driver.execute_script("return document.body.innerText") or ''
            except Exception:
                pass
            low_page = page_text.lower()

            import re as _re3
            # 28.09: после 20:00 hh пишет «Можно завтра в 02:31» — это не ловилось,
            # бот писал «кнопка не найдена» и дёргал страницу резюме каждые 30 минут.
            m_when = _re3.search(r'Можно (сегодня|завтра) в\s*(\d{1,2}:\d{2})', page_text)
            if m_when:
                msg = f"Резюме уже поднято, следующее бесплатное поднятие — {m_when.group(1)} в {m_when.group(2)}"
                logger.info(f" [i] {msg}")
                return False, msg
            if 'скрыт' in low_page or 'не опубликован' in low_page:
                msg = "Резюме скрыто или не опубликовано — поднимать нечего"
                logger.warning(msg)
                return False, msg
            if 'войти' in low_page and 'пароль' in low_page:
                msg = "Требуется вход на hh.ru — страница резюме не открылась"
                logger.warning(msg)
                return False, msg

            msg = "Кнопка «Поднять в поиске» на странице резюме не найдена"
            logger.warning(msg)
            return False, msg

        except Exception as e:
            logger.debug(f"Техническая причина сбоя поднятия: {e}")
            err = f"Не удалось поднять резюме в поиске: {explain_error(e)}"
            logger.error(err)
            return False, err


def run_resume_updater_cli():
    """CLI интерфейс обновления резюме."""
    import argparse
    parser = argparse.ArgumentParser(description="Автоматическое обновление навыков и параметров резюме на HH.ru")
    parser.add_argument("--resume-id", default=DEFAULT_RESUME_ID, help="ID резюме на hh.ru")
    parser.add_argument("--status", action="store_true", help="Проверить текущее состояние резюме")
    parser.add_argument("--check-about", action="store_true", help="Прочитать редактор «О себе» без сохранения")
    parser.add_argument("--show-browser", action="store_true", help="Показать окно браузера")
    parser.add_argument("--apply", action="store_true", help="Автоматически активировать и сохранить все навыки")
    parser.add_argument("--sync-adaptive", action="store_true", help="Внедрить адаптивные навыки из базы отказов")
    parser.add_argument("--full-update", action="store_true", help="Комплексная модернизация (навыки, уровни, блок Обо мне)")
    parser.add_argument("--bump", action="store_true", help="Поднять резюме в поиске (обновить дату публикации)")
    parser.add_argument("--watch-bump", action="store_true",
                        help="Цикл: поднимать резюме каждые N часов (по умолчанию 5)")
    parser.add_argument("--every-hours", type=float, default=5.0,
                        help="Интервал автоподнятия в часах")
    parser.add_argument("--promote", action="store_true",
                        help="Продвижение: проверить видимость и полноту, затем поднять резюме")
    parser.add_argument("--import-profile", action="store_true",
                        help="Заполнить профиль кандидата из резюме на hh")
    parser.add_argument("--dry-run", action="store_true", help="Показать, не сохраняя")
    parser.add_argument("--headless", action="store_true", default=True, help="Запуск в фоновом режиме")

    args = parser.parse_args()
    updater = HHResumeUpdater(resume_id=args.resume_id, headless=args.headless and not args.show_browser)

    try:
        if args.check_about:
            about = updater.read_about_section()
            print(f'[OK] Редактор «О себе» прочитан: {len(about)} символов. Ничего не сохранено.'
                  if about is not None else '[X] Редактор недоступен. Проверьте вход [N] → [L] и выбранное резюме [P] → [R].')
            return
        if args.watch_bump:
            updater.watch_and_bump(every_hours=args.every_hours)
            return

        if args.import_profile:
            print("\n" + "="*70)
            print(f"{RED}{BOLD}ПРОФИЛЬ КАНДИДАТА ИЗ РЕЗЮМЕ HH.RU{RESET}")
            print("="*70)
            page = updater.read_resume_page()
            if not page.get('title') and not page.get('skills'):
                print("[X] Резюме не прочиталось. Проверьте вход на hh.ru ([N] → [L]) и выбранное резюме ([P] → [R]).")
                print("="*70 + "\n")
                return
            from config_manager import load_config, save_config
            cfg = load_config()
            profile = merge_resume_into_profile(cfg.get('candidate_profile') or {}, page)
            print(f"  Должность:    {profile.get('specialization', '')}")
            print(f"  Опыт:         {profile.get('experience_years', '—')} лет, мест работы: "
                  f"{len(profile.get('experience_highlights') or [])}")
            print(f"  Навыков:      {len(profile.get('skills') or [])}")
            print(f"  «О себе»:     {'есть' if profile.get('about') else 'нет'}")
            print(f"  Образование:  {(profile.get('education') or '—')[:90]}")
            print(f"  Контакты:     {', '.join(f'{k}: {v}' for k, v in (profile.get('contacts') or {}).items()) or 'нет'}")
            if not (profile.get('contacts') or {}).get('telegram'):
                print("  Telegram на странице резюме нет — укажите его: [N] → [T].")
            if args.dry_run:
                print("  (проверка — ничего не сохранено)")
            else:
                cfg['candidate_profile'] = profile
                print("[OK] Профиль сохранён — письма и ответы работодателям теперь по нему."
                      if save_config(cfg) else "[X] Профиль не сохранился")
            print("="*70 + "\n")
            return

        if args.promote:
            print("\n" + "="*70)
            print(f"{RED}{BOLD}ПРОДВИЖЕНИЕ РЕЗЮМЕ В ПОИСКЕ HH.RU{RESET}")
            print("="*70)
            r = updater.promote_resume()
            print(f"  Видимость:  {VISIBILITY_TEXT[r['visible']]}")
            if r['gaps']:
                print(f"  Пустые блоки: {', '.join(r['gaps'])}")
                print("     Заполненность влияет на позицию в поиске и на ATS работодателя.")
            else:
                print("  Заполненность: ключевые блоки на месте")
            tag = "[OK]" if r['bumped'] else "[i]"
            print(f"  {tag} {r['bump_message']}")
            print("\n  Поднимать можно раз в 4 часа — это главный рычаг свежести.")
            print("="*70 + "\n")
            return

        if args.bump:
            print("\n" + "="*70)
            print(f"{RED}{BOLD}ПОДНЯТИЕ РЕЗЮМЕ В ПОИСКЕ НА HH.RU{RESET}")
            print("="*70)
            ok, msg = updater.bump_resume()
            tag = "[OK]" if ok else "[i]"
            print(f"{tag} {msg}")
            print("="*70 + "\n")
            return

        if args.status or (not args.apply and not args.sync_adaptive and not args.full_update):
            print("\n" + "="*70)
            print(f"{RED}{BOLD}ПРОВЕРКА СОСТОЯНИЯ РЕЗЮМЕ НА HH.RU{RESET}")
            print("="*70)
            info = updater.get_current_resume_status()
            # ID резюме, слово «скиллов», флаги командной строки и команды python
            # убраны: пользователь ходит через меню и ничего из этого не набирает.
            if not info.get('success'):
                print(f"[X] {info.get('error') or 'состояние резюме прочитать не удалось'}")
            else:
                print(f"Должность: {info.get('position')}")
                print(f"Навыков в резюме: {info.get('skills_count')}")
                if info.get('skills'):
                    print("Текущие навыки: " + ", ".join(info.get('skills')[:30]))
                else:
                    print("[X] В резюме нет ни одного навыка — отборочные роботы hh отсеют отклики.")
                    print("    Обновите резюме пунктом меню «Комплексная модернизация резюме».")
            print("="*70 + "\n")

        if args.full_update:
            print("\n" + "="*70)
            print(f"{RED}{BOLD}КОМПЛЕКСНАЯ МОДЕРНИЗАЦИЯ РЕЗЮМЕ НА HH.RU{RESET}")
            print("="*70)
            # Только навыки из профиля, которых ещё нет в резюме (лимит hh — 30).
            # Раньше сюда шли навыки из базы отказов, уровни вслепую и зашитый
            # абзац в «О себе», одинаковый для любого пользователя.
            try:
                from config_manager import load_config
                profile = (load_config() or {}).get('candidate_profile') or {}
            except Exception:
                profile = {}
            mod_res = updater.apply_full_modernization(
                list(profile.get('skills') or []), profile_only=True, profile=profile)
            # Раньше [OK] и «полностью оптимизировано» печатались всегда, при любом
            # исходе. Теперь метка и финальная строка идут от фактических флагов.
            added = int(mod_res.get('skills_added_count') or 0)
            skills_ok = bool(mod_res.get('skills_updated'))
            about_changed = bool(mod_res.get('about_changed'))
            print(f"{'[OK]' if skills_ok else '[X]'} Навыки: {mod_res.get('skills_message')}")
            print(f"{'[OK]' if mod_res.get('about_updated') else '[X]'} "
                  f"Раздел «О себе»: {mod_res.get('about_message')}")
            if added or about_changed:
                print("Резюме обновлено. Изменения уже видны работодателям.")
            elif skills_ok:
                print("Резюме менять не потребовалось: навыки и раздел «О себе» уже в порядке.")
            else:
                print("Резюме обновить не удалось — причина в строках выше.")
            print("="*70 + "\n")

        elif args.apply or args.sync_adaptive:
            print("\n" + "="*70)
            print(f"{RED}{BOLD}АВТОМАТИЧЕСКАЯ СИНХРОНИЗАЦИЯ НАВЫКОВ В РЕЗЮМЕ НА HH.RU{RESET}")
            print("="*70)
            success, msg, count = updater.sync_adaptive_skills()
            # Метка по фактическому результату, без победной фразы поверх провала.
            if success:
                print(f"[OK] {msg}")
            else:
                print(f"[X] Обновить не удалось: {msg}")
            print("="*70 + "\n")

    finally:
        updater.close()


if __name__ == '__main__':
    run_resume_updater_cli()

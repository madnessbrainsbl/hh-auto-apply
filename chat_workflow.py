"""Traversal and durable follow-ups for the HH messenger."""

import hashlib
import json
import logging
import os
import re
import tempfile
import time
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from markdown_it import MarkdownIt


logger = logging.getLogger('rejection_analyzer')
_INLINE_MARKDOWN = MarkdownIt('commonmark', {'html': False})
CARD_SELECTOR = '[data-qa*="chatik-open-chat"], [class*="chat-cell"], a[href*="/chat/"]'
MESSAGE_SELECTOR = '[data-qa*="chatik-chat-message"], [class*="chat-bubble"]'
REJECTION_MARKERS = (
    'вам отказали', 'не готовы пригласить', 'не готов пригласить',
    'не можем пригласить', 'не сможем пригласить', 'не готовы предложить',
    'не можем предложить', 'ваша кандидатура не', 'выбрали другого кандидата',
    'не прошли отбор', 'не подходите', 'ваш профиль не подходит',
    # 07.10 КСБ-СОФТ: ответ на наш вопрос о причине отказа
    'с более релевантным опытом', 'выбор сделан в пользу', 'решение в пользу другого',
)
# Работодатель сам предлагает продолжить общение, но вопроса в сообщении нет:
# «хотим связаться», «давайте созвонимся», «оставьте контакт». Раньше такие
# сообщения бот не читал как требующие ответа (отвечал только на вопросы).
CONTACT_REQUEST_MARKERS = (
    'хотим связаться', 'хотели бы связаться', 'хотел бы связаться', 'связаться с вами',
    'хотим обсудить', 'хотели бы обсудить', 'давайте обсудим', 'давайте созвон',
    'созвониться', 'созвон', 'позвонить', 'пообщаться', 'пригласить вас', 'приглашаем',
    'оставьте контакт', 'ваш телефон', 'ваш контакт', 'telegram', 'телеграм', 'whatsapp',
    'собеседован', 'интервью', 'знакомство', 'познакомиться',
)
# Типовое «мы рассмотрим и, если подойдёт, свяжемся» ответа не требует.
CONTACT_CANNED_MARKERS = ('если навыки', 'если ваш опыт', 'если вы подойд', 'если ваша кандидатура',
                          'рассмотрим ваше резюме', 'рассмотрим ваш отклик')


def wants_contact(text):
    """Работодатель предлагает связаться/созвониться/обсудить детали (не вопрос и не отказ)."""
    low = normalized(text)
    if not low or any(m in low for m in REJECTION_MARKERS):
        return False
    if any(m in low for m in CONTACT_CANNED_MARKERS):
        return False
    return any(m in low for m in CONTACT_REQUEST_MARKERS)


def contact_reply(profile):
    """Короткий ответ на предложение связаться: согласие и контакт из профиля."""
    contacts = (profile or {}).get('contacts') or {}
    telegram = str(contacts.get('telegram') or '').strip()
    if telegram and not telegram.startswith(('@', 'http')):
        telegram = '@' + telegram
    if telegram:
        return ('Здравствуйте! Благодарю за обратную связь. Да, готов обсудить детали. '
                f'Удобнее всего в Telegram: {telegram}. Если удобен другой формат, '
                'напишите здесь, подстроюсь.')
    return ('Здравствуйте! Благодарю за обратную связь. Да, готов обсудить детали: '
            'пишите здесь или предложите удобное время для разговора.')


EXPERIENCE_YES_REPLY = 'Да, есть опыт. Готов рассказать подробнее.'
_ASKING_WORDS = ('подскажите', 'укажите', 'напишите', 'назовите', 'озвучьте', 'сообщите', 'какие', 'какой',
                 'каков', 'сколько', 'интересует', 'уточните')
_EXPERIENCE_YES_NO = re.compile(
    r'есть ли у вас (?:опыт|практик)|работали ли|имеете ли (?:вы )?опыт|владеете ли|знакомы ли|'
    r'использовали ли|приходилось ли|имеется ли опыт|опыт работы с .{0,80}\?', re.I)
_DETAIL_WORDS = ('расскажите', 'опишите', 'подробн', 'как именно', 'какие именно', 'приведите пример',
                 'поделитесь', 'в чём заключал', 'в чем заключал')


TENURE_QUESTION = re.compile(
    r'сколько\s+(?:лет|времени)|как(?:ой|ов)\s+(?:у\s+вас\s+)?(?:общий\s+)?(?:срок|стаж)|стаж\w*\s+(?:работы|в\s)|'
    r'срок\w*\s+(?:практическ|работы)|как\s+долго|сколько\s+опыта|years\s+of\s+experience|how\s+(?:many|long)', re.I)


def tenure_line(cfg=None):
    """Конкретный стаж, если пользователь задал его (chat_autoreply.tenure_years), иначе None."""
    years = (cfg or {}).get('tenure_years')
    try:
        n = int(years)
    except (TypeError, ValueError):
        return None
    word = 'лет' if 11 <= n % 100 <= 14 else {1: 'год', 2: 'года', 3: 'года', 4: 'года'}.get(n % 10, 'лет')
    return f'Общий опыт работы — {n} {word}.'


def tenure_asked_again(chat, incoming):
    """Про стаж спрашивают повторно: в текущей реплике и в реплике до нашего последнего ответа.

    Первый раз отвечаем обобщённо («Да, такой опыт есть»), на повторный — цифрой (07.10, решение пользователя).
    """
    if not TENURE_QUESTION.search(incoming or ''):
        return False
    messages = chat.get('messages') or []
    last_out = max((i for i, m in enumerate(messages) if m.get('isOut')), default=-1)
    return any(not m.get('isOut') and TENURE_QUESTION.search(m.get('text') or '')
               for m in messages[:last_out])


def allowed_tail_lines(cfg=None):
    """Строки, которые пользователь сам разрешил отправлять с цифрами: зарплата и стаж."""
    return [line for line in (salary_line(cfg), tenure_line(cfg)) if line]


def salary_line(cfg=None):
    """Ответ на прямой вопрос о деньгах в чате.

    Сумма задаётся пользователем в настройках (chat_autoreply.salary_answer). Не задана —
    уклончивый ответ без цифры: в бота никакие личные цифры не вшиты.
    """
    from ai_assistant import SALARY_ANSWER
    return str(((cfg or {}).get('salary_answer') or SALARY_ANSWER)).strip()


def split_salary(text):
    """(остаток без вопросов о зарплате, был ли прямой вопрос о зарплате).

    Прямой — вопрос о деньгах в форме вопроса или просьбы («какие ожидания?», «укажите доход»).
    Простое упоминание зарплаты в тексте вакансии вопросом не считается.
    """
    from ai_assistant import is_salary_question
    sentences = re.split(r'(?<=[.!?])\s+|\n+', str(text or ''))
    rest, asked = [], False
    for sentence in sentences:
        low = normalized(sentence)
        if low and is_salary_question(low) and ('?' in low or any(w in low for w in _ASKING_WORDS)):
            asked = True
        elif sentence.strip():
            rest.append(sentence.strip())
    return ' '.join(rest), asked


def experience_yes_no(text):
    """Один вопрос «есть ли опыт с X?» — без просьбы рассказать подробнее."""
    low = normalized(text)
    if not low or low.count('?') > 1 or any(w in low for w in _DETAIL_WORDS):
        return False
    return bool(_EXPERIENCE_YES_NO.search(low))


# Открытый вопрос: ответ пишется текстом, вариантов-кнопок у него нет.
OPEN_QUESTION = re.compile(r'почему|расскажите|опишите|какие|каким|с какими|каков|как вы|что вы|поделитесь|приведите', re.I)


REASON_REQUEST = (
    'Здравствуйте! Спасибо за обратную связь. Подскажите, пожалуйста, '
    'основную причину отказа: каких навыков или опыта не хватило для этой позиции? '
    'Это поможет мне учесть ваши требования и улучшить кандидатуру. Спасибо!'
)


def normalized(text):
    return ' '.join(str(text or '').split()).casefold()


def _rendered_reply(reply):
    # Parse inline formatting only: list numbers and all factual text must survive.
    pieces = []
    for block in _INLINE_MARKDOWN.parseInline(str(reply or '')):
        for token in block.children or []:
            if token.type in ('text', 'code_inline'):
                pieces.append(token.content)
            elif token.type in ('softbreak', 'hardbreak'):
                pieces.append('\n')
            elif token.type not in ('strong_open', 'strong_close', 'em_open', 'em_close'):
                # Links and images carry information beyond visible text.
                return ''
    return normalized(''.join(pieces))


def confirms_reply(message, reply):
    expected = normalized(reply)
    if not expected or not message.get('isOut') or message.get('pending') or message.get('failed'):
        return False
    actual = normalized(message.get('raw_text', message.get('text')))
    if expected in actual:
        return True
    rendered = _rendered_reply(reply)
    return bool(rendered and rendered in actual)


_CARD_METADATA = re.compile(
    r'\d+|\d{1,2}:\d{2}|сегодня|вчера|позавчера|today|yesterday|'
    r'\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?|'
    r'\d{1,2}\s+(?:янв\w*|фев\w*|мар\w*|апр\w*|ма[йя]|ию[нл]\w*|авг\w*|сен\w*|окт\w*|ноя\w*|дек\w*)'
    r'(?:\s+\d{4})?|'
    r'понедельник|вторник|среда|четверг|пятница|суббота|воскресенье|'
    r'(?:пн|вт|ср|чт|пт|сб|вс|mon|tue|wed|thu|fri|sat|sun)\.?(?:,?\s+\d{1,2}:\d{2})?|'
    r'отказ|собеседование|приглашение|отклик на вакансию|вакансия в архиве|в архиве', re.IGNORECASE)


def chat_card_names(card):
    lines = [line.strip() for line in str(card.get('text') or '').split('\n')
             if line.strip() and not _CARD_METADATA.fullmatch(normalized(line))]
    return {
        'vacancy_title': card.get('vacancy_title') or (lines[0] if lines else ''),
        'company_name': card.get('company_name') or (lines[1] if len(lines) > 1 else ''),
    }


def chat_name_matches(expected, actual):
    expected, actual = normalized(expected), normalized(actual)
    if not expected or not actual:
        return False
    if expected == actual:
        return True
    for shortened, full in ((expected, actual), (actual, expected)):
        if shortened.endswith(('...', '…')):
            prefix = shortened.rstrip('.…')
            if len(prefix) >= 6 and full.startswith(prefix):
                return True
    return False


def fingerprint(text):
    return hashlib.sha256(normalized(text).encode('utf-8')).hexdigest()


def employer_turn(chat):
    """Only the unanswered incoming turn, not an older question above our reply."""
    turn = []
    for message in chat.get('messages') or []:
        if message.get('isOut'):
            turn = []
        elif message.get('text'):
            turn.append(message['text'])
    return '\n\n'.join(turn)


def external_interview_links(text):
    if not re.search(r'интервью|гига.?рекрутер|первичн\w*\s+отбор', text, re.I):
        return []
    links = []
    for raw in re.findall(r'https?://[^\s<>"\]]+', text):
        url = raw.rstrip('.,;!?)')
        try:
            parts = urlsplit(url)
            if parts.scheme == 'https' and parts.hostname and not parts.username and not parts.password:
                # A non-default port must never inherit the host allowlist.
                if parts.port in (None, 443) and url not in links:
                    links.append(url)
        except ValueError:
            continue
    return links


def supported_interview_link(url):
    try:
        parts = urlsplit(url)
        return (parts.scheme == 'https' and parts.port in (None, 443)
                and not parts.username and not parts.password
                and parts.hostname in ('t.me', 'telegram.me', 'max.ru')
                and parts.path.rstrip('/').casefold() == '/giga_recruiter_bot')
    except ValueError:
        return False


class ChatWorkflowMixin:
    def _chat_state_path(self):
        # Resolve at call time: profiles and tests override the data directory.
        from rejection_analyzer import SCRIPT_DIR
        return os.path.join(SCRIPT_DIR, 'chat_actions.json')

    def _load_chat_actions(self):
        if hasattr(self, '_chat_actions'):
            return self._chat_actions
        path = self._chat_state_path()
        self._chat_actions = {}
        if not os.path.exists(path):
            return self._chat_actions
        try:
            with open(path, encoding='utf-8') as stream:
                state = json.load(stream)
            if (not isinstance(state, dict) or state.get('version') != 1
                    or not isinstance(state.get('actions'), dict)
                    or any(not isinstance(v, dict) for v in state['actions'].values())):
                raise ValueError('Invalid chat action history')
            self._chat_actions = state['actions']
        except (OSError, ValueError) as exc:
            self._chat_actions_blocked = True
            logger.error('История ответов чата не читается; отправка остановлена, чтобы не создавать дубли: %s', exc)
        return self._chat_actions

    def _save_chat_action(self, key, action):
        actions = self._load_chat_actions()
        if getattr(self, '_chat_actions_blocked', False):
            return False
        updated = dict(actions, **{key: action})
        path = self._chat_state_path()
        directory = os.path.dirname(os.path.abspath(path))
        tmp = None
        try:
            os.makedirs(directory, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=directory,
                                             prefix='.chat-actions-', suffix='.tmp', delete=False) as stream:
                tmp = stream.name
                json.dump({'version': 1, 'actions': updated}, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            for attempt in range(6):
                try:
                    os.replace(tmp, path)
                    break
                except PermissionError:
                    if attempt == 5:
                        raise
                    time.sleep(.05 * (2 ** attempt))
            self._chat_actions = updated
            return True
        except OSError as exc:
            self._chat_actions_blocked = True
            logger.error('Не удалось сохранить историю чата; новые сообщения не отправляю: %s', exc)
            return False
        finally:
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)

    def _chat_action_key(self, kind, chat, incoming=''):
        identity = (chat.get('vacancy_url') if kind == 'reason' else None) or chat.get('identity') or (
            chat.get('vacancy_title', '') + '|' + chat.get('company_name', ''))
        if kind == 'reason':
            vacancy = re.search(r'/vacancy/(\d+)', identity)
            if vacancy:
                identity = 'vacancy:' + vacancy.group(1)
        # Ask the reason once per conversation, not once per repeated rejection.
        return fingerprint(kind + '|' + identity + '|' + (incoming if kind != 'reason' else ''))

    def _action_record(self, kind, chat, status, **extra):
        return dict(kind=kind, status=status, chat_url=chat.get('chat_url', ''),
                    identity=chat.get('identity', ''), title=chat.get('vacancy_title', ''),
                    company=chat.get('company_name', ''), updated_at=datetime.now().isoformat(), **extra)

    def _current_chat_matches(self, chat, incoming=''):
        current = self._read_open_chat()
        if not current.get('vacancy_title') or not current.get('company_name'):
            return False
        if (normalized(current['vacancy_title']) != normalized(chat.get('vacancy_title'))
                or normalized(current['company_name']) != normalized(chat.get('company_name'))):
            return False
        expected_id = re.search(r'/vacancy/(\d+)', chat.get('vacancy_url', ''))
        current_id = re.search(r'/vacancy/(\d+)', current.get('vacancy_url', ''))
        if expected_id and (not current_id or current_id.group(1) != expected_id.group(1)):
            return False
        return not incoming or normalized(employer_turn(current)) == normalized(incoming)

    @staticmethod
    def _choice_advanced(chat, question):
        last = next((m for m in reversed(chat.get('messages') or []) if not m.get('isOut')), {})
        text = last.get('text', '')
        return bool(last.get('isBot') and normalized(text) != normalized(question)
                    and (chat.get('choice_options') or '?' in text
                         or re.search(r'ваши ответы (?:отправлены|переданы) работодателю', text, re.I)))

    def _click_chat_choice(self, chat, choice, incoming):
        if not self._current_chat_matches(chat, incoming):
            self._chat_send_failure = 'choice_not_clicked'
            return False
        # One native option click; never type its label into the composer.
        clicked = self.driver.execute_script(r"""
            const button = document.querySelector('[data-bot-chat-choice="' + arguments[0] + '"]');
            const question = document.querySelector('[data-bot-recruiter-question="current"]');
            const visible = el => el && el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
            const content = question && (question.querySelector('[data-qa="chat-bubble-text"], [data-qa^="chatik-chat-message"][data-qa$="-text"]') || question);
            if (!visible(button) || !visible(question) || !content
                    || content.innerText.trim() !== arguments[2]
                    || button.disabled || button.getAttribute('aria-disabled') === 'true'
                    || button.innerText.trim() !== arguments[1]) return 'changed';
            const later = [...document.querySelectorAll(arguments[3])].some(el =>
                visible(el) && !el.contains(question) && !question.contains(el)
                && !el.matches('input, textarea, button, [contenteditable="true"]')
                && !/input|button|send|list|container|scroll|messages$/.test(el.getAttribute('data-qa') || '')
                && (question.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING));
            if (later) return 'changed';
            const region = document.querySelector('[data-bot-chat-options-root="current"]');
            if (!region || !region.contains(button)) return 'changed';
            if ([...region.querySelectorAll('textarea, [contenteditable="true"], input[data-qa*="chat"]')].some(el =>
                visible(el) && !el.closest(arguments[4]) && (el.value || el.innerText || '').trim())) return 'draft';
            return 'ok';
        """, choice['index'], choice['label'], chat['messages'][-1].get('raw_text', chat['messages'][-1]['text']),
            MESSAGE_SELECTOR, CARD_SELECTOR)
        if clicked == 'ok':
            # Клик мышью (ActionChains): JS-клик вёрстка hh на React может проигнорировать.
            from selenium.webdriver.common.action_chains import ActionChains
            from selenium.webdriver.common.by import By
            try:
                button = self.driver.find_element(By.CSS_SELECTOR, f'[data-bot-chat-choice="{choice["index"]}"]')
                try:
                    ActionChains(self.driver).move_to_element(button).click().perform()
                except Exception:
                    button.click()
                clicked = 'clicked'
            except Exception as e:
                logger.debug('Кнопка варианта не нажалась: %s', str(e)[:120])
                clicked = 'changed'
        if clicked != 'clicked':
            logger.debug('Вариант робота-рекрутера не нажат: %s', clicked)
            self._chat_send_failure = 'draft' if clicked == 'draft' else 'choice_not_clicked'
            return False
        question = chat['messages'][-1]['text']
        for _ in range(40):
            time.sleep(.25)
            current = self._read_open_chat()
            if not self._current_chat_matches(chat):
                return False
            if self._choice_advanced(current, question):
                return True
        return False

    def _answer_recruiter_choices(self, chat):
        while chat.get('choice_options') and not getattr(self, '_user_closed', False):
            cfg = getattr(self, 'config', {}).get('chat_autoreply') or {}
            if not cfg.get('enabled', True):
                return None
            incoming = employer_turn(chat)
            choices = [c for c in chat['choice_options'] if not c.get('disabled')]
            question = chat['messages'][-1]['text']
            key = self._chat_action_key('choice', chat, incoming)
            previous = self._load_chat_actions().get(key, {})
            if previous.get('status') in ('sent', 'pending_confirmation'):
                return None
            ai = getattr(self, 'ai_assistant', None)
            try:
                index = ai.answer_question(question, question_type='radio',
                                           options=[c['label'] for c in choices],
                                           vacancy_context=chat.get('vacancy_title', '')) if ai and choices else None
            except Exception:
                logger.warning('Не удалось выбрать вариант анкеты; вопрос сохранён для проверки', exc_info=True)
                index = None
            if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(choices):
                self._save_chat_action(key, self._action_record('choice', chat, 'deferred', incoming=incoming,
                                       note='Не выбран доступный вариант анкеты; свободный текст не отправлен'))
                logger.warning('Не выбран вариант робота-рекрутера в чате %s; текст вместо кнопки не отправляю', chat.get('company_name'))
                return None
            choice = choices[index]
            if not self._send_chat_action('choice', chat, choice['label'], incoming, choice=choice):
                return None
            chat = dict(chat, **self._read_open_chat())
        return chat

    def _send_chat_action(self, kind, chat, text, incoming='', choice=None):
        cfg = getattr(self, 'config', {}).get('chat_autoreply') or {}
        if not cfg.get('enabled', True):
            return False
        key = self._chat_action_key(kind, chat, incoming)
        previous = self._load_chat_actions().get(key, {})
        if getattr(self, '_chat_actions_blocked', False):
            return False
        if previous.get('status') == 'pending_confirmation' and previous.get('reply'):
            confirmed = (self._choice_advanced(chat, previous.get('question', '')) if kind == 'choice'
                         else any(confirms_reply(m, previous['reply']) for m in chat.get('messages') or []))
            if confirmed:
                self._save_chat_action(key, dict(previous, status='sent'))
        if previous.get('status') in ('sent', 'pending_confirmation'):
            # An uncertain submission is never automatically repeated.
            return False
        if kind != 'choice' and any(confirms_reply(m, text)
               for m in chat.get('messages') or []):
            self._save_chat_action(key, self._action_record(kind, chat, 'sent'))
            return False
        limit = int(cfg.get('max_per_run') or 0)
        if limit > 0 and getattr(self, '_chat_sent_this_run', 0) >= limit:
            self._save_chat_action(key, self._action_record(kind, chat, 'deferred', note='Локальный лимит ответов'))
            return False
        if kind != 'choice' and not self.find_chat_message_input():
            self._save_chat_action(key, self._action_record(kind, chat, 'composer_disabled'))
            logger.info('  В чате %s нет доступного поля для ответа; отправка пропущена, обход продолжается',
                        chat.get('company_name'))
            return False
        if not self._current_chat_matches(chat, incoming):
            self._save_chat_action(key, self._action_record(kind, chat, 'deferred',
                                   incoming=incoming, note='Чат или сообщение изменились до отправки'))
            logger.warning('Чат %s изменился до отправки; ответ отложен для повторной проверки',
                           chat.get('company_name'))
            return False
        action = self._action_record(kind, chat, 'pending_confirmation', reply=text,
                                     incoming=incoming)
        if kind == 'choice':
            action['question'] = chat['messages'][-1]['text']
        if not self._save_chat_action(key, action):
            return False
        self._chat_send_failure = None
        try:
            sent = self._click_chat_choice(chat, choice, incoming) if kind == 'choice' else self.send_chat_reply(text)
        except Exception:
            logger.exception('Отправку в чате не удалось подтвердить')
            sent = False
        if sent:
            action['status'] = 'sent'
            self._chat_sent_this_run = getattr(self, '_chat_sent_this_run', 0) + 1
            self._save_chat_action(key, action)
            logger.info('  [%s] %s: %s', 'ПРИЧИНА ОТКАЗА' if kind == 'reason' else 'ОТВЕТ',
                        chat.get('company_name'), text)
        else:
            failure = getattr(self, '_chat_send_failure', None)
            if failure in ('draft', 'choice_not_clicked'):
                action.update(status='deferred', note='Сохранён существующий черновик' if failure == 'draft'
                              else 'Вопрос или кнопки изменились; клик не выполнен')
                self._save_chat_action(key, action)
            reason = {'draft': 'в поле уже есть черновик', 'choice_not_clicked': 'кнопка варианта не нажалась или вопрос сменился',
                      'restricted_content': 'в тексте запрещённые личные сведения'}.get(
                          failure, 'поле ответа не найдено или сообщение не появилось в чате')
            logger.warning('  Отправка в чат не подтверждена (%s, %s). Повторять автоматически не буду: %s',
                           'вариант анкеты' if kind == 'choice' else 'текст', reason, chat.get('company_name'))
        return bool(sent)

    def _ask_rejection_reason(self, chat):
        cfg = getattr(self, 'config', {}).get('chat_autoreply') or {}
        if not cfg.get('ask_rejection_reason', True):
            return False
        for message in chat.get('messages') or []:
            if message.get('isOut') and re.search(
                    r'причин.{0,35}отказ|почему.{0,35}отказ|каких.{0,30}(навык|опыт).{0,25}не хват',
                    message.get('text', ''), re.I):
                return False
        # Compare the same snapshot: another cleanup can remove repeated lines across messages.
        return self._send_chat_action('reason', chat, REASON_REQUEST, employer_turn(chat))

    def _open_external_interview(self, url):
        if not supported_interview_link(url):
            return False
        main = self.driver.current_window_handle
        try:
            self.driver.switch_to.new_window('tab')
            handle = self.driver.current_window_handle
            self._external_chat_tabs = getattr(self, '_external_chat_tabs', set()) | {handle}
            self.driver.get(url)
            return True
        except Exception as exc:
            logger.warning('Не удалось открыть внешнее интервью: %s', exc)
            return False
        finally:
            if main in self.driver.window_handles:
                self.driver.switch_to.window(main)

    def _handle_external_invitation(self, chat, incoming):
        turn_links = []
        for message in chat.get('messages') or []:
            if message.get('isOut'):
                turn_links = []
            else:
                turn_links.extend(message.get('links') or [])
        links = external_interview_links(incoming + '\n' + '\n'.join(turn_links))
        if not links:
            return False
        # Prefer a browser-accessible Telegram link over launching the MAX app.
        supported = sorted((url for url in links if supported_interview_link(url)),
                           key=lambda url: urlsplit(url).hostname == 'max.ru')
        url = supported[0] if supported else links[0]
        key = self._chat_action_key('external', chat, url)
        if key not in self._load_chat_actions():
            action = self._action_record('external', chat, 'external_interview_required',
                                         url=url, urls=links, opened=False)
            if self._save_chat_action(key, action):
                cfg = getattr(self, 'config', {}).get('chat_autoreply') or {}
                if supported and cfg.get('open_external_interviews', True):
                    action['opened'] = self._open_external_interview(url)
                    self._save_chat_action(key, action)
                logger.warning('  ВНЕШНЕЕ ИНТЕРВЬЮ: %s — %s. Требуется прохождение в другом сервисе: %s',
                               chat.get('company_name'), chat.get('vacancy_title'), url)
        return True

    def _handle_chat(self, chat, preview='', depth=0):
        from rejection_analyzer import clean_employer_messages
        if chat.get('unavailable_reason'):
            logger.info('Чат %s недоступен: %s; обход продолжается',
                        chat.get('company_name'), chat['unavailable_reason'])
            return None
        for key, action in list(self._load_chat_actions().items()):
            if action.get('status') != 'pending_confirmation' or not action.get('reply'):
                continue
            if action.get('identity') != chat.get('identity'):
                continue
            confirmed = (self._choice_advanced(chat, action.get('question', '')) if action.get('kind') == 'choice'
                         else any(confirms_reply(m, action['reply']) for m in chat.get('messages') or []))
            if confirmed:
                self._save_chat_action(key, dict(action, status='sent'))
        if (chat.get('choice_options') and (chat.get('messages') or [{}])[-1].get('isBot')
                and not OPEN_QUESTION.search((chat.get('messages') or [{}])[-1].get('text', ''))):
            chat = self._answer_recruiter_choices(chat)
            if chat is None:
                return None
        incoming = employer_turn(chat)
        last_incoming_message = next((m for m in reversed(chat.get('messages') or [])
                                      if not m.get('isOut')), {})
        last_incoming = last_incoming_message.get('text', '')
        explicit_refusal = any(p in normalized(last_incoming) for p in REJECTION_MARKERS)
        refusal = explicit_refusal or (self.looks_like_rejection_card(preview)
                                       and not self.looks_like_question_card(incoming))
        # Only a visible HH outcome updates an existing application; never infer an invitation from a question.
        invited = re.search(r'(?im)^\s*(?:Собеседование|Приглашение)\s*$', preview)
        if (refusal or invited) and getattr(self, 'db', None):
            url = urlsplit(chat.get('vacancy_url', ''))
            vacancy = re.fullmatch(r'/vacancy/(\d+)/?', url.path)
            if vacancy and (url.hostname == 'hh.ru' or (url.hostname or '').endswith('.hh.ru')):
                self.db.update_application_status(vacancy[1], 'discarded' if refusal else 'invited')
        if refusal:
            # Preserve employer evidence before sending our follow-up.
            history = [{'sender': 'Соискатель' if m.get('isOut') else 'Работодатель', 'text': m['text']}
                       for m in chat.get('messages') or []]
            employer_messages = clean_employer_messages([m['text'] for m in history if m['sender'] == 'Работодатель'])
            rejection = dict(chat_url=chat.get('chat_url', ''), vacancy_title=chat.get('vacancy_title', ''),
                             company_name=chat.get('company_name', ''), vacancy_url=chat.get('vacancy_url', ''),
                             chat_history=history, employer_messages=employer_messages,
                             cover_letter=next((m['text'] for m in history if m['sender'] == 'Соискатель'), ''),
                             rejection_message='\n'.join(employer_messages) or 'Отказ в чате',
                             date=datetime.now().strftime('%Y-%m-%d'))
            self._ask_rejection_reason(chat)
            return rejection
        if not incoming:
            return None
        if (last_incoming_message.get('isBot') and '?' not in last_incoming
                and re.search(r'ваши ответы (?:отправлены|переданы) работодателю', last_incoming, re.I)):
            logger.debug('Анкета робота-рекрутера завершена; повторный ответ не требуется')
            return None
        if self._handle_external_invitation(chat, incoming):
            return None
        rest, has_salary = split_salary(incoming)
        is_question = bool(rest) and self.looks_like_question_card(rest)
        is_contact = wants_contact(incoming)
        if is_question or is_contact or has_salary:
            cfg = getattr(self, 'config', {}).get('chat_autoreply') or {}
            if not cfg.get('enabled', True):
                return None
            key = self._chat_action_key('answer', chat, incoming)
            if self._load_chat_actions().get(key, {}).get('status') in ('sent', 'pending_confirmation'):
                previous = self._load_chat_actions()[key]
                if previous.get('status') == 'pending_confirmation' and previous.get('reply'):
                    self._send_chat_action('answer', chat, previous['reply'], incoming)
                return None
            profile = getattr(getattr(self, 'ai_assistant', None), 'candidate_profile', {}) or {}
            money = salary_line(cfg)
            tenure = tenure_line(cfg) if tenure_asked_again(chat, incoming) else None
            if tenure:
                # Повторный вопрос о стаже: сам вопрос отвечаем цифрой, остальное — как обычно.
                rest = ' '.join(s for s in re.split(r'(?<=[.!?])\s+|\n+', rest or '')
                                if s.strip() and not TENURE_QUESTION.search(s))
                is_question = bool(rest) and self.looks_like_question_card(rest)
            # Шаблоны без ИИ: прямой вопрос о деньгах, «есть ли опыт с X?», «давайте связаться».
            if tenure and not is_question and not is_contact:
                reply = tenure + ('\n\n' + money if has_salary else '')
            elif has_salary and not is_question and not is_contact:
                reply = money
            elif (cfg.get('experience_yes_template', False)
                  and not has_salary and not is_contact and experience_yes_no(incoming)):
                reply = EXPERIENCE_YES_REPLY
            elif is_contact and not is_question:
                reply = contact_reply(profile)
                if has_salary:
                    reply += '\n\n' + money
            else:
                reply = self.compose_chat_reply(rest or incoming, chat.get('vacancy_title', ''), chat.get('company_name', ''))
                telegram = str((profile.get('contacts') or {}).get('telegram') or '').strip()
                if reply and is_contact and telegram and telegram.lstrip('@').lower() not in reply.lower():
                    reply += f'\n\nДетали удобно обсудить в Telegram: {telegram}'
                if reply and tenure:
                    reply += '\n\n' + tenure
                if reply and has_salary:
                    reply += '\n\n' + money
            if reply:
                if self._send_chat_action('answer', chat, reply, incoming) and depth < 3:
                    return self._answer_followup(chat, incoming, depth)
            else:
                self._save_chat_action(key, self._action_record('answer', chat, 'deferred',
                                       incoming=incoming, note='ИИ не дал подходящий ответ'))
                logger.warning('  Вопросы %s сохранены для повторной обработки: ИИ не дал ответ', chat.get('company_name'))
        return None

    # Боты рекрутеров отвечают за 1–3 с; дольше ждать нельзя: ожидание идёт после каждого ответа.
    FOLLOWUP_WAIT_SECONDS = 6

    def _answer_followup(self, chat, answered, depth):
        """Рекрутер или его бот часто пишет следующий вопрос сразу после нашего ответа
        (07.10 EKONIKA: «Расскажите, с каким вендором NGFW…» через секунды). Раньше бот
        уходил к следующему чату и отвечал только в следующий прогон. Ждём немного и
        отвечаем на новую реплику, не больше трёх раз подряд в одном чате."""
        deadline = time.time() + self.FOLLOWUP_WAIT_SECONDS
        while time.time() < deadline:
            time.sleep(2)
            try:
                current = self._read_open_chat()
            except Exception:
                return None
            turn = employer_turn(current)
            if turn and normalized(turn) != normalized(answered):
                updated = dict(chat, messages=current.get('messages') or [],
                               choice_options=current.get('choice_options'))
                logger.info('  Работодатель ответил сразу — отвечаю в том же чате: %s', chat.get('company_name'))
                return self._handle_chat(updated, '', depth + 1)
        return None

    REVISIT_HOURS = 72
    REVISIT_MAX = 15

    def _recently_answered_chats(self, seen=()):
        """(identity, chat_url) чатов, куда бот отвечал за последние REVISIT_HOURS часов."""
        cutoff = datetime.now() - timedelta(hours=self.REVISIT_HOURS)
        result, urls = [], set()
        actions = sorted(self._load_chat_actions().values(), key=lambda a: a.get('updated_at', ''), reverse=True)
        for action in actions:
            if action.get('kind') not in ('answer', 'reason', 'choice') or action.get('status') != 'sent':
                continue
            url, identity = action.get('chat_url') or '', action.get('identity') or ''
            try:
                fresh = datetime.fromisoformat(action.get('updated_at', '')) >= cutoff
            except ValueError:
                fresh = False
            if not fresh or not re.search(r'/chat/\d+', url) or url in urls or identity in seen:
                continue
            urls.add(url)
            result.append((identity or url, url))
            if len(result) >= self.REVISIT_MAX:
                break
        return result

    def _revisit_answered_chats(self, seen, summary):
        """Перепроверяет недавно отвеченные чаты и отвечает, если работодатель написал снова."""
        rejections = []
        targets = self._recently_answered_chats(seen)
        if not targets:
            return rejections
        logger.info('Перепроверяю чаты, где уже отвечал: %s', len(targets))
        for identity, url in targets:
            if getattr(self, '_user_closed', False) or getattr(self, '_chat_actions_blocked', False):
                break
            try:
                self.driver.get(url)
                time.sleep(3)
                chat = self._read_open_chat()
            except Exception as exc:
                logger.debug('Чат %s не открылся при перепроверке: %s', url, str(exc)[:120])
                continue
            if not chat.get('messages_loaded') or not employer_turn(chat):
                continue
            chat['identity'] = identity
            summary['viewed'] += 1
            # Карточки чата здесь нет, а отказ виден по истории: передаём его как подсказку,
            # чтобы ответ работодателя с причиной отказа попал в разбор, а не остался без внимания.
            refused = any(not m.get('isOut') and any(p in normalized(m.get('text', '')) for p in REJECTION_MARKERS)
                          for m in chat.get('messages') or [])
            try:
                rejection = self._handle_chat(chat, 'Отказ' if refused else '')
            except Exception as exc:
                logger.warning('Ошибка перепроверки чата %s: %s', url, str(exc)[:120])
                continue
            if rejection:
                rejections.append(rejection)
        try:
            self.goto('https://hh.ru/chat')
        except Exception:
            pass
        return rejections

    def _set_unread_filter(self, enabled):
        script = r"""
            const desired = arguments[0];
            for (const cb of document.querySelectorAll('input[type="checkbox"]')) {
                const label = cb.closest('label') || (cb.id && document.querySelector('label[for="' + CSS.escape(cb.id) + '"]'));
                const text = label ? label.innerText || label.textContent : cb.getAttribute('aria-label');
                if (/только непрочитанные/i.test(text || '') && (label || cb).getClientRects().length) {
                    if (cb.checked === desired) return 'confirmed';
                    if (arguments[1]) cb.click();
                    return 'waiting';
                }
            }
            return 'missing';
        """
        clicked = False
        for _ in range(20):
            state = self.driver.execute_script(script, bool(enabled), not clicked)
            if state == 'confirmed':
                return True
            if state == 'waiting' and not clicked:
                clicked = True
                # HH updates the filtered list asynchronously after toggling.
                time.sleep(.5)
            time.sleep(.25)
        return False

    def _chat_list_empty(self):
        return bool(self.driver.execute_script("""
            const visible = e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
            if ([...document.querySelectorAll('[role="progressbar"], [data-qa*="loading"], [data-qa*="spinner"]')].some(visible)) return false;
            return [...document.querySelectorAll('[data-qa*="empty"], [class*="empty-state"]')]
                .some(e => visible(e) && /нет|пуст|не найден/i.test(e.innerText))
                || [...document.querySelectorAll('[class*="magritte-placeholder"] [data-qa="title"]')]
                    .some(e => visible(e) && /^Нет непрочитанных чатов[.!]?$/i.test((e.innerText || '').trim()));
        """))

    def _read_chat_cards(self):
        cards = self.driver.execute_script(r"""
            const selector = arguments[0];
            document.querySelectorAll('[data-bot-chat]').forEach(e => e.removeAttribute('data-bot-chat'));
            const all = [...document.querySelectorAll(selector)].filter(e => e.getClientRects().length && (e.innerText || '').trim());
            const cards = all.filter(e => !all.some(other => other !== e && other.contains(e)));
            return cards.map((card, idx) => {
                card.setAttribute('data-bot-chat', String(idx));
                const text = card.innerText.trim();
                const link = card.matches('a[href*="/chat/"]') ? card : card.querySelector('a[href*="/chat/"]');
                const href = link ? link.href : '';
                const id = card.getAttribute('data-chat-id') || card.getAttribute('data-topic-id') || href;
                const qa = card.getAttribute('data-qa') || '';
                const title = card.querySelector('[data-qa="chat-cell-title"], [class*="chat-cell-title"]');
                const company = card.querySelector('[data-qa="chat-cell-subtitle"], [class*="chat-cell-subtitle"]');
                const stable = id || (/chatik-open-chat[-_].+/.test(qa) ? qa : '');
                return {idx, text, href, key: stable, stableIdentity: !!stable,
                        vacancy_title: title ? title.innerText.trim() : '',
                        company_name: company ? company.innerText.trim() : ''};
            });
        """, CARD_SELECTOR) or []
        for card in cards:
            card.update(chat_card_names(card))
            if not card.get('stableIdentity'):
                card['key'] = card['vacancy_title'] + '|' + card['company_name']
        return cards

    def _scroll_chat_list(self, reset=False):
        return self.driver.execute_script(r"""
            const card = document.querySelector('[data-bot-chat], ' + arguments[0]);
            for (let el = card && card.parentElement; el; el = el.parentElement) {
                if (el.clientHeight > 0 && el.scrollHeight > el.clientHeight + 5 && /auto|scroll/.test(getComputedStyle(el).overflowY)) {
                    const before = el.scrollTop;
                    el.scrollTop = arguments[1] ? 0 : Math.min(el.scrollHeight, before + Math.max(100, el.clientHeight * .8));
                    return {moved: Math.abs(el.scrollTop - before) > 1, position: el.scrollTop};
                }
            }
            return {moved: false};
        """, CARD_SELECTOR, bool(reset)) or {'moved': False}

    def _read_open_chat(self):
        from rejection_analyzer import clean_text_blob, is_ui_noise
        data = self.driver.execute_script(r"""
            const candidates = [...document.querySelectorAll(arguments[0])].filter(el => {
                const qa = el.getAttribute('data-qa') || '';
                return el.getClientRects().length && !/input|button|send|list|container|scroll|messages$/.test(qa)
                    && !el.matches('input, textarea, button, [contenteditable="true"]');
            });
            const roots = candidates.filter(el => !candidates.some(other => other !== el && other.contains(el)));
            // HH wraps a bubble with date markers and application actions; they are not message text.
            const isBubble = el => [...el.classList].some(name => /^chat-bubble(?:--|$)/.test(name));
            const bubbles = roots.map(el => isBubble(el) ? el
                : [...el.querySelectorAll('[class*="chat-bubble"]')].find(isBubble) || el);
            const messages = bubbles.map(el => {
                const content = el.querySelector('[data-qa="chat-bubble-text"]')
                    || el.querySelector('[data-qa^="chatik-chat-message"][data-qa$="-text"]') || el;
                let out = false, bot = false;
                for (let parent = el; parent && parent !== document.body; parent = parent.parentElement) {
                    if (/outgoing|outbound|message_my|my-message|author_me/i.test(String(parent.className))) out = true;
                    if (parent.getAttribute('data-is-own') === 'true' || parent.getAttribute('data-sender') === 'applicant') out = true;
                    if (/chat-bubble_bot(?:--|\b)/.test(String(parent.className))) bot = true;
                }
                if (/(?:^|\n)Робот-рекрутер(?:\n|$)/i.test(el.innerText.trim())) bot = true;
                const state = String(el.className) + ' ' + (el.getAttribute('data-status') || '');
                return {text: content.innerText.trim(), isOut: out, isBot: bot, id: el.getAttribute('data-message-id') || '',
                        failed: /failed|error|не отправлено/i.test(state)
                            || !!el.querySelector('[data-qa*="message-error"], [data-qa*="message-retry"]'),
                        pending: /pending|sending/i.test(state)
                            || !!el.querySelector('[data-qa*="message-pending"], [data-qa*="message-sending"]'),
                        links: [...el.querySelectorAll('a[href]')].map(a => a.href)};
            });
            const visible = el => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
            const companies = [...document.querySelectorAll('[data-qa="participant-info-details"]')].filter(visible);
            const companyNames = [...new Set(companies.map(el => el.innerText.trim().split('\n')[0]).filter(Boolean))];
            const comp = companyNames.length === 1 ? companies[0] : null;
            // Only inspect the conversation header, never vacancy links inside messages or the sidebar.
            let root = comp;
            while (root && !bubbles.some(bubble => root.contains(bubble))) root = root.parentElement;
            root = root || document.body;
            const outsideConversation = el => !el.closest(arguments[1])
                && !bubbles.some(bubble => bubble.contains(el));
            const unavailable = [...root.querySelectorAll('[class*="dialog-splash-screen"] [data-qa="title"]')]
                .find(el => visible(el) && outsideConversation(el)
                    && /^Компания заблокирована$/i.test((el.innerText || '').trim()));
            const vacancyUrl = el => {
                const a = el.matches('a[href]') ? el : el.querySelector('a[href*="/vacancy/"]');
                if (!a) return '';
                try {
                    const url = new URL(a.href);
                    return url.protocol === 'https:' && !url.username && !url.password
                        && (!url.port || url.port === '443')
                        && /(^|\.)hh\.ru$/i.test(url.hostname) && /^\/vacancy\/\d+\/?$/.test(url.pathname)
                        ? url.origin + url.pathname.replace(/\/$/, '') : '';
                } catch (_) { return ''; }
            };
            const cleanTitle = text => (text || '').replace(/\s+/g, ' ').trim()
                .replace(/^(?:Вакансия(?:\s+в\s+архиве)?|в\s+архиве)(?:\s+|$)/i, '')
                .replace(/(?:^|\s+)Перейти$/i, '').trim();
            const readTitle = el => {
                let title = cleanTitle(el.innerText);
                if (title) return title;
                // On HH the link can say only "Перейти", with the title in an adjacent element.
                for (let parent = el.parentElement, depth = 0; parent && depth < 4; parent = parent.parentElement, depth++) {
                    if ((comp && parent.contains(comp)) || bubbles.some(bubble => parent.contains(bubble))
                            || parent.querySelector(arguments[1])) break;
                    const text = (parent.innerText || '').trim();
                    if (/^(?:Вакансия|в\s+архиве)(?:\s+|$)/i.test(text)) {
                        title = cleanTitle(text);
                        if (title && title.length <= 500) return title;
                    }
                }
                return '';
            };
            const headers = [...root.querySelectorAll('[data-qa="chatik-header-vacancy-link"], a[href*="/vacancy/"]')]
                .filter(el => visible(el) && outsideConversation(el) && (vacancyUrl(el)
                    || (el.matches('[data-qa="chatik-header-vacancy-link"]')
                        && !el.matches('a[href]') && !el.querySelector('a[href]'))));
            const urls = [...new Set(headers.map(vacancyUrl).filter(Boolean))];
            const titles = [...new Set(headers.map(readTitle).filter(Boolean))];
            const unambiguous = !!comp && urls.length <= 1 && titles.length === 1;
            document.querySelectorAll('[data-bot-chat-choice]').forEach(el => el.removeAttribute('data-bot-chat-choice'));
            document.querySelectorAll('[data-bot-recruiter-question], [data-bot-chat-options-root]').forEach(el => {
                el.removeAttribute('data-bot-recruiter-question'); el.removeAttribute('data-bot-chat-options-root');
            });
            const last = bubbles[bubbles.length - 1], message = messages[messages.length - 1];
            let choices = [];
            if (unambiguous && last && message && message.isBot && !message.isOut) {
                last.setAttribute('data-bot-recruiter-question', 'current');
                root.setAttribute('data-bot-chat-options-root', 'current');
                choices = [...root.querySelectorAll('button, [role="button"]')].filter(el => {
                    const label = (el.innerText || '').trim();
                    return visible(el) && label && label.length <= 200 && !el.closest(arguments[1])
                        && !bubbles.some(b => b !== last && b.contains(el))
                        && (last.contains(el) || (last.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING))
                        && !/^(?:отправить|прикрепить|назад|закрыть|перейти|скачать|добавить файл)$/i.test(label)
                        // Подсказки hh для соискателя под чатом — вопросы работодателю, а не ответы
                        // робота-рекрутера («Где находится место работы?», «…»). Их нажатие отправило бы
                        // работодателю наш вопрос (07.10: «Отправка не подтверждена» в Сбер IT).
                        && !/[?？]$/.test(label) && !/^[.…⋯]+$/.test(label)
                        && !/^(?:у меня есть профильный опыт|можно без опыта|какой график работы|какая схема оплаты|где находится место работы|могу работать в гибком графике|здравствуйте|добрый день|добрый вечер|доброе утро|привет)/i.test(label);
                }).map((el, index) => {
                    el.setAttribute('data-bot-chat-choice', index);
                    return {index, label:el.innerText.trim(), disabled:!!el.disabled || el.getAttribute('aria-disabled') === 'true'};
                });
            }
            return {company_name: comp ? companyNames[0] : '',
                    vacancy_title: unambiguous ? titles[0] : '',
                    vacancy_url: unambiguous ? urls[0] || '' : '', messages, choice_options:choices,
                    unavailable_reason: unavailable ? unavailable.innerText.trim() : '',
                    messages_loaded: bubbles.some(el => (el.innerText || '').trim())};
        """, MESSAGE_SELECTOR, CARD_SELECTOR) or {}
        cleaned = []
        for message in data.get('messages') or []:
            text = clean_text_blob(message.get('text', ''))
            if text and not is_ui_noise(text):
                message['raw_text'] = message.get('text', '')
                message['text'] = text
                if not cleaned or (text, message.get('isOut')) != (cleaned[-1]['text'], cleaned[-1].get('isOut')):
                    cleaned.append(message)
        data['messages'] = cleaned
        data['links'] = [url for message in cleaned if not message.get('isOut')
                         for url in message.get('links') or []]
        data['chat_url'] = self.driver.current_url
        return data

    def _capture_chat_diagnostics(self, stage):
        if getattr(self, '_chat_diagnostic_count', 0) >= 3:
            return
        self._chat_diagnostic_count = getattr(self, '_chat_diagnostic_count', 0) + 1
        try:
            directory = os.path.join(os.path.dirname(self._chat_state_path()), '.chat_diagnostics')
            os.makedirs(directory, exist_ok=True)
            path = os.path.join(directory, datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '-' + stage)
            details = self.driver.execute_script("""
                const visible = el => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
                const nodes = [...document.querySelectorAll('[data-qa*="chat"], [class*="chat-bubble"], [role="textbox"]')].filter(visible);
                return {url: location.origin + location.pathname,
                    nodes: nodes.slice(0, 100).map(el => ({tag:el.tagName, qa:el.getAttribute('data-qa'),
                        class:typeof el.className === 'string' ? el.className : '', role:el.getAttribute('role'),
                        text_length:(el.innerText || '').trim().length})),
                    loading: [...document.querySelectorAll('[role="progressbar"], [data-qa*="loading"], [data-qa*="spinner"]')].some(visible)};
            """)
            with open(path + '.json', 'w', encoding='utf-8') as stream:
                json.dump({'stage': stage, 'problem': getattr(self, '_last_chat_open_problem', {}),
                           'dom': details}, stream, ensure_ascii=False, indent=2)
            self.driver.save_screenshot(path + '.png')
            logger.warning('Состояние браузера сохранено для диагностики: %s', path + '.png')
        except Exception:
            logger.debug('Не удалось сохранить диагностику чата', exc_info=True)

    def _open_chat_card(self, card):
        names = chat_card_names(card)
        self._last_chat_open_problem = dict(names, reason='Карточка чата исчезла до открытия')
        clicked = self.driver.execute_script("""
            const card = document.querySelector('[data-bot-chat="' + arguments[0] + '"]');
            if (!card) return false;
            card.scrollIntoView({block:'nearest'}); card.click(); return true;
        """, card['idx'])
        if not clicked:
            return None
        for _ in range(40):
            time.sleep(.25)
            chat = self._read_open_chat()
            href = card.get('href')
            # Never answer a stale previous conversation while a new one is loading.
            company = normalized(chat.get('company_name'))
            title_match = chat_name_matches(names['vacancy_title'], chat.get('vacancy_title'))
            company_match = not names['company_name'] or chat_name_matches(names['company_name'], company)
            same_url = bool(href and urlsplit(href)._replace(query='', fragment='')
                            == urlsplit(self.driver.current_url)._replace(query='', fragment=''))
            unavailable_chat = bool(chat.get('unavailable_reason') and same_url and company_match and company)
            missing_title = not chat.get('vacancy_title') and bool(company)
            self._last_chat_open_problem = dict(names,
                actual_title=chat.get('vacancy_title', ''), actual_company=chat.get('company_name', ''),
                code='missing_vacancy_title' if missing_title else 'chat_not_confirmed',
                reason=('Не найдено название вакансии в открытом чате' if missing_title
                        else 'Переписка не загрузилась' if title_match and company_match
                        else 'Заголовок открытого чата не соответствует карточке'))
            if unavailable_chat or ((chat.get('messages') or chat.get('messages_loaded'))
                    and title_match and (company_match or (href and self.driver.current_url == href and company))):
                chat['identity'] = (card['key'] if card.get('stableIdentity')
                                    else chat.get('vacancy_url') or card['key'])
                chat['vacancy_title'] = chat.get('vacancy_title') or names['vacancy_title']
                chat['company_name'] = chat.get('company_name') or names['company_name']
                return chat
        logger.debug('Чат не подтверждён: ожидалось %s / %s, открыто %s / %s',
                     names['vacancy_title'], names['company_name'],
                     self._last_chat_open_problem.get('actual_title'), self._last_chat_open_problem.get('actual_company'))
        self._capture_chat_diagnostics('open')
        return None

    def _recover_messenger(self, unread_only=True):
        composer = self.find_chat_message_input()
        if composer is not None:
            draft = composer.get_attribute('value')
            if draft is None:
                draft = composer.text
            if str(draft or '').strip():
                self._messenger_recovery_problem = 'В открытом чате есть черновик; перезагрузка отменена, чтобы сохранить его'
                return False
        if not self.is_driver_alive() or getattr(self, '_user_closed', False):
            return False
        self.driver.refresh()
        for _ in range(20):
            if not self.is_driver_alive() or getattr(self, '_user_closed', False):
                return False
            if 'login' in (self.driver.current_url or '').lower():
                self._messenger_recovery_problem = 'HH требует повторного входа. Запустите бот с окном браузера и войдите'
                return False
            if not self._set_unread_filter(unread_only) and unread_only:
                self._messenger_recovery_problem = 'Не удалось включить фильтр «Только непрочитанные». Проверьте чаты в режиме с окном браузера'
                return False
            self._scroll_chat_list(reset=True)
            if self._read_chat_cards():
                return True
            time.sleep(.25)
        # Narrow HH layouts show only the active conversation, even after refresh.
        if self._back_to_chat_list():
            if not self._set_unread_filter(unread_only) and unread_only:
                self._messenger_recovery_problem = 'Не удалось включить фильтр «Только непрочитанные» после возврата к списку'
                return False
            self._scroll_chat_list(reset=True)
            if self._read_chat_cards():
                return True
        self._messenger_recovery_problem = 'После перезагрузки список чатов не загрузился. Проверьте HH в режиме с окном браузера'
        return False

    def _defer_unopened_chat(self, card):
        names = chat_card_names(card)
        problem = getattr(self, '_last_chat_open_problem', {})
        note = problem.get('reason') or 'Чат не загрузился или его заголовок не подтверждён'
        if problem.get('actual_title') or problem.get('actual_company'):
            note += (f". Ожидалось: {names['vacancy_title']} / {names['company_name']}; "
                     f"открыто: {problem.get('actual_title', '')} / {problem.get('actual_company', '')}")
        chat = dict(names, identity=card['key'], chat_url=card.get('href') or 'https://hh.ru/chat')
        self._save_chat_action(self._chat_action_key('chat_open', chat),
                               self._action_record('chat_open', chat, 'deferred', note=note))
        return f"{names['vacancy_title']} / {names['company_name']}: {note}"

    def _block_messenger(self, reason):
        hint = ('Откройте чаты в видимом браузере, проверьте вход и загрузку переписки. '
                'Затем выберите повторную проверку в терминале.')
        if getattr(self, '_last_chat_open_problem', {}).get('code') == 'missing_vacancy_title':
            hint += (' Компания найдена, но название вакансии не прочитано. Если оно видно в браузере, '
                     'а ошибка повторяется, требуется исправить чтение разметки HH; удалять кеш или профиль не нужно.')
        self.messenger_summary.update(blocked=True, reason=reason, complete=False,
                                      recovery_hint=hint,
                                      followups_path=os.path.join(os.path.dirname(self._chat_state_path()), 'chat_followups.md'))
        logger.error('[СТОП] Обход чатов остановлен: %s. Анализ, правки резюме и отклики не продолжатся до устранения сбоя.', reason)
        logger.warning('Что делать: %s', hint)
        self._capture_chat_diagnostics('blocked')

    def _write_chat_followups(self):
        actions = self._load_chat_actions()
        pending = [a for a in actions.values() if a.get('status') in (
            'deferred', 'pending_confirmation', 'external_interview_required')]
        path = os.path.join(os.path.dirname(self._chat_state_path()), 'chat_followups.md')
        lines = ['# Незавершённые действия в чатах HH', '',
                 f"Отложено: {sum(a.get('status') == 'deferred' for a in pending)}",
                 f"Отправка не подтверждена: {sum(a.get('status') == 'pending_confirmation' for a in pending)}",
                 f"Внешнее интервью: {sum(a.get('status') == 'external_interview_required' for a in pending)}", '',
                 'Неподтверждённые сообщения не отправлять повторно: сначала проверить доставку в чате.',
                 'Отложенные действия: причина указана под каждым пунктом; история не очищается.',
                 'Внешнее интервью требует участия пользователя; открытие ссылки не означает прохождение.', '']
        for action in pending:
            lines.extend([f"## {action.get('company', '')}: {action.get('title', '')}",
                          f"Статус: {action.get('status')}", action.get('url') or action.get('chat_url', ''),
                          action.get('note', ''), action.get('incoming', ''), ''])
        try:
            with open(path, 'w', encoding='utf-8') as stream:
                stream.write('\n'.join(lines))
        except OSError as exc:
            logger.warning('Отчёт о незавершённых действиях не сохранён: %s', exc)
        if pending:
            logger.warning('Незавершённых действий в чатах: %s. Подробности: %s', len(pending), path)
            logger.info('Чаты: %s; %s; %s. Это не счётчик ошибок текущего обхода.',
                        lines[2], lines[3], lines[4])

    def _scan_messenger_chats(self, max_chats=0, unread_only=True):
        from rejection_analyzer import is_dead_session_message, short_error
        summary = self.messenger_summary = {'viewed': 0, 'rejections': 0, 'sent': 0,
                                             'failed': 0, 'complete': False}
        self._chat_sent_this_run = 0
        if getattr(self, '_user_closed', False):
            return []
        if not self.is_driver_alive() and not self._init_driver():
            self._live_fetch_failed = True
            return []
        if '/chat' not in (self.driver.current_url or '').lower() and not self.goto('https://hh.ru/chat'):
            return []
        if 'login' in (self.driver.current_url or '').lower():
            logger.warning('Для обработки чатов требуется вход в HH')
            return []
        chats, seen, failures = [], set(), {}
        stalled = 0
        recovery_used = False
        consecutive_open_failures = 0
        self._last_chat_open_problem = {}
        self._messenger_recovery_problem = ''
        target = None
        logger.info('Проверяю только непрочитанные чаты: отказы, вопросы и приглашения'
                    if unread_only else 'Проверяю весь доступный список чатов: отказы, вопросы и приглашения')
        try:
            while self.is_driver_alive() and not getattr(self, '_user_closed', False):
                if not self._set_unread_filter(unread_only) and unread_only:
                    summary['failed'] += 1
                    self._block_messenger('Не удалось подтвердить галочку «Только непрочитанные». '
                                          'Прочитанные диалоги не открываю; проверьте фильтр в видимом браузере')
                    break
                if not seen:
                    self._scroll_chat_list(reset=True)
                if getattr(self, '_chat_actions_blocked', False):
                    summary['failed'] += 1
                    self._block_messenger('История сообщений недоступна для записи. '
                                          'Закройте программы, блокирующие chat_actions.json, и повторите запуск')
                    break
                if max_chats > 0 and len(seen) >= max_chats:
                    logger.warning('Обход чатов остановлен по явно заданному ограничению %s', max_chats)
                    break
                cards = self._read_chat_cards()
                target = next((c for c in cards if c['key'] not in seen), None)
                if target is None:
                    progress = self._scroll_chat_list()
                    stalled = 0 if progress.get('moved') else stalled + 1
                    if stalled >= (12 if not cards and not seen else 3):
                        summary['complete'] = bool(seen) and not summary['failed']
                        if not cards:
                            # Empty is only confirmed by an explicit messenger empty state.
                            summary['complete'] = self._chat_list_empty() and not summary['failed']
                            if not summary['complete']:
                                self._capture_chat_diagnostics('list-end')
                        break
                    time.sleep(.4)
                    continue
                stalled = 0
                chat = self._open_chat_card(target)
                if chat is None:
                    failures[target['key']] = failures.get(target['key'], 0) + 1
                    if not recovery_used:
                        recovery_used = True
                        logger.info('Чат не подтверждён. Одна попытка восстановления мессенджера перед продолжением.')
                        if self._recover_messenger(unread_only):
                            continue
                        summary['failed'] += 1
                        note = self._defer_unopened_chat(target)
                        self._block_messenger(self._messenger_recovery_problem or note)
                        break
                    if failures[target['key']] >= 2:
                        seen.add(target['key'])
                        summary['failed'] += 1
                        consecutive_open_failures += 1
                        note = self._defer_unopened_chat(target)
                        if consecutive_open_failures >= 2:
                            self._block_messenger('Два чата подряд не подтверждены после попытки восстановления. ' + note)
                            break
                        logger.warning('Не удалось подтвердить чат; сохранён для повторной проверки: %s', note)
                    if not self._back_to_chat_list():
                        self._block_messenger('Не удалось вернуть список чатов после сбоя открытия')
                        break
                    continue
                consecutive_open_failures = 0
                seen.add(target['key'])
                summary['viewed'] += 1
                pending_chat = dict(chat_card_names(target), identity=target['key'])
                open_key = self._chat_action_key('chat_open', pending_chat)
                if open_key in self._load_chat_actions():
                    self._save_chat_action(open_key, self._action_record('chat_open', pending_chat, 'resolved'))
                try:
                    self._mark_chat_read()
                    rejection = self._handle_chat(chat, target.get('text', ''))
                    if rejection:
                        chats.append(rejection)
                        summary['rejections'] += 1
                except Exception as exc:
                    if is_dead_session_message(exc):
                        raise
                    summary['failed'] += 1
                    logger.warning('Ошибка обработки чата %s; продолжаю с остальными: %s', target['key'], exc)
                if summary['viewed'] % 25 == 0:
                    logger.info('Обход продолжается: проверено чатов %s, отправлено сообщений %s, ошибок %s',
                                summary['viewed'], getattr(self, '_chat_sent_this_run', 0), summary['failed'])
                if not self._back_to_chat_list():
                    summary['failed'] += 1
                    self._block_messenger('Список чатов не вернулся после обработки диалога')
                    break
                if unread_only:
                    # Reading removes cards from this queue, changing virtual row offsets.
                    self._scroll_chat_list(reset=True)
            # Чаты, где мы уже ответили, а работодатель написал снова, пока бот был в чате:
            # hh считает их прочитанными, и обход «только непрочитанные» их не видит
            # (07.10 EKONIKA: вопрос про NGFW пришёл в ту же секунду и остался без ответа).
            if (unread_only and summary.get('complete') and not getattr(self, '_user_closed', False)
                    and (getattr(self, 'config', {}).get('chat_autoreply') or {}).get('revisit_answered', True)):
                for rejection in self._revisit_answered_chats(seen, summary):
                    chats.append(rejection)
                    summary['rejections'] += 1
        except Exception as exc:
            if is_dead_session_message(exc):
                self._user_closed = True
            else:
                if target and target['key'] not in seen:
                    self._defer_unopened_chat(target)
                self._block_messenger('Сбой мессенджера: ' + short_error(exc))
            summary['failed'] += 1
            logger.debug('Ошибка обхода чатов', exc_info=True)
        finally:
            summary['sent'] = getattr(self, '_chat_sent_this_run', 0)
            self._write_chat_followups()
        logger.info('Чаты: просмотрено %s, отказов %s, отправлено сообщений %s, ошибок %s. Обход %s.',
                    summary['viewed'], summary['rejections'], summary['sent'], summary['failed'],
                    'завершён' if summary['complete'] else 'не подтверждён до конца')
        return chats

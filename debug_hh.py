import requests
import json
import os
import sys

# Путь к директории скрипта
from app_paths import CODE_DIR, DATA_DIR, PROFILE_ID

SCRIPT_DIR = DATA_DIR
if CODE_DIR not in sys.path:
    sys.path.insert(0, CODE_DIR)

from config_manager import get_active_resume

# Загружаем токен
with open(os.path.join(SCRIPT_DIR, 'hh_token.json'), 'r') as f:
    token_data = json.load(f)

access_token = token_data['access_token']
headers = {
    'Authorization': f'Bearer {access_token}',
    'User-Agent': 'AutoJobApplyBot/1.0'
}

print("=" * 60)
print("ДИАГНОСТИКА HH.RU API")
print("=" * 60)

# 1. Проверяем информацию о пользователе
print("\n1. Информация о пользователе (/me):")
r = requests.get('https://api.hh.ru/me', headers=headers)
print(f"   Статус: {r.status_code}")
if r.status_code == 200:
    me = r.json()
    print(f"   Имя: {me.get('first_name')} {me.get('last_name')}")
    print(f"   Email: {me.get('email')}")
    print(f"   Тип: {me.get('is_applicant')=}, {me.get('is_employer')=}")
else:
    print(f"   Ошибка: {r.text}")

# 2. Проверяем резюме
print("\n2. Список резюме (/resumes/mine):")
r = requests.get('https://api.hh.ru/resumes/mine', headers=headers)
print(f"   Статус: {r.status_code}")
if r.status_code == 200:
    resumes = r.json().get('items', [])
    print(f"   Найдено резюме: {len(resumes)}")
    for res in resumes:
        print(f"   - ID: {res.get('id')}")
        print(f"     Название: {res.get('title')}")
        print(f"     Статус: {res.get('status', {}).get('id')} - {res.get('status', {}).get('name')}")
        print(f"     Можно откликаться: {res.get('can_publish_or_update')}")
        access = res.get('access', {})
        print(f"     Доступ: {access.get('type', {}).get('id')}")
else:
    print(f"   Ошибка: {r.text[:500]}")

# 3. Проверяем существующие отклики
print("\n3. Существующие отклики (/negotiations):")
r = requests.get('https://api.hh.ru/negotiations', headers=headers, params={'per_page': 3})
print(f"   Статус: {r.status_code}")
if r.status_code == 200:
    data = r.json()
    print(f"   Всего откликов: {data.get('found', 0)}")
else:
    print(f"   Ошибка: {r.text[:500]}")

# 4. Пробуем получить информацию о конкретной вакансии
print("\n4. Тест вакансии (первая из кеша):")
cache_file = os.path.join(SCRIPT_DIR, 'vacancies_cache.json')
try:
    with open(cache_file, 'r', encoding='utf-8') as f:
        cache = json.load(f)
    if cache.get('vacancies'):
        vacancy = cache['vacancies'][0]
        vacancy_id = vacancy.get('id')
        print(f"   Вакансия ID: {vacancy_id}")
        print(f"   Название: {vacancy.get('name')}")
        
        # Получаем детали вакансии
        r = requests.get(f'https://api.hh.ru/vacancies/{vacancy_id}', headers=headers)
        if r.status_code == 200:
            v_data = r.json()
            relations = v_data.get('relations', [])
            print(f"   Relations: {relations}")
            
            # Проверяем можно ли откликнуться
            response_info = v_data.get('response_letter_required')
            print(f"   Требуется письмо: {response_info}")
            
            # Проверяем тесты
            has_test = v_data.get('has_test', False)
            print(f"   Есть тест: {has_test}")
except Exception as e:
    print(f"   Ошибка: {e}")

# 5. Тестовый отклик с подробным выводом
print("\n5. Тестовый отклик (с полным выводом):")
# Резюме берём из конфига (пункт меню [R]), а не из зашитого ID:
# чужой ID здесь означал бы тестовый отклик не от того аккаунта.
RESUME_ID, RESUME_TITLE = get_active_resume()
if not RESUME_ID:
    print("   [X] Резюме не настроено: в hh_selenium_config.json пусто поле resume_id.")
    print("   Откройте меню (python test.py) -> пункт [R] и выберите резюме из своего аккаунта hh.ru,")
    print("   либо задайте переменную окружения HH_RESUME_ID. Тестовый отклик пропущен.")
    print("\n" + "=" * 60)
    sys.exit(0)
print(f"   Резюме из конфига: {RESUME_TITLE}")

try:
    with open(cache_file, 'r', encoding='utf-8') as f:
        cache = json.load(f)
    if cache.get('vacancies'):
        vacancy_id = cache['vacancies'][0].get('id')
        
        print(f"   Отправляем отклик на вакансию {vacancy_id}")
        print(f"   Resume ID: {RESUME_ID}")
        
        data = {
            'vacancy_id': str(vacancy_id),
            'resume_id': RESUME_ID,
            'message': 'Добрый день! Заинтересован в данной позиции.'
        }
        
        r = requests.post(
            'https://api.hh.ru/negotiations',
            headers=headers,
            data=data
        )
        
        print(f"   Статус ответа: {r.status_code}")
        print(f"   Заголовки ответа: {dict(r.headers)}")
        print(f"   Тело ответа: {r.text}")
        
except Exception as e:
    print(f"   Ошибка: {e}")

print("\n" + "=" * 60)

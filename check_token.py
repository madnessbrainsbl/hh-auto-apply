import requests
import json
import os

from app_paths import CODE_DIR, DATA_DIR, PROFILE_ID

SCRIPT_DIR = DATA_DIR

with open(os.path.join(SCRIPT_DIR, 'hh_token.json'), 'r') as f:
    token_data = json.load(f)

access_token = token_data['access_token']

# Проверяем токен через специальный endpoint
headers = {
    'Authorization': f'Bearer {access_token}',
    'User-Agent': 'AutoJobApplyBot/1.0'
}

print("Проверка токена...")
print(f"Access Token: {access_token[:20]}...")

# Попробуем получить информацию о токене
r = requests.get('https://api.hh.ru/me', headers=headers)
print(f"\n/me статус: {r.status_code}")
if r.status_code == 200:
    data = r.json()
    print(f"Пользователь: {data.get('first_name')} {data.get('last_name')}")
    
    # Проверяем есть ли информация о правах
    print(f"\nПолный ответ /me:")
    for key, value in data.items():
        print(f"  {key}: {value}")

# Проверяем доступные методы
print("\n\nПроверка доступа к методам:")

endpoints = [
    ('GET', '/resumes/mine', 'Мои резюме'),
    ('GET', '/negotiations', 'Мои отклики'),
    ('GET', '/vacancies', 'Поиск вакансий'),
    ('GET', '/employers', 'Работодатели'),
]

for method, endpoint, desc in endpoints:
    url = f'https://api.hh.ru{endpoint}'
    if method == 'GET':
        r = requests.get(url, headers=headers, params={'per_page': 1})
    print(f"  {desc} ({endpoint}): {r.status_code}")
    if r.status_code == 403:
        print(f"    Ошибка: {r.json()}")

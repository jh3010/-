# tickets_db.py
import json
import os

# 티켓 정보를 저장할 파일 이름
DB_FILE = 'tickets.json'

def _load_data():
    """JSON 파일에서 티켓 데이터를 불러옵니다."""
    if not os.path.exists(DB_FILE):
        return {}
    try:
        with open(DB_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except json.JSONDecodeError:
        return {}

def _save_data(data):
    """JSON 파일에 티켓 데이터를 저장합니다."""
    with open(DB_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4)

def get_open_ticket(user_id):
    """특정 유저가 이미 열어둔 티켓(채널 ID)이 있는지 확인하고 반환합니다."""
    data = _load_data()
    return data.get(str(user_id))

def record_ticket(user_id, channel_id):
    """유저가 생성한 티켓(채널 ID)을 데이터베이스에 기록합니다."""
    data = _load_data()
    data[str(user_id)] = channel_id
    _save_data(data)

def remove_ticket_by_channel(channel_id):
    """채널이 삭제될 때 해당 채널 ID를 기반으로 티켓 기록을 삭제합니다."""
    data = _load_data()
    user_to_delete = None
    
    # 딕셔너리를 돌면서 해당 채널 ID를 가진 유저를 찾음
    for uid, cid in data.items():
        if cid == channel_id:
            user_to_delete = uid
            break
            
    # 찾았다면 해당 유저의 티켓 기록 삭제
    if user_to_delete:
        del data[user_to_delete]
        _save_data(data)
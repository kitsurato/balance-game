import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room, leave_room
from flask_sqlalchemy import SQLAlchemy
import time
import threading
import random
import math
import json
import os
from copy import deepcopy

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///game.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
socketio = SocketIO(app, cors_allowed_origins="*")

ADMIN_PASSWORD = "110110" 

# 配置常量
MAX_HP = 10
MAX_PLAYERS = 8
MAX_ROOMS = 5
TIME_LIMIT_ROUND = 30
TIME_LIMIT_PREGAME = 60
TIME_LIMIT_RULE = 5
TIME_LIMIT_RESULT = 5
TIME_LIMIT_GAMEOVER = 60 

ULTIMATE_PIG_NAMES = [
    "大白猪", "长白猪", "杜洛克猪", "汉普夏猪", "皮特兰猪", "巴克夏猪", "波中猪", "切斯特白猪", "塔姆沃思猪", "赫里福德猪",
    "曼加利察猪", "伊比利亚猪", "越南大肚猪", "哥廷根猪", "英国大黑猪", "英国鞍背猪", "施瓦本厅猪", "红河猪", "梅山猪", "东北民猪",
    "金华猪", "宁乡猪", "荣昌猪", "太湖猪", "内江猪", "成华猪", "藏猪", "巴马香猪", "五指山猪", "互助八眉猪",
    "淮猪", "姜曲海猪", "陆川猪", "蓝塘猪", "广东大花白猪", "马身猪", "雅南猪", "乌金猪", "关岭猪", "柯乐猪",
    "凉山猪", "浦东白猪", "沙子岭猪", "通城猪", "乐平猪", "确山黑猪", "莱芜猪", "深州猪", "汉江黑猪", "滇南小耳猪"
]

class User(db.Model):
    id = db.Column(db.String(50), primary_key=True) 
    password = db.Column(db.String(50), nullable=False)
    nickname = db.Column(db.String(50), nullable=False, default="Player")
    score = db.Column(db.Integer, default=0)
    ultimate_title = db.Column(db.String(50), nullable=True)

    def get_rank_info(self):
        if self.score < 10:
            return {"title": "猪仔", "icon": "🍼", "class": "text-gray-500", "is_max": False}
        elif self.score < 50:
            return {"title": "保育猪", "icon": "🐽", "class": "text-blue-500", "is_max": False}
        elif self.score < 200:
            return {"title": "生长猪", "icon": "🐖", "class": "text-green-500", "is_max": False}
        else:
            if not self.ultimate_title:
                self.ultimate_title = random.choice(ULTIMATE_PIG_NAMES)
                db.session.commit()
            return {"title": self.ultimate_title, "icon": "🐗", "class": "text-yellow-500", "is_max": True}

    def to_dict(self):
        return {
            'uid': self.id,
            'nickname': self.nickname,
            'score': self.score,
            'rank_info': self.get_rank_info()
        }

class GameRecord(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=db.func.now())
    players_json = db.Column(db.Text)
    details_json = db.Column(db.Text)

with app.app_context():
    db.create_all()

# --- 全局状态 ---
rooms = {} 
SID_TO_ROOM = {}
SID_TO_UID = {}

BASIC_RULES = [
    "每轮选取 0 至 100 之间的整数。",
    "目标值为全员平均数的 X 倍 (默认为 0.8)。",
    "最接近目标者获胜，其余玩家扣除 1 点生命。",
    "玩家淘汰时，追加永久规则。",
    "每回合可能触发随机限定规则。"
]

PERMANENT_RULE_POOL = [
    {"id": 1, "desc": "【冲突】若数字与他人重复，则判定为失败并扣除 1 点生命。", "type": "perm"},
    {"id": 2, "desc": "【精准】若赢家误差小于 1，败者将扣除 2 点生命。", "type": "perm"},
    {"id": 3, "desc": "【极值】若 0 与 100 同时出现，选 100 者直接获胜。", "type": "perm"},
    {"id": 4, "desc": "【幽灵】已淘汰玩家的最后数字将永远参与均值计算(权重1)。", "type": "perm"},
    {"id": 5, "desc": "【绝境】HP < 3 的玩家，其数字对均值的权重变为 3 倍。", "type": "perm"},
    {"id": 6, "desc": "【通缉】HP 最高者若未获胜，额外扣 1 血。", "type": "perm"}
]

ROUND_EVENT_POOL = [
    {"id": 101, "desc": "【混乱】你选择的数字将与其他人进行交换！", "type": "temp"},
    {"id": 102, "desc": "【波动】本回合目标倍率发生突变！", "type": "temp"},
    {"id": 103, "desc": "【安全】选择数字在 40-60 时 +1 HP，且本回合胜者 +1 HP！", "type": "temp"},
    {"id": 104, "desc": "【黑暗】隐藏全员 HP，且无法看到自己选择的数字！", "type": "temp"},
    {"id": 105, "desc": "【革命】逻辑反转！目标值变为：100 - (均值 x 倍率)！", "type": "temp"},
    {"id": 106, "desc": "【赌徒】幸运尾数！命中幸运数字的人 +1 HP！", "type": "temp"}
]

timer_thread = None

# --- 辅助函数 ---
def init_room_state(room_id, room_name):
    return {
        "id": room_id,
        "name": room_name,
        "phase": "LOBBY",
        "round": 0,
        "timer": 0,
        "players": {},
        "spectators": [], # Store objects: {uid, name, likes_sent}
        "rules": [],
        "new_rule": None,
        "round_event": None,
        "multiplier": 0.8,
        "dead_guesses": [],
        "blind_mode": False,
        "logs": [],
        "last_result": {},
        "full_history": [],
        "config": {"max_likes": 10},
        "kick_votes": {},
        "pending_events": {"perm": [], "temp": None},
        "available_perm_rules": list(PERMANENT_RULE_POOL),
        "elimination_stack": [],
        "basic_rules": BASIC_RULES,
        "announcement_queue": []
    }

def get_room_by_sid(sid):
    room_id = SID_TO_ROOM.get(sid)
    if room_id and room_id in rooms:
        return rooms[room_id]
    return None

def broadcast_room_state(room_id):
    if room_id in rooms:
        socketio.emit('state_update', rooms[room_id], room=room_id)

def broadcast_room_list():
    room_list = []
    for rid, r in rooms.items():
        room_list.append({
            "id": rid,
            "name": r["name"],
            "count": len(r["players"]),
            "phase": r["phase"]
        })
    socketio.emit('room_list_update', room_list)

# --- 核心逻辑 ---

def apply_round_event(room, event):
    event_copy = deepcopy(event)
    room["round_event"] = event_copy
    if event_copy["id"] == 102:
        new_mult = round(random.randint(1, 20) * 0.1, 1)
        room["multiplier"] = new_mult
        event_copy["desc"] = f"【波动】本回合目标倍率变更为 x{new_mult} !"
    elif event_copy["id"] == 104:
        room["blind_mode"] = True
    elif event_copy["id"] == 106:
        lucky_digit = random.randint(0, 9)
        event_copy["lucky_digit"] = lucky_digit 
        event_copy["desc"] = f"【赌徒】幸运尾数 {lucky_digit}！选择以 {lucky_digit} 结尾数字的人，回合后 +1 HP！"

def trigger_room_rule(room, new_rule, log_append="", author_name=None):
    rule_copy = deepcopy(new_rule)
    if author_name:
        rule_copy["desc"] += f" (💀 {author_name})"
    room["rules"].append(rule_copy)
    room["announcement_queue"].append(rule_copy) 
    room["new_rule"] = rule_copy
    if log_append: log_append += f" | {rule_copy['desc']}"

def process_announcement_queue(room_id):
    room = rooms.get(room_id)
    if not room: return

    if len(room["announcement_queue"]) > 0:
        next_rule = room["announcement_queue"].pop(0)
        room["new_rule"] = next_rule
        room["phase"] = "RULE_ANNOUNCEMENT"
        room["timer"] = TIME_LIMIT_RULE
    else:
        start_new_round_logic(room)
    
    broadcast_room_state(room_id)

def start_new_round(room_id):
    room = rooms.get(room_id)
    if not room: return

    # 1. 检查管理员预设规则
    if room["pending_events"]["perm"]:
        for pid in room["pending_events"]["perm"]:
            rule_obj = next((r for r in PERMANENT_RULE_POOL if r["id"] == pid), None)
            if rule_obj:
                if rule_obj in room["available_perm_rules"]:
                    room["available_perm_rules"].remove(rule_obj)
                trigger_room_rule(room, rule_obj)
        room["pending_events"]["perm"] = []

    # 2. 检查公告队列
    if room["announcement_queue"]:
        process_announcement_queue(room_id)
    else:
        start_new_round_logic(room)
        broadcast_room_state(room_id)

def start_new_round_logic(room):
    room["phase"] = "INPUT"
    room["round"] += 1
    room["timer"] = TIME_LIMIT_ROUND
    room["multiplier"] = 0.8
    room["round_event"] = None
    room["blind_mode"] = False
    
    for p in room["players"].values():
        p["submitted"] = False
        p["guess"] = None

    alive_count = sum(1 for p in room["players"].values() if p["alive"])
    
    pending_temp_id = room["pending_events"]["temp"]
    if pending_temp_id:
        event = next((r for r in ROUND_EVENT_POOL if r["id"] == pending_temp_id), None)
        if event: apply_round_event(room, event)
        room["pending_events"]["temp"] = None
    else:
        if alive_count == 2 and random.random() < 0.7:
            chaos_event = next(e for e in ROUND_EVENT_POOL if e["id"] == 101)
            apply_round_event(room, chaos_event)
        elif random.random() < 0.4:
            other_events = [e for e in ROUND_EVENT_POOL if e["id"] != 101]
            if other_events:
                event = random.choice(other_events)
                apply_round_event(room, event)

def calculate_points_and_save_room(room, winner_uid):
    with app.app_context():
        ranked_uids = [winner_uid] + list(reversed(room["elimination_stack"]))
        ranked_uids = [u for u in ranked_uids if u]
        total_players = len(ranked_uids)
        points_map = {}
        
        if 3 <= total_players <= 4:
            for i, uid in enumerate(ranked_uids): points_map[uid] = 2 if i == 0 else 1
        elif 5 <= total_players <= 6:
            for i, uid in enumerate(ranked_uids): points_map[uid] = 3 if i==0 else (2 if i==1 else 1)
        elif 7 <= total_players <= 8:
            for i, uid in enumerate(ranked_uids): 
                if i==0: points_map[uid]=4
                elif i==1: points_map[uid]=3
                elif i in [2,3]: points_map[uid]=2
                else: points_map[uid]=1
        else:
             for i, uid in enumerate(ranked_uids): points_map[uid] = 1 if i == 0 else 0

        record_data = []
        for i, uid in enumerate(ranked_uids):
            player_data = room["players"].get(uid, {})
            change = points_map.get(uid, 0)
            
            is_suicide = player_data.get("suicided", False)
            if is_suicide:
                hp_at_death = player_data.get("hp_at_death", 0)
                if hp_at_death > 1:
                    change = 0 
            
            user = db.session.get(User, uid)
            if user:
                user.score += change
                if uid in room["players"]:
                    room["players"][uid]["points_change"] = change
                    room["players"][uid]["rank_info"] = user.get_rank_info()
                    # FIX: 必须同步 score 回到内存 room 对象，否则前端进度条不更新
                    room["players"][uid]["score"] = user.score
                
                record_data.append({
                    "uid": uid,
                    "nickname": user.nickname,
                    "score_change": change,
                    "new_score": user.score,
                    "rank": user.get_rank_info(),
                    "game_rank": i + 1, 
                    "total_players": total_players,
                    "is_suicide": is_suicide
                })
        db.session.commit()
        
        new_record = GameRecord(
            players_json=json.dumps(record_data),
            details_json=json.dumps(room["full_history"])
        )
        db.session.add(new_record)
        db.session.commit()

def calculate_round(room_id):
    room = rooms.get(room_id)
    if not room: return
    
    players = room["players"]
    alive = [p for p in players.values() if p["alive"]]
    
    if not alive: 
        room["phase"] = "END"
        broadcast_room_state(room_id)
        return

    guesses = []
    for p in alive:
        val = p["guess"]
        if val is None: val = random.randint(0, 100)
        guesses.append({"player": p, "val": val, "org_val": val, "source": p["name"]}) 
    
    log_msg = f"R{room['round']}"
    
    if room["round_event"] and room["round_event"]["id"] == 101 and len(guesses) > 1:
        indices = list(range(len(guesses)))
        is_fixed = True
        while is_fixed:
            random.shuffle(indices)
            is_fixed = False
            for i, idx in enumerate(indices):
                if i == idx: is_fixed = True
        original_data = [(g["val"], g["player"]["name"]) for g in guesses]
        for i, g in enumerate(guesses):
            g["val"] = original_data[indices[i]][0]
            g["source"] = original_data[indices[i]][1]
        log_msg += " | ⚡交换"

    active_rule_ids = set([r["id"] for r in room["rules"]])
    
    is_final_duel = len(alive) <= 2
    if is_final_duel: active_rule_ids.add(3)

    total_val = 0
    total_w = 0
    values = []
    for g in guesses:
        values.append(g['val'])
        w = 3 if (5 in active_rule_ids and g['player']['hp'] < 3) else 1
        total_val += g['val'] * w
        total_w += w
    if 4 in active_rule_ids:
        for ghost_val in room["dead_guesses"]:
            total_val += ghost_val
            total_w += 1
            
    avg = total_val / total_w if total_w else 0
    target = avg * room["multiplier"]
    if room["round_event"] and room["round_event"]["id"] == 105:
        target = 100 - target
        log_msg += f": 革命! {target:.2f}"
    else:
        log_msg += f": 均值 {avg:.2f} -> 目标 {target:.2f}"

    winners = []
    base_damage = 1
    
    rule3_triggered = False
    if 3 in active_rule_ids and 0 in values and 100 in values:
        winners = [g["player"] for g in guesses if g["val"] == 100]
        rule3_triggered = True
        log_msg += " | 极值(100胜)"

    if not rule3_triggered:
        candidates = guesses[:]
        if 1 in active_rule_ids:
            counts = {x: values.count(x) for x in values}
            if any(c > 1 for c in counts.values()): log_msg += " | 冲突"
            candidates = [g for g in candidates if counts[g['val']] == 1]
        
        if not candidates: winners = []
        else:
            candidates.sort(key=lambda x: abs(x['val'] - target))
            min_diff = abs(candidates[0]['val'] - target)
            winners = [x['player'] for x in candidates if abs(x['val'] - target) == min_diff]
            if 2 in active_rule_ids and min_diff < 1: 
                base_damage = 2
                log_msg += " | 精准"

    max_hp_val = max(p['hp'] for p in alive) if 6 in active_rule_ids and alive else -999

    round_details = []
    for p in alive:
        pg = next(g for g in guesses if g['player'] == p)
        is_winner = p in winners
        actual_dmg = 0
        
        if is_winner:
            if room["round_event"] and room["round_event"]["id"] == 103:
                p["hp"] = min(MAX_HP, p["hp"] + 1)
        else:
            actual_dmg = base_damage
            if room["round_event"] and room["round_event"]["id"] == 103 and 40 <= pg["val"] <= 60:
                actual_dmg = 0
            if 6 in active_rule_ids and p['hp'] == max_hp_val:
                actual_dmg += 1
            p["hp"] -= actual_dmg
        
        if room["round_event"] and room["round_event"]["id"] == 106:
            lucky = room["round_event"].get("lucky_digit")
            if lucky is not None and pg["val"] % 10 == lucky:
                p["hp"] = min(MAX_HP, p["hp"] + 1)

        p["last_dmg"] = actual_dmg
        p["is_winner"] = is_winner
        round_details.append({
            "uid": p["uid"], "name": p["name"], "val": pg["val"],
            "org_val": pg["org_val"], "source": pg["source"], 
            "hp": p["hp"], "dmg": actual_dmg, "win": is_winner
        })

    active_rules_desc = []
    for rid in active_rule_ids:
        rdef = next((r for r in PERMANENT_RULE_POOL if r["id"] == rid), None)
        if rdef:
            desc = rdef["desc"]
            if rid == 3 and is_final_duel: desc = "【极值(决战强制)】0 与 100 同时出现，选 100 者直接获胜。"
            room_rule = next((r for r in room["rules"] if r["id"] == rid), None)
            if room_rule: desc = room_rule["desc"]
            active_rules_desc.append(desc)
    
    room["full_history"].append({
        "round_num": room["round"], "target": round(target, 2), "avg": round(avg, 2),
        "event_desc": room["round_event"]["desc"] if room["round_event"] else None,
        "active_rules": active_rules_desc, "player_data": round_details
    })

    newly_dead = [p for p in players.values() if p["hp"] <= 0 and p["alive"]]
    current_alive_count = sum(1 for p in players.values() if p["hp"] > 0)
    
    for p in newly_dead:
        p["alive"] = False
        dead_val = next((d['val'] for d in round_details if d['name'] == p['name']), 0)
        room["dead_guesses"].append(dead_val)
        if p["uid"] not in room["elimination_stack"]:
            room["elimination_stack"].append(p["uid"])

    # 规则触发
    if newly_dead:
        if current_alive_count == 2:
            rule_3 = next((r for r in room["available_perm_rules"] if r["id"] == 3), None)
            if rule_3:
                room["available_perm_rules"].remove(rule_3)
                trigger_room_rule(room, rule_3, author_name="System")
        
        if room["available_perm_rules"] and not (current_alive_count==2 and rule_3):
             idx = random.randint(0, len(room["available_perm_rules"]) - 1)
             new_rule = room["available_perm_rules"].pop(idx)
             trigger_room_rule(room, new_rule)

    room["last_result"] = {
        "avg": round(avg, 2), "target": round(target, 2), "details": round_details, "log": log_msg
    }
    room["logs"].insert(0, log_msg)
    room["phase"] = "RESULT"
    room["timer"] = TIME_LIMIT_RESULT

    if current_alive_count <= 1:
        winner_uid = None
        if current_alive_count == 1:
            winner = next((p for p in players.values() if p["alive"]), None)
            if winner: winner_uid = winner["uid"]
        calculate_points_and_save_room(room, winner_uid)
        room["phase"] = "END"
        room["timer"] = TIME_LIMIT_GAMEOVER
        broadcast_room_list()
    
    broadcast_room_state(room_id)

def handle_timeout(room_id):
    room = rooms.get(room_id)
    if not room: return
    if room["phase"] == "PRE_GAME": start_new_round(room_id)
    elif room["phase"] == "RULE_ANNOUNCEMENT": process_announcement_queue(room_id)
    elif room["phase"] == "INPUT": calculate_round(room_id)
    elif room["phase"] == "RESULT":
        if len(room["announcement_queue"]) > 0:
             process_announcement_queue(room_id)
        else:
             start_new_round(room_id)
    elif room["phase"] == "END": perform_reset(room_id)

def check_all_ready(room_id):
    broadcast_room_state(room_id)
    broadcast_room_list()

def start_pre_game(room_id):
    room = rooms.get(room_id)
    if not room: return
    
    room["elimination_stack"] = []
    for p in room["players"].values():
        p["confirmed"] = False
        p["points_change"] = 0
    
    room["phase"] = "PRE_GAME"
    room["round"] = 0
    room["timer"] = TIME_LIMIT_PREGAME
    
    global timer_thread
    if not timer_thread:
        timer_thread = threading.Thread(target=background_timer, daemon=True)
        timer_thread.start()
        
    broadcast_room_state(room_id)
    broadcast_room_list() 

def check_all_submitted(room_id):
    room = rooms.get(room_id)
    if not room: return
    alive = [p for p in room["players"].values() if p["alive"]]
    if not alive: return
    if all(p["submitted"] for p in alive):
        calculate_round(room_id)

def check_all_confirmed(room_id):
    room = rooms.get(room_id)
    if not room: return
    alive = [p for p in room["players"].values() if p["alive"]]
    if not alive: return
    if all(p.get("confirmed", False) for p in alive):
        start_new_round(room_id)

def perform_reset(room_id):
    room = rooms.get(room_id)
    if not room: return
    current_config = room["config"]
    current_spectators = room["spectators"]
    for p in room["players"].values():
        p.update({
            "hp": MAX_HP, "alive": True, "guess": None, "submitted": False,
            "confirmed": False, "ready": False, "last_dmg": 0, "is_winner": False,
            "likes": 0, "likes_sent": 0, "points_change": 0,
            "suicided": False, "hp_at_death": 0
        })
    room.update({
        "phase": "LOBBY", "round": 0, "rules": [], "logs": [],
        "new_rule": None, "round_event": None, "multiplier": 0.8,
        "dead_guesses": [], "blind_mode": False, "full_history": [],
        "kick_votes": {}, "pending_events": {"perm": [], "temp": None},
        "available_perm_rules": list(PERMANENT_RULE_POOL),
        "elimination_stack": [], "config": current_config,
        "basic_rules": BASIC_RULES,
        "spectators": current_spectators,
        "announcement_queue": []
    })
    broadcast_room_state(room_id)
    broadcast_room_list()

def background_timer():
    while True:
        eventlet.sleep(1)
        for room_id in list(rooms.keys()):
            room = rooms.get(room_id)
            if not room: continue
            if room["phase"] in ["PRE_GAME", "INPUT", "RULE_ANNOUNCEMENT", "END", "RESULT"]:
                if room["timer"] > 0:
                    room["timer"] -= 1
                    socketio.emit('timer_update', {"timer": room["timer"]}, room=room_id)
                else:
                    handle_timeout(room_id)

# --- Events ---

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('login')
def on_login(data):
    uid = data.get('uid')
    password = data.get('password')
    nickname = data.get('nickname', uid) 
    if not uid or not password: return
    with app.app_context():
        user = db.session.get(User, uid)
        if user:
            if user.password == password:
                emit('login_result', {'success': True, 'is_new': False, 'user': user.to_dict()})
            else:
                emit('login_result', {'success': False, 'msg': '密码错误'})
        else:
            new_user = User(id=uid, password=password, nickname=nickname)
            db.session.add(new_user)
            db.session.commit()
            emit('login_result', {'success': True, 'is_new': True, 'user': new_user.to_dict()})

@socketio.on('set_nickname')
def on_set_nickname(data):
    uid = data.get('uid')
    new_nick = data.get('nickname')
    with app.app_context():
        user = db.session.get(User, uid)
        if user:
            user.nickname = new_nick
            db.session.commit()
            emit('nickname_updated', {'user': user.to_dict()})
            
            for room in rooms.values():
                if uid in room["players"]:
                    room["players"][uid]["name"] = new_nick
                    broadcast_room_state(room["id"])
                    break
                # Update spectator name as well
                for spec in room["spectators"]:
                    if spec["uid"] == uid:
                        spec["name"] = new_nick
                        broadcast_room_state(room["id"])
                        break

@socketio.on('change_nickname')
def on_change_nickname(data):
    uid = data.get('uid')
    new_nick = data.get('new_nick')
    with app.app_context():
        user = db.session.get(User, uid)
        if user and user.score >= 1:
            user.score -= 1
            user.nickname = new_nick
            db.session.commit()
            emit('reroll_success', {'user': user.to_dict()})
            
            for room in rooms.values():
                if uid in room["players"]:
                    room["players"][uid]["name"] = new_nick
                    broadcast_room_state(room["id"])
                    break
                # Update spectator name
                for spec in room["spectators"]:
                    if spec["uid"] == uid:
                        spec["name"] = new_nick
                        broadcast_room_state(room["id"])
                        break
        else:
            emit('error_msg', {'msg': '积分不足'})

@socketio.on('change_password')
def on_change_password(data):
    uid = data.get('uid')
    new_pwd = data.get('new_password')
    with app.app_context():
        user = db.session.get(User, uid)
        if user:
            user.password = new_pwd
            db.session.commit()
            emit('password_changed', {'success': True})

@socketio.on('get_room_list')
def on_get_room_list():
    broadcast_room_list()

@socketio.on('create_room')
def on_create_room(data):
    if len(rooms) >= MAX_ROOMS:
        emit('error_msg', {'msg': '房间数量已达上限'})
        return
    room_name = data.get('name', 'Room')
    room_id = f"room_{int(time.time()*1000)}_{random.randint(100,999)}"
    rooms[room_id] = init_room_state(room_id, room_name)
    global timer_thread
    if not timer_thread:
        timer_thread = threading.Thread(target=background_timer, daemon=True)
        timer_thread.start()
    broadcast_room_list()
    emit('room_created', {'room_id': room_id})

@socketio.on('join_room')
def on_join_room_req(data):
    room_id = data.get('room_id')
    uid = data.get('uid')
    is_spectator = data.get('is_spectator', False)
    
    if room_id not in rooms: return
    room = rooms[room_id]
    
    join_room(room_id)
    SID_TO_ROOM[request.sid] = room_id
    SID_TO_UID[request.sid] = uid
    
    # 共同逻辑：获取最新昵称
    display_name = uid
    rank_info = {"title": "Unknown", "icon": "❓", "class": "text-gray-500", "is_max": False}
    current_score = 0
    with app.app_context():
        user = db.session.get(User, uid)
        if user: 
            rank_info = user.get_rank_info()
            display_name = user.nickname
            current_score = user.score

    if is_spectator:
        # FIX: 观战者存储为对象，包含名字
        if not any(s['uid'] == uid for s in room["spectators"]):
             room["spectators"].append({'uid': uid, 'name': display_name, 'likes_sent': 0})
        emit('joined_room_success', {'room_id': room_id, 'is_spectator': True})
        broadcast_room_state(room_id)
        return

    if uid in room["players"]:
        pass # Reconnect
    else:
        if len(room["players"]) >= MAX_PLAYERS: 
            emit('error_msg', {'msg': '房间已满'})
            return
        if room["phase"] != "LOBBY": 
            emit('error_msg', {'msg': '游戏进行中'})
            return

    if uid not in room["players"]:
        room["players"][uid] = {
            "uid": uid, "name": display_name, "hp": MAX_HP, "alive": True,
            "guess": None, "submitted": False, "confirmed": False, "ready": False,
            "last_dmg": 0, "is_winner": False, "likes": 0, "likes_sent": 0,
            "rank_info": rank_info, "points_change": 0,
            "suicided": False, "hp_at_death": 0,
            "score": current_score # FIX: 增加 score 字段到房间数据
        }
    
    emit('joined_room_success', {'room_id': room_id, 'is_spectator': False})
    broadcast_room_state(room_id)
    broadcast_room_list()

@socketio.on('identify')
def on_identify(data):
    uid = data.get('uid')
    if uid:
        SID_TO_UID[request.sid] = uid
        found_room = None
        is_spectator = False
        for room in rooms.values():
            if uid in room["players"]:
                found_room = room
                is_spectator = False
                break
            # 查找对象列表
            if any(s['uid'] == uid for s in room["spectators"]):
                found_room = room
                is_spectator = True
                break
        
        if found_room:
            SID_TO_ROOM[request.sid] = found_room["id"]
            join_room(found_room["id"])
            emit('reconnect_room', {'room': found_room, 'is_spectator': is_spectator})

@socketio.on('leave_room_req')
def on_leave_room_req():
    room = get_room_by_sid(request.sid)
    uid = SID_TO_UID.get(request.sid)
    if room and uid:
        leave_room(room["id"])
        if request.sid in SID_TO_ROOM: del SID_TO_ROOM[request.sid]
        
        if uid in room["players"]: del room["players"][uid]
        # FIX: 从对象列表中删除
        room["spectators"] = [s for s in room["spectators"] if s['uid'] != uid]
            
        if len(room["players"]) == 0 and room["phase"] == "LOBBY":
             del rooms[room["id"]]
        
        broadcast_room_state(room["id"])
        broadcast_room_list()
        emit('left_room_success')

@socketio.on('delete_room')
def on_delete_room(data):
    room_id = data.get('room_id')
    if room_id in rooms:
        if len(rooms[room_id]["players"]) == 0:
            del rooms[room_id]
            broadcast_room_list()
        else:
            emit('error_msg', {'msg': '无法删除有人的房间'})

@socketio.on('reroll_title')
def on_reroll_title(data):
    uid = data.get('uid')
    with app.app_context():
        user = db.session.get(User, uid)
        if user and user.score >= 200 and user.score >= 10:
            user.score -= 10
            user.ultimate_title = random.choice(ULTIMATE_PIG_NAMES)
            db.session.commit()
            emit('reroll_success', {'user': user.to_dict()})
        else:
            emit('error_msg', {'msg': '积分不足'})

@socketio.on('toggle_ready')
def on_toggle_ready():
    room = get_room_by_sid(request.sid)
    uid = SID_TO_UID.get(request.sid)
    if room and uid in room["players"] and room["phase"] == "LOBBY":
        room["players"][uid]["ready"] = not room["players"][uid]["ready"]
        broadcast_room_state(room["id"])

@socketio.on('vote_kick')
def on_vote_kick(data):
    room = get_room_by_sid(request.sid)
    sender_uid = SID_TO_UID.get(request.sid)
    target_uid = data.get('target_uid')
    if room and sender_uid in room["players"] and target_uid and room["phase"] == "LOBBY":
        if target_uid not in room["players"]: return
        if target_uid not in room["kick_votes"]: room["kick_votes"][target_uid] = []
        votes = room["kick_votes"][target_uid]
        if sender_uid in votes: votes.remove(sender_uid)
        else: votes.append(sender_uid)
        
        threshold = math.floor(len(room["players"]) / 2) + 1
        if len(votes) >= threshold:
            del room["players"][target_uid]
        
        broadcast_room_state(room["id"])
        broadcast_room_list()

@socketio.on('request_start_game')
def on_req_start():
    room = get_room_by_sid(request.sid)
    uid = SID_TO_UID.get(request.sid)
    if room and uid in room["players"] and room["phase"] == "LOBBY":
        if len(room["players"]) >= 3 and all(p["ready"] for p in room["players"].values()):
            start_pre_game(room["id"])

@socketio.on('confirm_rule')
def on_confirm():
    room = get_room_by_sid(request.sid)
    uid = SID_TO_UID.get(request.sid)
    if room and uid in room["players"]:
        room["players"][uid]["confirmed"] = True
        broadcast_room_state(room["id"])
        check_all_confirmed(room["id"])

@socketio.on('submit_guess')
def on_submit(data):
    room = get_room_by_sid(request.sid)
    uid = SID_TO_UID.get(request.sid)
    if room and uid in room["players"]:
        player = room["players"][uid]
        if not player["alive"]: return
        
        try:
            val = int(data.get('val'))
            if 0 <= val <= 100:
                player["guess"] = val
                player["submitted"] = True
                broadcast_room_state(room["id"])
                check_all_submitted(room["id"])
        except: pass

@socketio.on('suicide')
def on_suicide(data):
    room = get_room_by_sid(request.sid)
    uid = SID_TO_UID.get(request.sid)
    if room and uid in room["players"]:
        player = room["players"][uid]
        if not player["alive"]: return
        
        alive_count = sum(1 for p in room["players"].values() if p["alive"])
        if alive_count <= 2: return 

        player["suicided"] = True
        player["hp_at_death"] = player["hp"]
        player["hp"] = 0
        player["alive"] = False
        room["elimination_stack"].append(uid)
        
        selected_rule_id = int(data.get('rule_id'))
        rule_to_add = next((r for r in room["available_perm_rules"] if r["id"] == selected_rule_id), None)
        
        if rule_to_add:
            room["available_perm_rules"].remove(rule_to_add)
            trigger_room_rule(room, rule_to_add, author_name=player['name'])
            process_announcement_queue(room["id"])
        else:
            start_new_round(room["id"])

@socketio.on('send_emote')
def on_emote(data):
    room = get_room_by_sid(request.sid)
    if room:
        uid = data.get('uid')
        emote = data.get('emote')
        socketio.emit('player_emote', {'uid': uid, 'emote': emote[:4]}, room=room["id"])

@socketio.on('send_like')
def on_like(data):
    room = get_room_by_sid(request.sid)
    sender_uid = SID_TO_UID.get(request.sid)
    target_uid = data.get('target_uid')
    if room and sender_uid and target_uid:
        sender = None
        # FIX: 检查玩家 OR 观战者
        if sender_uid in room["players"]:
            sender = room["players"][sender_uid]
        else:
            sender = next((s for s in room["spectators"] if s['uid'] == sender_uid), None)
            
        if sender:
             target = room["players"].get(target_uid)
             # 简单的点赞逻辑，观战者也可以点赞，限制次数
             if target and sender["likes_sent"] < room["config"]["max_likes"]:
                sender["likes_sent"] += 1
                target["likes"] += 1
                broadcast_room_state(room["id"])
                socketio.emit('trigger_like_effect', {'target_uid': target_uid}, room=room["id"])

@socketio.on('admin_login')
def on_admin_login(data):
    print(f"DEBUG: Admin Login Attempt: {data.get('password')}")
    if data.get('password') == ADMIN_PASSWORD:
        emit('admin_auth_success', {'perm_pool': PERMANENT_RULE_POOL, 'temp_pool': ROUND_EVENT_POOL, 'config': {}})
    else:
        emit('admin_auth_fail')

@socketio.on('reset_game')
def on_reset_game():
    room = get_room_by_sid(request.sid)
    if room and room["phase"] == "END":
        perform_reset(room["id"])

@socketio.on('admin_command')
def on_admin(data):
    if data.get('password') != ADMIN_PASSWORD: return
    room = get_room_by_sid(request.sid)
    if not room: 
        emit('error_msg', {'msg': '请先进入游戏房间进行管理'})
        return
    
    cmd = data.get('cmd')
    if cmd == 'reset': perform_reset(room["id"])
    elif cmd == 'add_perm_rule':
         rule_id = data.get('rule_id')
         rule_to_add = next((r for r in PERMANENT_RULE_POOL if r["id"] == rule_id), None)
         if rule_to_add:
             if room["phase"] in ["LOBBY", "PRE_GAME"]:
                 if rule_to_add in room["available_perm_rules"]:
                     room["available_perm_rules"].remove(rule_to_add)
                 trigger_room_rule(room, rule_to_add) 
                 
                 if room["phase"] != "LOBBY":
                     process_announcement_queue(room["id"])
                 else:
                     broadcast_room_state(room["id"])
             else:
                 if rule_id not in room["pending_events"]["perm"]:
                     room["pending_events"]["perm"].append(rule_id)

    elif cmd == 'add_temp_rule':
        room["pending_events"]["temp"] = data.get('rule_id')
    elif cmd == 'update_config':
        room["config"]["max_likes"] = int(data.get("max_likes", 10))
        broadcast_room_state(room["id"])

@socketio.on('get_history')
def on_get_history(data):
    uid = data.get('uid')
    with app.app_context():
        records = GameRecord.query.order_by(GameRecord.timestamp.desc()).all()
        user_history = []
        for r in records:
            try:
                players_data = json.loads(r.players_json)
                player_rec = next((p for p in players_data if p['uid'] == uid), None)
                if player_rec:
                    user_history.append({
                        'id': r.id,
                        'time': r.timestamp.strftime("%Y-%m-%d %H:%M"),
                        'score_change': player_rec['score_change'],
                        'rank': player_rec['rank'],
                        'game_rank': player_rec.get('game_rank', '-'),
                        'total_players': player_rec.get('total_players', '-'),
                        'is_suicide': player_rec.get('is_suicide', False),
                    })
            except: continue
        emit('history_data', user_history)

if __name__ == '__main__':
    socketio.run(app, debug=True, host='0.0.0.0', port=5002)
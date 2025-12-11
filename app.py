import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit
import time
import threading
import random

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'

socketio = SocketIO(app, cors_allowed_origins="*")

ADMIN_PASSWORD = "110120119" 

MAX_HP = 10
MAX_PLAYERS = 8
TIME_LIMIT_ROUND = 30
TIME_LIMIT_PREGAME = 60
TIME_LIMIT_RULE = 5
TIME_LIMIT_GAMEOVER = 60 

SID_TO_UID = {} 

game_state = {
    "phase": "LOBBY", 
    "round": 0,
    "timer": 0,
    "players": {}, 
    "rules": [],       
    "new_rule": None,  
    "round_event": None, 
    "multiplier": 0.8,
    "dead_guesses": [],
    "blind_mode": False,
    "logs": [],     
    "last_result": {},
    "full_history": []
}

BASIC_RULES = [
    "每轮选取 0 至 100 之间的整数。",
    "目标值为全员平均数的 X 倍 (默认为 0.8)。",
    "最接近目标者获胜，其余玩家扣除 1 点生命。",
    "每回合可能触发随机限定规则。",
    "玩家淘汰时，追加永久规则。"
]

PERMANENT_RULE_POOL = [
    {"id": 1, "desc": "【冲突】若数字重复，则选择无效并扣除 1 点生命。", "type": "perm"},
    {"id": 2, "desc": "【精准】若赢家误差小于 1，败者将扣除 2 点生命。", "type": "perm"},
    {"id": 3, "desc": "【极值】若 0 与 100 同时出现，选 100 的人为本回合胜者。", "type": "perm"},
    {"id": 4, "desc": "【幽灵】已淘汰玩家的最后数字将永远参与均值计算。", "type": "perm"},
    {"id": 5, "desc": "【绝境】HP < 3 的玩家，其数字对均值的权重变为 3 倍。", "type": "perm"}
]

ROUND_EVENT_POOL = [
    {"id": 101, "desc": "【混乱】两人选择数字交换！", "type": "temp"},
    {"id": 102, "desc": "【波动】本回合目标倍率发生突变！", "type": "temp"},
    {"id": 103, "desc": "【安全】选 40-60 时免除伤害，且本回合胜者 +1 HP！", "type": "temp"},
    {"id": 104, "desc": "【黑暗】隐藏全员 HP，且无法看到自己选择的数字！", "type": "temp"}
]

current_perm_pool = list(PERMANENT_RULE_POOL)
timer_thread = None

def get_player_by_sid(sid):
    uid = SID_TO_UID.get(sid)
    if uid and uid in game_state["players"]:
        return game_state["players"][uid]
    return None

def background_timer():
    global timer_thread
    while True:
        eventlet.sleep(1)
        current_phase = game_state["phase"]
        if current_phase in ["PRE_GAME", "INPUT", "RULE_ANNOUNCEMENT", "END"]:
            if game_state["timer"] > 0:
                game_state["timer"] -= 1
                socketio.emit('timer_update', {"timer": game_state["timer"]})
            else:
                handle_timeout(current_phase)

def handle_timeout(phase):
    if phase == "PRE_GAME":
        start_new_round()
    elif phase == "RULE_ANNOUNCEMENT":
        start_new_round()
    elif phase == "INPUT":
        calculate_round()
    elif phase == "END":
        perform_reset()

def perform_reset():
    global game_state, current_perm_pool, SID_TO_UID
    game_state["players"] = {} 
    SID_TO_UID = {} 
    game_state["phase"] = "LOBBY"
    game_state["round"] = 0
    game_state["rules"] = []
    game_state["logs"] = []
    game_state["new_rule"] = None
    game_state["round_event"] = None
    game_state["multiplier"] = 0.8
    game_state["dead_guesses"] = []
    game_state["blind_mode"] = False
    game_state["full_history"] = []
    current_perm_pool = list(PERMANENT_RULE_POOL)
    broadcast_state()

def start_new_round():
    game_state["phase"] = "INPUT"
    game_state["round"] += 1
    game_state["timer"] = TIME_LIMIT_ROUND
    
    for p in game_state["players"].values():
        p["submitted"] = False
        p["guess"] = None
    
    game_state["multiplier"] = 0.8
    game_state["round_event"] = None
    game_state["blind_mode"] = False

    alive_count = sum(1 for p in game_state["players"].values() if p["alive"])
    
    # 2人时 70% 概率触发混乱
    if alive_count == 2 and random.random() < 0.7:
        chaos_event = next(e for e in ROUND_EVENT_POOL if e["id"] == 101)
        apply_round_event(chaos_event)
    # 常规 30% 概率
    elif random.random() < 0.4:
        other_events = [e for e in ROUND_EVENT_POOL if e["id"] != 101]
        if other_events:
            event = random.choice(other_events)
            apply_round_event(event)
    
    broadcast_state()

def apply_round_event(event):
    event_copy = event.copy()
    game_state["round_event"] = event_copy
    if event_copy["id"] == 102:
        new_mult = round(random.randint(1, 20) * 0.1, 1)
        game_state["multiplier"] = new_mult
        event_copy["desc"] = f"【波动】本回合目标倍率变更为 x{new_mult} !"
    elif event_copy["id"] == 104:
        game_state["blind_mode"] = True

def check_all_ready():
    players = game_state["players"]
    ready_count = sum(1 for p in players.values() if p["ready"])
    if len(players) < 3: return
    if len(players) == ready_count:
        start_pre_game()

def start_pre_game():
    for p in game_state["players"].values():
        p["confirmed"] = False
    game_state["phase"] = "PRE_GAME"
    game_state["round"] = 0
    game_state["timer"] = TIME_LIMIT_PREGAME
    
    global timer_thread
    if not timer_thread:
        timer_thread = threading.Thread(target=background_timer, daemon=True)
        timer_thread.start()
    broadcast_state()

def check_all_submitted():
    alive = [p for p in game_state["players"].values() if p["alive"]]
    if not alive: return
    if all(p["submitted"] for p in alive):
        calculate_round()

def check_all_confirmed():
    alive = [p for p in game_state["players"].values() if p["alive"]]
    if not alive: return
    if all(p.get("confirmed", False) for p in alive):
        start_new_round()

def calculate_round():
    players = game_state["players"]
    alive = [p for p in players.values() if p["alive"]]
    
    if not alive: 
        game_state["phase"] = "END"
        game_state["timer"] = TIME_LIMIT_GAMEOVER
        broadcast_state()
        return

    guesses = []
    for p in alive:
        val = p["guess"]
        if val is None: val = random.randint(0, 100)
        guesses.append({"player": p, "val": val, "org_val": val, "source": p["name"]}) 
    
    log_msg = f"R{game_state['round']}"
    
    # --- 修复逻辑：强制混乱交换 (错排算法) ---
    if game_state["round_event"] and game_state["round_event"]["id"] == 101 and len(guesses) > 1:
        # 创建索引列表 [0, 1, 2...]
        indices = list(range(len(guesses)))
        
        # 尝试打乱，直到没有任何一个索引在原来的位置 (错排)
        # 例如：[0, 1] 必须变成 [1, 0]，不能是 [0, 1]
        is_fixed_point = True
        while is_fixed_point:
            random.shuffle(indices)
            is_fixed_point = False
            for i, new_idx in enumerate(indices):
                if i == new_idx:
                    is_fixed_point = True
                    break
        
        # 应用交换：先备份原始数据，再根据打乱的索引赋值
        original_data = [(g["val"], g["player"]["name"]) for g in guesses]
        
        for i, g in enumerate(guesses):
            target_idx = indices[i]
            g["val"] = original_data[target_idx][0]
            g["source"] = original_data[target_idx][1]
            
        log_msg += " | ⚡交换触发"

    rule_ids = [r["id"] for r in game_state["rules"]]
    total_val = 0
    total_w = 0
    values = [] 
    
    for g in guesses:
        values.append(g['val'])
        w = 3 if (5 in rule_ids and g['player']['hp'] < 3) else 1
        total_val += g['val'] * w
        total_w += w
        
    if 4 in rule_ids:
        for ghost_val in game_state["dead_guesses"]:
            total_val += ghost_val
            total_w += 1

    avg = total_val / total_w if total_w else 0
    current_mult = game_state["multiplier"]
    target = avg * current_mult
    
    log_msg += f": 均值 {avg:.2f} x {current_mult} -> 目标 {target:.2f}"
    
    winners = []
    base_damage = 1

    rule3_triggered = False
    if 3 in rule_ids and 0 in values and 100 in values:
        winners = [g["player"] for g in guesses if g["val"] == 100]
        rule3_triggered = True
        log_msg += " | 极值(100胜)"

    if not rule3_triggered:
        candidates = guesses[:]
        if 1 in rule_ids: 
            counts = {x: values.count(x) for x in values}
            conflict_vals = [v for v, c in counts.items() if c > 1]
            candidates = [g for g in guesses if counts[g['val']] == 1]
            if conflict_vals:
                log_msg += " | 冲突"

        if not candidates:
            winners = []
        else:
            candidates.sort(key=lambda x: abs(x['val'] - target))
            min_diff = abs(candidates[0]['val'] - target)
            winners = [x['player'] for x in candidates if abs(x['val'] - target) == min_diff]
            if 2 in rule_ids and min_diff < 1: 
                base_damage = 2
                log_msg += " | 精准"

    round_details = []
    for p in alive:
        player_guess_data = next(g for g in guesses if g['player'] == p)
        is_winner = p in winners
        actual_dmg = 0
        
        if is_winner:
            if game_state["round_event"] and game_state["round_event"]["id"] == 103:
                p["hp"] = min(MAX_HP, p["hp"] + 1)
        else:
            actual_dmg = base_damage
            if game_state["round_event"] and game_state["round_event"]["id"] == 103:
                if 40 <= player_guess_data["val"] <= 60:
                    actual_dmg = 0
            p["hp"] -= actual_dmg
        
        p["last_dmg"] = actual_dmg
        p["is_winner"] = is_winner
        
        round_details.append({
            "name": p["name"],
            "val": player_guess_data["val"],
            "org_val": player_guess_data["org_val"],
            "source": player_guess_data["source"], 
            "hp": p["hp"],
            "dmg": actual_dmg,
            "win": is_winner
        })

    round_history = {
        "round_num": game_state["round"],
        "target": round(target, 2),
        "avg": round(avg, 2),
        "event_desc": game_state["round_event"]["desc"] if game_state["round_event"] else None,
        "active_rules": [r["desc"] for r in game_state["rules"]],
        "player_data": round_details
    }
    game_state["full_history"].append(round_history)

    newly_dead = [p for p in players.values() if p["hp"] <= 0 and p["alive"]]
    game_state["new_rule"] = None 

    triggered_new_rule = False
    for p in newly_dead:
        p["alive"] = False
        dead_val = next((d['val'] for d in round_details if d['name'] == p['name']), 0)
        game_state["dead_guesses"].append(dead_val)

        if current_perm_pool and not triggered_new_rule: 
            random_index = random.randint(0, len(current_perm_pool) - 1)
            new_rule = current_perm_pool.pop(random_index)
            trigger_perm_rule(new_rule, log_msg)
            triggered_new_rule = True

    game_state["last_result"] = {
        "avg": round(avg, 2),
        "target": round(target, 2),
        "details": round_details,
        "log": log_msg
    }
    game_state["logs"].insert(0, log_msg)
    game_state["phase"] = "RESULT"
    broadcast_state()
    threading.Timer(5, after_result_display, [triggered_new_rule]).start()

def trigger_perm_rule(new_rule, log_msg_append=""):
    game_state["rules"].append(new_rule)
    game_state["new_rule"] = new_rule
    if log_msg_append:
        log_msg_append += f" | ⚠永久规则: {new_rule['desc']}"

def after_result_display(has_new_rule):
    alive_count = sum(1 for p in game_state["players"].values() if p["alive"])
    if alive_count <= 1:
        game_state["phase"] = "END"
        game_state["timer"] = TIME_LIMIT_GAMEOVER
    elif has_new_rule:
        game_state["phase"] = "RULE_ANNOUNCEMENT"
        game_state["timer"] = TIME_LIMIT_RULE
    else:
        start_new_round()
    broadcast_state()

def broadcast_state():
    socketio.emit('state_update', game_state)

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('connect')
def on_connect():
    emit('state_update', game_state)
    emit('init_config', {'basic_rules': BASIC_RULES})

@socketio.on('identify')
def on_identify(data):
    uid = data.get('uid')
    if uid:
        SID_TO_UID[request.sid] = uid
        if uid in game_state["players"]:
            broadcast_state()

@socketio.on('join')
def on_join(data):
    uid = data.get('uid')
    if not uid: return
    SID_TO_UID[request.sid] = uid
    if uid in game_state["players"]:
        broadcast_state()
        return
    if game_state["phase"] != "LOBBY": return
    if len(game_state["players"]) >= MAX_PLAYERS: return
    name = data.get('name', f'Player')
    game_state["players"][uid] = {
        "name": name, "hp": MAX_HP, "alive": True,
        "guess": None, "submitted": False, 
        "confirmed": False, "ready": False,
        "last_dmg": 0, "is_winner": False
    }
    broadcast_state()

@socketio.on('leave_game')
def on_leave(data):
    uid = data.get('uid')
    if uid and uid in game_state["players"]:
        del game_state["players"][uid]
        keys_to_remove = [k for k,v in SID_TO_UID.items() if v == uid]
        for k in keys_to_remove: del SID_TO_UID[k]
        broadcast_state()

# --- 修复：添加 broadcast=True，让所有人都能收到表情 ---
@socketio.on('send_emote')
def on_send_emote(data):
    uid = data.get('uid')
    emote = data.get('emote')
    if uid and uid in game_state["players"] and emote:
        emit('player_emote', {'uid': uid, 'emote': emote[:4]}, broadcast=True)

@socketio.on('toggle_ready')
def on_toggle_ready():
    if game_state["phase"] != "LOBBY": return
    p = get_player_by_sid(request.sid)
    if p:
        p["ready"] = not p["ready"]
        broadcast_state()
        check_all_ready()

@socketio.on('confirm_rule')
def on_confirm_rule():
    p = get_player_by_sid(request.sid)
    if p:
        p["confirmed"] = True
        broadcast_state()
        check_all_confirmed()

@socketio.on('submit_guess')
def on_submit(data):
    p = get_player_by_sid(request.sid)
    if p and p["alive"]:
        try:
            val = int(data.get('val'))
            if 0 <= val <= 100:
                p["guess"] = val
                p["submitted"] = True
                broadcast_state()
                check_all_submitted()
        except: pass

@socketio.on('admin_login')
def on_admin_login(data):
    if data.get('password') == ADMIN_PASSWORD:
        emit('admin_auth_success', {'perm_pool': current_perm_pool, 'temp_pool': ROUND_EVENT_POOL})
    else:
        emit('admin_auth_fail')

@socketio.on('admin_command')
def on_admin_command(data):
    if data.get('password') != ADMIN_PASSWORD: return
    cmd = data.get('cmd')
    if cmd == 'reset': perform_reset()
    elif cmd == 'add_perm_rule':
        rule_id = data.get('rule_id')
        rule_to_add = next((r for r in current_perm_pool if r["id"] == rule_id), None)
        if rule_to_add:
            current_perm_pool.remove(rule_to_add)
            trigger_perm_rule(rule_to_add)
            game_state["phase"] = "RULE_ANNOUNCEMENT"
            game_state["timer"] = TIME_LIMIT_RULE
            broadcast_state()
    elif cmd == 'add_temp_rule':
        rule_id = data.get('rule_id')
        event = next((r for r in ROUND_EVENT_POOL if r["id"] == rule_id), None)
        if event:
            apply_round_event(event)
            broadcast_state()
    elif cmd == 'refresh_pool':
         emit('admin_pool_update', {'perm_pool': current_perm_pool, 'temp_pool': ROUND_EVENT_POOL})

if __name__ == '__main__':
    socketio.run(app, debug=True, host='0.0.0.0', port=5002)
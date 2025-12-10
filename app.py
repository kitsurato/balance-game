from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit
import time
import threading

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# --- 游戏配置 ---
MAX_HP = 10
TIME_LIMIT_ROUND = 30
TIME_LIMIT_PREGAME = 60
TIME_LIMIT_RULE = 20

game_state = {
    "phase": "LOBBY",
    "round": 0,
    "timer": 0,
    "players": {}, 
    "rules": [],
    "new_rule": None,
    "logs": [],     
    "last_result": {} 
}

# --- 文案优化后的规则 ---
BASIC_RULES = [
    "每轮选取 0 至 100 之间的整数。",
    "目标值为全员平均数的 0.8 倍。",
    "最接近目标者获胜，其余玩家扣除 1 点生命。",
    "玩家淘汰时，自动追加隐藏规则。"
]

RULE_POOL = [
    {"id": 1, "desc": "【冲突】若数字重复，则选择无效并扣除 1 点生命。"},
    {"id": 2, "desc": "【精准】若赢家误差小于 1，败者将扣除 2 点生命。"},
    {"id": 3, "desc": "【极值】若 0 与 100 同时出现，选 100 者直接获胜。"}
]

timer_thread = None

def background_timer():
    global timer_thread
    while True:
        time.sleep(1)
        current_phase = game_state["phase"]
        
        if current_phase in ["PRE_GAME", "INPUT", "RULE_ANNOUNCEMENT"]:
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

def start_new_round():
    game_state["phase"] = "INPUT"
    game_state["round"] += 1
    game_state["timer"] = TIME_LIMIT_ROUND
    # 重置输入状态
    for p in game_state["players"].values():
        p["submitted"] = False
        p["guess"] = None
    broadcast_state()

def check_all_submitted():
    alive = [p for p in game_state["players"].values() if p["alive"]]
    if not alive: return
    if all(p["submitted"] for p in alive):
        calculate_round()

def check_all_confirmed():
    """检查是否所有人都确认了规则"""
    alive = [p for p in game_state["players"].values() if p["alive"]]
    if not alive: return
    
    # 如果所有存活玩家都 confirm 了
    if all(p.get("confirmed", False) for p in alive):
        # 立即开始游戏，跳过剩余时间
        start_new_round()

def calculate_round():
    players = game_state["players"]
    alive = [p for p in players.values() if p["alive"]]
    
    if not alive: 
        game_state["phase"] = "END"
        broadcast_state()
        return

    guesses = []
    for p in alive:
        val = p["guess"] if p["guess"] is not None else 0
        guesses.append({"player": p, "val": val})
    
    values = [g["val"] for g in guesses]
    avg = sum(values) / len(values) if values else 0
    target = avg * 0.8
    
    winners = []
    base_damage = 1
    log_msg = f"R{game_state['round']}: 均值 {avg:.2f} -> 目标 {target:.2f}"
    
    rule_ids = [r["id"] for r in game_state["rules"]]

    # 规则判断
    rule3_triggered = False
    if 3 in rule_ids and 0 in values and 100 in values:
        winners = [g["player"] for g in guesses if g["val"] == 100]
        rule3_triggered = True
        log_msg += " | 极值触发(100胜)"

    if not rule3_triggered:
        candidates = guesses[:]
        if 1 in rule_ids:
            counts = {x: values.count(x) for x in values}
            conflict_vals = [v for v, c in counts.items() if c > 1]
            candidates = [g for g in guesses if counts[g['val']] == 1]
            if conflict_vals:
                log_msg += " | 冲突发生"

        if not candidates:
            winners = []
        else:
            candidates.sort(key=lambda x: abs(x['val'] - target))
            min_diff = abs(candidates[0]['val'] - target)
            winners = [x['player'] for x in candidates if abs(x['val'] - target) == min_diff]
            
            if 2 in rule_ids and min_diff < 1:
                base_damage = 2
                log_msg += " | 精准打击(伤害x2)"

    round_details = []
    for p in alive:
        is_winner = p in winners
        actual_dmg = 0
        if not is_winner:
            actual_dmg = base_damage
            p["hp"] -= actual_dmg
        
        p["last_dmg"] = actual_dmg
        p["is_winner"] = is_winner
        
        round_details.append({
            "name": p["name"],
            "val": p["guess"] if p["guess"] is not None else 0,
            "hp": p["hp"],
            "dmg": actual_dmg,
            "win": is_winner
        })

    newly_dead = [p for p in players.values() if p["hp"] <= 0 and p["alive"]]
    game_state["new_rule"] = None 

    triggered_new_rule = False
    for p in newly_dead:
        p["alive"] = False
        if RULE_POOL and not triggered_new_rule: 
            new_rule = RULE_POOL.pop(0)
            game_state["rules"].append(new_rule)
            game_state["new_rule"] = new_rule
            log_msg += f" | ⚠新规则: {new_rule['desc']}"
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

def after_result_display(has_new_rule):
    alive_count = sum(1 for p in game_state["players"].values() if p["alive"])
    
    if alive_count <= 1:
        game_state["phase"] = "END"
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

@socketio.on('join')
def on_join(data):
    if game_state["phase"] != "LOBBY": return
    if len(game_state["players"]) >= 5: return
    
    sid = request.sid
    name = data.get('name', f'Player {len(game_state["players"])+1}')
    
    game_state["players"][sid] = {
        "name": name, "hp": MAX_HP, "alive": True,
        "guess": None, "submitted": False, "confirmed": False, # 新增确认字段
        "last_dmg": 0, "is_winner": False
    }
    broadcast_state()

@socketio.on('start_game')
def on_start():
    if len(game_state["players"]) < 3: return
    
    # 重置所有人的 confirmed 状态
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

@socketio.on('confirm_rule')
def on_confirm_rule():
    """玩家确认规则"""
    sid = request.sid
    if sid in game_state["players"]:
        game_state["players"][sid]["confirmed"] = True
        broadcast_state()
        check_all_confirmed() # 检查是否全员通过

@socketio.on('submit_guess')
def on_submit(data):
    sid = request.sid
    if sid not in game_state["players"]: return
    player = game_state["players"][sid]
    if not player["alive"]: return
    
    try:
        val = int(data.get('val'))
        if 0 <= val <= 100:
            player["guess"] = val
            player["submitted"] = True
            broadcast_state()
            check_all_submitted()
    except: pass

@socketio.on('reset_game')
def on_reset():
    global game_state, RULE_POOL
    game_state["phase"] = "LOBBY"
    game_state["round"] = 0
    game_state["players"] = {}
    game_state["rules"] = []
    game_state["logs"] = []
    game_state["new_rule"] = None
    RULE_POOL = [
        {"id": 1, "desc": "【冲突】若数字重复，则选择无效并扣除 1 点生命。"},
        {"id": 2, "desc": "【精准】若赢家误差小于 1，败者将扣除 2 点生命。"},
        {"id": 3, "desc": "【极值】若 0 与 100 同时出现，选 100 者直接获胜。"}
    ]
    broadcast_state()

if __name__ == '__main__':
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)
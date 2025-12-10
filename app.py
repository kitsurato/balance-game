from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit
import time
import threading

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# --- 管理员配置 ---
ADMIN_PASSWORD = "admin"  # 这里设置你的管理员密码

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

BASIC_RULES = [
    "每轮选取 0 至 100 之间的整数。",
    "目标值为全员平均数的 0.8 倍。",
    "最接近目标者获胜，其余玩家扣除 1 点生命。",
    "玩家淘汰时，自动追加隐藏规则。"
]

# 初始规则库备份，用于重置
INITIAL_RULE_POOL = [
    {"id": 1, "desc": "【冲突】若数字重复，则选择无效并扣除 1 点生命。"},
    {"id": 2, "desc": "【精准】若赢家误差小于 1，败者将扣除 2 点生命。"},
    {"id": 3, "desc": "【极值】若 0 与 100 同时出现，选 100 者直接获胜。"},
    {"id": 4, "desc": "【盲盒】本回合所有玩家无法看到自己的输入数值。"},
    {"id": 5, "desc": "【狂暴】本回合所有扣血伤害 +1。"}
]

# 当前剩余的规则池
current_rule_pool = list(INITIAL_RULE_POOL)

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
    alive = [p for p in game_state["players"].values() if p["alive"]]
    if not alive: return
    if all(p.get("confirmed", False) for p in alive):
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

    # 规则: 狂暴
    if 5 in rule_ids:
        base_damage += 1
        log_msg += " | 狂暴(+1伤)"

    # 规则: 极值
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

    # 处理死亡与自动规则触发
    newly_dead = [p for p in players.values() if p["hp"] <= 0 and p["alive"]]
    game_state["new_rule"] = None 

    triggered_new_rule = False
    for p in newly_dead:
        p["alive"] = False
        # 仅当还有规则且本轮未触发过时触发
        if current_rule_pool and not triggered_new_rule: 
            new_rule = current_rule_pool.pop(0)
            trigger_rule_event(new_rule, log_msg)
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

def trigger_rule_event(new_rule, log_msg_append=""):
    """激活新规则并触发公告"""
    game_state["rules"].append(new_rule)
    game_state["new_rule"] = new_rule
    if log_msg_append:
        log_msg_append += f" | ⚠新规则: {new_rule['desc']}"

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

# --- 路由与事件 ---

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
        "guess": None, "submitted": False, "confirmed": False,
        "last_dmg": 0, "is_winner": False
    }
    broadcast_state()

@socketio.on('start_game')
def on_start():
    if len(game_state["players"]) < 3: return
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
    sid = request.sid
    if sid in game_state["players"]:
        game_state["players"][sid]["confirmed"] = True
        broadcast_state()
        check_all_confirmed()

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

# --- 管理员相关事件 ---

@socketio.on('admin_login')
def on_admin_login(data):
    """管理员登录验证"""
    if data.get('password') == ADMIN_PASSWORD:
        # 登录成功，发送当前的规则池给管理员
        emit('admin_auth_success', {'pool': current_rule_pool})
    else:
        emit('admin_auth_fail')

@socketio.on('admin_command')
def on_admin_command(data):
    """处理管理员指令"""
    if data.get('password') != ADMIN_PASSWORD:
        return

    cmd = data.get('cmd')
    
    if cmd == 'reset':
        # 立即重置游戏
        global game_state, current_rule_pool
        game_state["phase"] = "LOBBY"
        game_state["round"] = 0
        game_state["players"] = {}
        game_state["rules"] = []
        game_state["logs"] = []
        game_state["new_rule"] = None
        current_rule_pool = list(INITIAL_RULE_POOL)
        broadcast_state()
        
    elif cmd == 'add_rule':
        # 强制添加规则
        rule_id = data.get('rule_id')
        # 在当前池中查找该规则
        rule_to_add = next((r for r in current_rule_pool if r["id"] == rule_id), None)
        
        if rule_to_add:
            current_rule_pool.remove(rule_to_add) # 从池中移除
            trigger_rule_event(rule_to_add) # 激活
            # 强制进入公告阶段，让所有人看到
            game_state["phase"] = "RULE_ANNOUNCEMENT"
            game_state["timer"] = TIME_LIMIT_RULE
            broadcast_state()
            
    elif cmd == 'refresh_pool':
         emit('admin_pool_update', {'pool': current_rule_pool})

if __name__ == '__main__':
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)
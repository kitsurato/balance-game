# --- 必须放在第一行！解决 Render/Gunicorn 部署报错的关键 ---
import eventlet
eventlet.monkey_patch()
# -------------------------------------------------------

from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit
import time
import threading
import random

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'

socketio = SocketIO(app, cors_allowed_origins="*")

# --- 管理员密码 ---
ADMIN_PASSWORD = "admin" 

# --- 游戏配置 ---
MAX_HP = 10
TIME_LIMIT_ROUND = 30
TIME_LIMIT_PREGAME = 60
TIME_LIMIT_RULE = 5  # 修改：缩短为 5秒

game_state = {
    "phase": "LOBBY", 
    "round": 0,
    "timer": 0,
    "players": {}, 
    "rules": [],       # 永久生效的规则
    "new_rule": None,  # 新增的永久规则（用于展示）
    "round_event": None, # 本回合限定规则 (新增)
    "multiplier": 0.8,   # 当前回合倍率 (新增)
    "logs": [],     
    "last_result": {} 
}

BASIC_RULES = [
    "每轮选取 0 至 100 之间的整数。",
    "目标值为全员平均数的 X 倍 (默认为 0.8)。",
    "最接近目标者获胜，其余玩家扣除 1 点生命。",
    "玩家淘汰时，追加永久规则；每回合可能触发随机限定规则。"
]

# --- 规则库 ---

# 1. 永久规则池 (有人淘汰时触发)
PERMANENT_RULE_POOL = [
    {"id": 1, "desc": "【冲突】若数字重复，则选择无效并扣除 1 点生命。", "type": "perm"},
    {"id": 2, "desc": "【精准】若赢家误差小于 1，败者将扣除 2 点生命。", "type": "perm"},
    {"id": 3, "desc": "【极值】若 0 与 100 同时出现，选 100 者直接获胜。", "type": "perm"}
]

# 2. 回合限定事件池 (每回合随机触发)
ROUND_EVENT_POOL = [
    {"id": 101, "desc": "【混乱】本回合所有人的数字将随机互换！", "type": "temp"},
    {"id": 102, "desc": "【波动】本回合目标倍率发生突变！", "type": "temp"}
]

# 当前剩余的永久规则
current_perm_pool = list(PERMANENT_RULE_POOL)
timer_thread = None

def background_timer():
    global timer_thread
    while True:
        eventlet.sleep(1)
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
    
    # --- 回合初始化逻辑 ---
    # 1. 重置玩家提交状态
    for p in game_state["players"].values():
        p["submitted"] = False
        p["guess"] = None
    
    # 2. 重置回合参数
    game_state["multiplier"] = 0.8
    game_state["round_event"] = None

    # 3. 随机触发回合限定规则 (30% 概率)
    # 管理员也可以通过后台强制注入，所以这里判断一下是否已经有被注入的事件
    if random.random() < 0.3: 
        event = random.choice(ROUND_EVENT_POOL)
        apply_round_event(event)
    
    broadcast_state()

def apply_round_event(event):
    """应用回合限定规则"""
    game_state["round_event"] = event
    
    # 处理倍率波动逻辑
    if event["id"] == 102:
        # 生成 0.1 到 2.0 之间的随机数，步长 0.1
        new_mult = round(random.randint(1, 20) * 0.1, 1)
        game_state["multiplier"] = new_mult
        event["desc"] = f"【波动】本回合目标倍率变更为 x{new_mult} !"

def check_all_ready():
    """检查是否所有人都准备好了"""
    players = game_state["players"]
    if len(players) < 3: return
    
    if all(p["ready"] for p in players.values()):
        # 全员准备就绪，开始游戏
        start_pre_game()

def start_pre_game():
    for p in game_state["players"].values():
        p["confirmed"] = False # 重置规则确认状态
    
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
        broadcast_state()
        return

    # 1. 收集原始数据
    # guesses 结构: [{'player': Obj, 'val': 50, 'org_val': 50}]
    guesses = []
    for p in alive:
        val = p["guess"] if p["guess"] is not None else 0
        guesses.append({"player": p, "val": val, "org_val": val}) # org_val用于记录交换前的数字
    
    log_msg = f"R{game_state['round']}"
    
    # --- 处理【混乱】事件 (数字交换) ---
    if game_state["round_event"] and game_state["round_event"]["id"] == 101:
        # 提取所有填写的数字
        raw_values = [g["val"] for g in guesses]
        # 打乱数字
        random.shuffle(raw_values)
        # 重新分配
        for i, g in enumerate(guesses):
            g["val"] = raw_values[i]
        log_msg += " | ⚡数字已互换"

    # 2. 计算平均值和目标值
    values = [g["val"] for g in guesses]
    avg = sum(values) / len(values) if values else 0
    
    # 使用动态倍率
    current_mult = game_state["multiplier"]
    target = avg * current_mult
    
    log_msg += f": 均值 {avg:.2f} x {current_mult} -> 目标 {target:.2f}"
    
    winners = []
    base_damage = 1
    rule_ids = [r["id"] for r in game_state["rules"]]

    # --- 永久规则判断 ---
    
    # 规则: 极值
    rule3_triggered = False
    if 3 in rule_ids and 0 in values and 100 in values:
        winners = [g["player"] for g in guesses if g["val"] == 100]
        rule3_triggered = True
        log_msg += " | 极值触发(100胜)"

    if not rule3_triggered:
        candidates = guesses[:]
        # 规则: 冲突
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
            
            # 规则: 精准
            if 2 in rule_ids and min_diff < 1:
                base_damage = 2
                log_msg += " | 精准打击(伤害x2)"

    # 3. 结算与记录
    round_details = []
    for p in alive:
        # 找到该玩家对应的数据对象（可能被交换过数字）
        player_guess_data = next(g for g in guesses if g['player'] == p)
        
        is_winner = p in winners
        actual_dmg = 0
        if not is_winner:
            actual_dmg = base_damage
            p["hp"] -= actual_dmg
        
        p["last_dmg"] = actual_dmg
        p["is_winner"] = is_winner
        
        round_details.append({
            "name": p["name"],
            "val": player_guess_data["val"],      # 实际参与计算的数字
            "org_val": player_guess_data["org_val"], # 原本选择的数字(用于展示交换效果)
            "hp": p["hp"],
            "dmg": actual_dmg,
            "win": is_winner
        })

    # 4. 死亡判定与新永久规则
    newly_dead = [p for p in players.values() if p["hp"] <= 0 and p["alive"]]
    game_state["new_rule"] = None 

    triggered_new_rule = False
    for p in newly_dead:
        p["alive"] = False
        if current_perm_pool and not triggered_new_rule: 
            new_rule = current_perm_pool.pop(0)
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
    elif has_new_rule:
        game_state["phase"] = "RULE_ANNOUNCEMENT"
        game_state["timer"] = TIME_LIMIT_RULE
    else:
        start_new_round()
    
    broadcast_state()

def broadcast_state():
    socketio.emit('state_update', game_state)

# --- 路由 ---
@app.route('/')
def index():
    return render_template('index.html')

# --- Socket 事件 ---
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
        "guess": None, "submitted": False, 
        "confirmed": False, "ready": False, # 新增 ready 状态
        "last_dmg": 0, "is_winner": False
    }
    broadcast_state()

@socketio.on('toggle_ready')
def on_toggle_ready():
    """玩家切换准备状态"""
    if game_state["phase"] != "LOBBY": return
    sid = request.sid
    if sid in game_state["players"]:
        p = game_state["players"][sid]
        p["ready"] = not p["ready"]
        broadcast_state()
        check_all_ready()

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

# --- 管理员事件 ---
@socketio.on('admin_login')
def on_admin_login(data):
    if data.get('password') == ADMIN_PASSWORD:
        # 管理员不仅可以看到永久规则池，也可以看到临时事件池
        emit('admin_auth_success', {'perm_pool': current_perm_pool, 'temp_pool': ROUND_EVENT_POOL})
    else:
        emit('admin_auth_fail')

@socketio.on('admin_command')
def on_admin_command(data):
    if data.get('password') != ADMIN_PASSWORD: return
    cmd = data.get('cmd')
    
    if cmd == 'reset':
        global game_state, current_perm_pool
        game_state["phase"] = "LOBBY"
        game_state["round"] = 0
        game_state["players"] = {}
        game_state["rules"] = []
        game_state["logs"] = []
        game_state["new_rule"] = None
        game_state["round_event"] = None
        game_state["multiplier"] = 0.8
        current_perm_pool = list(PERMANENT_RULE_POOL)
        broadcast_state()
        
    elif cmd == 'add_perm_rule':
        # 强制激活永久规则
        rule_id = data.get('rule_id')
        rule_to_add = next((r for r in current_perm_pool if r["id"] == rule_id), None)
        if rule_to_add:
            current_perm_pool.remove(rule_to_add)
            trigger_perm_rule(rule_to_add)
            game_state["phase"] = "RULE_ANNOUNCEMENT"
            game_state["timer"] = TIME_LIMIT_RULE
            broadcast_state()

    elif cmd == 'refresh_pool':
         emit('admin_pool_update', {'perm_pool': current_perm_pool, 'temp_pool': ROUND_EVENT_POOL})

if __name__ == '__main__':
    socketio.run(app, debug=True, host='0.0.0.0', port=5002)
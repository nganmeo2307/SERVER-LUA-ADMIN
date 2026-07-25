import telebot
import json
import uuid
import datetime
import os
import logging
import threading
import time
import requests
from flask import Flask, request, jsonify
import firebase_admin
from firebase_admin import credentials, firestore

# ================= CẤU HÌNH SERVER =================
BOT_TOKEN = os.getenv("BOT_TOKEN") # Bot Quản lý Key (Gõ lệnh /vip, /free...)
FEEDBACK_BOT_TOKEN = os.getenv("FEEDBACK_BOT_TOKEN") # Bot chuyên gửi ảnh Top 1
ADMIN_ID = os.getenv("ADMIN_ID")
SERVER_URL = os.getenv("SERVER_URL")
FIREBASE_CONFIG = os.getenv("FIREBASE_JSON")
FEEDBACK_CHAT_ID = os.getenv("FEEDBACK_CHAT_ID")
IMGBB_KEY = os.getenv("IMGBB_KEY")

# Bộ nhớ đệm lưu thời gian gửi ảnh cuối cùng của từng HWID
LAST_FEEDBACK_TIME = {}
# Khoảng thời gian cấm gửi liên tiếp (Tính bằng giây, 600 giây = 10 phút)
COOLDOWN_SECONDS = 600 



# Xử lý ADMIN_ID để tránh lỗi format
try:
    REAL_ADMIN_ID = str(ADMIN_ID).strip().replace("Value:", "").replace(" ", "")
except:
    REAL_ADMIN_ID = ""

# Thiết lập Log
logging.basicConfig(level=logging.INFO)
telebot.logger.setLevel(logging.INFO)

# Khởi tạo Bot và Flask
bot = telebot.TeleBot(BOT_TOKEN, threaded=False)
server = Flask(__name__)
import json
server.json.ensure_ascii = False

# ================= KẾT NỐI FIREBASE =================
db = None
try:
    if not FIREBASE_CONFIG:
        print("❌ LỖI: Chưa cấu hình biến môi trường 'FIREBASE_JSON'")
    else:
        cred_dict = json.loads(FIREBASE_CONFIG)
        cred = credentials.Certificate(cred_dict)
        try:
            firebase_admin.get_app()
        except ValueError:
            firebase_admin.initialize_app(cred)
        db = firestore.client()
        print("✅ KẾT NỐI FIREBASE THÀNH CÔNG!")
except Exception as e:
    print(f"⚠️ Lỗi kết nối Firebase: {e}")

# ================= HÀM HỖ TRỢ =================
def calculate_expiry(duration_str):
    """Tính toán thời gian hết hạn từ chuỗi (vd: 1d, 2h)"""
    now = datetime.datetime.now()
    try:
        val = int(''.join(filter(str.isdigit, duration_str)))
        if 'd' in duration_str: return now + datetime.timedelta(days=val), f"{val} Ngày"
        if 'h' in duration_str: return now + datetime.timedelta(hours=val), f"{val} Giờ"
        if 'm' in duration_str: return now + datetime.timedelta(minutes=val), f"{val} Phút"
    except: pass
    return None, None

def check_admin(user_id):
    """Kiểm tra quyền Admin"""
    return str(user_id) == REAL_ADMIN_ID

def send_admin_notify(message):
    """Gửi thông báo về Telegram Admin"""
    try:
        if REAL_ADMIN_ID:
            bot.send_message(REAL_ADMIN_ID, message, parse_mode="Markdown")
    except Exception as e:
        print(f"⚠️ Lỗi gửi tin nhắn: {e}")

def get_key_document(client_key, game_id):
    """
    Tìm key trong DB.
    1. Tìm chính xác (Key hệ thống tự sinh).
    2. Tìm theo Prefix Game (Custom key trùng tên).
    Trả về: (document_snapshot, real_key_id_in_db)
    """
    try:
        # Cách 1: Tìm chính xác
        doc_ref = db.collection('keys').document(client_key)
        doc = doc_ref.get()
        if doc.exists:
            return doc, client_key

        # Cách 2: Tìm theo Prefix Game (Nếu client có gửi game_id)
        if game_id:
            prefixed_key = f"{game_id.upper()}-{client_key}"
            doc_ref_p = db.collection('keys').document(prefixed_key)
            doc_p = doc_ref_p.get()
            if doc_p.exists:
                return doc_p, prefixed_key
    except Exception:
        pass
    return None, None

# ================= AUTO CLEAN EXPIRED KEYS =================
def auto_clean_expired_keys():
    """Background thread: Scan and delete expired keys"""
    print("⏳ Waiting for server stabilization (30s)...")
    time.sleep(30)
    
    print("🧹 Starting expired key cleaner...")
    while True:
        try:
            if db:
                now = datetime.datetime.now()
                # Định dạng thời gian hiện tại thành chuỗi để query trực tiếp
                now_str = now.strftime("%Y-%m-%d %H:%M:%S")
                
                # CHỈ lấy những key có thời gian hết hạn nhỏ hơn thời gian hiện tại
                expired_docs = db.collection('keys').where('expiry', '<', now_str).stream()
                
                for doc in expired_docs:
                    key_id = doc.id
                    print(f"🗑️ Deleting expired key: {key_id}")
                    db.collection('keys').document(key_id).delete()
                    msg = f"🗑️ *Deleted expired key*\n`{key_id}`"
                    send_admin_notify(msg)
        except Exception as e:
            print(f"⚠️ Auto Clean thread error: {e}")
        
        # Tăng thời gian chờ lên 2 tiếng (7200 giây) thay vì 60 giây
        time.sleep(7200)

# ================= API CHECK KEY =================
@server.route('/check_key', methods=['POST'])
def api_check_key():
    try:
        if not db: return jsonify({"status": False, "msg": "Lỗi Server Database"})
        
        # --- THAY ĐỔI Ở ĐÂY: Lấy dữ liệu từ Form thay vì JSON ---
        client_key = request.form.get('key', '').strip()
        client_hwid = request.form.get('hwid', '').strip()
        client_game_id = request.form.get('game_id', '').strip().lower()

        # Nếu request gửi lên không có key, có thể nó là JSON fallback (để an toàn, ta bắt cả 2)
        if not client_key:
            data = request.get_json(silent=True) or {}
            client_key = data.get('key', '').strip()
            client_hwid = data.get('hwid', '').strip()
            client_game_id = data.get('game_id', '').strip().lower()

        if not client_key:
            return jsonify({"status": False, "msg": "Không nhận được dữ liệu Key từ Game!"})
        
        # --- 1. CHECK BLOCK HWID ---
        try:
            blacklist_doc = db.collection('settings').document('blacklist').get()
            if blacklist_doc.exists:
                blocked_models = blacklist_doc.to_dict().get('models', [])
                # So sánh trực tiếp với HWID client gửi lên
                for blocked_kw in blocked_models:
                    if str(blocked_kw).strip() == client_hwid:
                        return jsonify({"status": False, "msg": f"Thiết bị của bạn đã bị Admin chặn!"})
        except Exception: pass

        # --- 2. TÌM KEY ---
        doc, real_key_id = get_key_document(client_key, client_game_id)

        if not doc:
            return jsonify({"status": False, "msg": "Key không tồn tại!"})

        key_data = doc.to_dict()
        doc_ref = db.collection('keys').document(real_key_id) 

        # --- 3. CHECK GAME ID (CHỐNG DÙNG CHÉO TOOL) ---
        key_game_id = key_data.get('game_id', 'all').lower()
        if key_game_id != 'all' and key_game_id != client_game_id:
             return jsonify({
                 "status": False, 
                 "msg": f"Key này dành cho Tool: {key_game_id.upper()}\nBạn đang dùng Tool: {client_game_id.upper()}"
             })

        # --- 4. CHECK KHÓA & HẠN ---
        if key_data.get('is_locked', False) is True:
             return jsonify({"status": False, "msg": "Key này đã bị khóa vĩnh viễn!"})

        try:
            expiry_dt = datetime.datetime.strptime(key_data['expiry'], "%Y-%m-%d %H:%M:%S")
            now = datetime.datetime.now()
            if now > expiry_dt:
                return jsonify({"status": False, "msg": "Key đã hết hạn!"})

            remaining = expiry_dt - now
            remain_str = str(remaining).split('.')[0].replace("days", "ngày").replace("day", "ngày")
        except:
            return jsonify({"status": False, "msg": "Lỗi định dạng ngày tháng."})

        # --- 5. XỬ LÝ LOGIC VIP ---
        if key_data['type'] == 'vip':
            stored_hwid = key_data.get('hwid')

            if stored_hwid is None:
                doc_ref.update({
                    "hwid": client_hwid,
                    "info": f"Active {client_hwid}"  # Đổi thành lưu log theo HWID
                })
                notify_msg = (
                    f"👑 *ĐÃ KÍCH HOẠT KEY VIP ({key_game_id.upper()})* 👑\n"
                    f"🔑 *Key:* `{client_key}`\n"
                    f"📱 *HWID:* `{client_hwid}`\n"
                    f"🕒 *Thời gian:* `{now.strftime('%H:%M:%S %d/%m/%y')}`\n"                    
                    f"⏳ *Còn lại:* `{remain_str}`"
                )
                send_admin_notify(notify_msg)
                return jsonify({"status": True, "msg": "VIP Active Success", "type": "VIP"})

            elif stored_hwid == client_hwid:
                return jsonify({"status": True, "msg": "VIP Login Success", "type": "VIP"})
            else:
                return jsonify({"status": False, "msg": "❌ Sai thiết bị! Vui lòng liên hệ Admin reset."})

        # --- 6. XỬ LÝ LOGIC FREE ---
        elif key_data['type'] == 'free':
            hwids = key_data.get('hwids', [])
            max_dev = key_data.get('max_devices', 1)

            if client_hwid in hwids:
                return jsonify({"status": True, "msg": "FREE Login Success", "type": "FREE"})

            elif len(hwids) < max_dev:
                doc_ref.update({
                    "hwids": firestore.ArrayUnion([client_hwid])
                })
                notify_msg = (
                    f"🎁 *ĐÃ KÍCH HOẠT KEY FREE ({key_game_id.upper()})* 🎁\n"
                    f"🔑 *Key:* `{client_key}`\n"
                    f"📱 *HWID:* `{client_hwid}`\n"
                    f"🔢 *Slot:* {len(hwids) + 1}/{max_dev}\n"
                    f"⏳ *Còn lại:* `{remain_str}`"
                )
                send_admin_notify(notify_msg)
                return jsonify({"status": True, "msg": "FREE Active Success", "type": "FREE"})
            else:
                return jsonify({"status": False, "msg": "Key đã đầy slot (Full Devices)"})

        return jsonify({"status": False, "msg": "Lỗi dữ liệu Key."})
    except Exception as e:
        return jsonify({"status": False, "msg": f"System Error: {str(e)}"})


# ================= API STATS =================
@server.route('/stats', methods=['GET'])
def api_stats():
    try:
        if not db: return jsonify({"status": False, "vip": 0, "free": 0})
        vip_c, free_c = 0, 0
        docs = db.collection('keys').stream()
        for doc in docs:
            d = doc.to_dict()
            if d.get('type') == 'vip' and d.get('hwid'): vip_c += 1
            elif d.get('type') == 'free': free_c += len(d.get('hwids', []))
        return jsonify({"status": True, "vip": vip_c, "free": free_c})
    except:
        return jsonify({"status": False, "vip": 0, "free": 0})

# ================= WEBHOOK & ROUTES =================
@server.route('/webhook', methods=['POST'])
def webhook_handler():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        t = threading.Thread(target=bot.process_new_updates, args=([update],))
        t.start()
        return "OK", 200
    return "Access Denied", 403

@server.route("/")
def index():
    return f"✅ Server is Running... Time: {datetime.datetime.now()}", 200

@server.route("/set_webhook")
def set_webhook():
    bot.remove_webhook()
    clean_url = SERVER_URL.strip().rstrip('/')
    webhook_url = f"{clean_url}/webhook"
    bot.set_webhook(url=webhook_url)
    return f"✅ Webhook set to: {webhook_url}", 200

# ================= BOT COMMANDS =================
def show_menu(m):
    bot.reply_to(m, """
🔥 *AKMODPUBG - SERVER KEY* 🔥

1️⃣ *TẠO KEY*
   `/vip `*<tool> <ngày>*
   `/free `*<tool> <sltb> <ngày>*
   `/custom vip `*<tool> <ngày> <key>*
   `/custom free `*<tool> <sltb> <ngày> <key>*

2️⃣ *QUẢN LÝ KEY*
   `/list `- *Xem list key (theo Game)*
   `/delete `- *Xóa key (nhập ID hệ thống)*
   `/reset `- *Reset thiết bị (nhập ID hệ thống)*
   `/lockkey `- *Khóa key*
   `/unlockkey `- *Mở khóa key*

3️⃣ *CHẶN THIẾT BỊ*
   `/blockmodel `- *Chặn thiết bị*
   `/unlockmodel `- *Mở chặn thiết bị*
   `/listblock `- *Xem danh sách chặn*
""", parse_mode="Markdown")

@bot.message_handler(commands=['start', 'help'])
def send_welcome(m):
    if check_admin(m.from_user.id): show_menu(m)

# --- TẠO VIP: /vip <game> <time> ---
@bot.message_handler(commands=['vip'])
def create_vip(m):
    if not check_admin(m.from_user.id): return
    try:
        args = m.text.split()
        if len(args) < 3: return bot.reply_to(m, "⚠️ Sai cú pháp. Ví dụ: `/vip pubg 1d`")
        
        game_id = args[1].lower()
        expiry, label = calculate_expiry(args[2])
        if not expiry: return bot.reply_to(m, "⚠️ Sai định dạng thời gian.")
        
        key = f"{game_id.upper()}-VIP-{str(uuid.uuid4())[:6].upper()}"
        data = {
            "type": "vip", 
            "game_id": game_id,
            "hwid": None, 
            "expiry": expiry.strftime("%Y-%m-%d %H:%M:%S"), 
            "info": "Chưa kích hoạt",
            "is_locked": False,
            "created_at": firestore.SERVER_TIMESTAMP
        }
        db.collection('keys').document(key).set(data)
        bot.reply_to(m, f"👑 *TẠO KEY VIP {game_id.upper()} ({label})*\n\n🔑 *KEY*: `{key}`", parse_mode="Markdown")
    except Exception as e: bot.reply_to(m, f"❌ Error: {e}")

# --- TẠO FREE: /free <game> <slot> <time> ---
@bot.message_handler(commands=['free'])
def create_free(m):
    if not check_admin(m.from_user.id): return
    try:
        args = m.text.split()
        if len(args) < 4: return bot.reply_to(m, "⚠️ Sai cú pháp. Ví dụ: `/free pubg 10 1d`")
        
        game_id = args[1].lower()
        max_d = int(args[2])
        expiry, label = calculate_expiry(args[3])
        if not expiry: return bot.reply_to(m, "⚠️ Sai định dạng thời gian.")
        
        key = f"{game_id.upper()}-FREE-{str(uuid.uuid4())[:6].upper()}"
        data = {
            "type": "free",
            "game_id": game_id, 
            "max_devices": max_d, "hwids": [], 
            "expiry": expiry.strftime("%Y-%m-%d %H:%M:%S"), 
            "info": f"Free ({max_d} slots)",
            "is_locked": False,
            "created_at": firestore.SERVER_TIMESTAMP
        }
        db.collection('keys').document(key).set(data)
        bot.reply_to(m, f"🎁 *TẠO KEY FREE {game_id.upper()} ({max_d} SLOT - {label})*\n\n🔑 *KEY:* `{key}`", parse_mode="Markdown")
    except Exception as e: bot.reply_to(m, f"❌ Error: {e}")

# --- TẠO CUSTOM (CHO PHÉP TRÙNG KEY NẾU KHÁC GAME) ---
@bot.message_handler(commands=['custom'])
def create_custom(m):
    if not check_admin(m.from_user.id): return
    try:
        args = m.text.split()
        type_k = args[1].lower()
        
        if type_k == 'vip':
            # /custom vip <game> <time> <name>
            game_id = args[2].lower()
            expiry, label = calculate_expiry(args[3])
            user_key_name = args[4].strip()
            
            # Lưu với ID là GAME-KEY để tránh trùng
            db_id = f"{game_id.upper()}-{user_key_name}"
            
            if db.collection('keys').document(db_id).get().exists: 
                return bot.reply_to(m, f"⚠️ Key `{user_key_name}` cho game {game_id} đã tồn tại!")
            
            data = {
                "type": "vip",
                "game_id": game_id,
                "hwid": None, 
                "expiry": expiry.strftime("%Y-%m-%d %H:%M:%S"), 
                "info": "Chưa kích hoạt", 
                "is_locked": False
            }
            db.collection('keys').document(db_id).set(data)
            bot.reply_to(m, f"👑 *CUSTOM VIP {game_id.upper()} ({label})*\n\n🔑 *KEY:* `{user_key_name}`\n(ID Hệ thống: `{db_id}`)", parse_mode="Markdown")
            
        elif type_k == 'free':
            # /custom free <game> <slot> <time> <name>
            game_id = args[2].lower()
            max_d = int(args[3])
            expiry, label = calculate_expiry(args[4])
            user_key_name = args[5].strip()
            
            # Lưu với ID là GAME-KEY
            db_id = f"{game_id.upper()}-{user_key_name}"

            if db.collection('keys').document(db_id).get().exists: 
                return bot.reply_to(m, f"⚠️ Key `{user_key_name}` cho game {game_id} đã tồn tại!")
            
            data = {
                "type": "free",
                "game_id": game_id,
                "max_devices": max_d, 
                "hwids": [], 
                "expiry": expiry.strftime("%Y-%m-%d %H:%M:%S"), 
                "info": f"Free ({max_d} slots)", 
                "is_locked": False
            }
            db.collection('keys').document(db_id).set(data)
            bot.reply_to(m, f"🎁 *CUSTOM FREE {game_id.upper()} ({max_d} SLOT - {label})*\n\n🔑 *KEY:* `{user_key_name}`", parse_mode="Markdown")
    except: 
        bot.reply_to(m, "⚠️ Sai cú pháp custom.\nVIP: `/custom vip pubg 1d KEYNAME`\nFREE: `/custom free pubg 10 1d KEYNAME`")

# --- DANH SÁCH KEY (PHÂN LOẠI THEO GAME) ---
@bot.message_handler(commands=['list'])
def list_keys(m):
    if not check_admin(m.from_user.id): return
    
    docs = db.collection('keys').stream()
    
    # 1. Gom nhóm Key theo Game ID
    grouped_keys = {} # {'PUBG': [str1, str2], 'LIENQUAN': [...]}
    total_count = 0
    
    for doc in docs:
        total_count += 1
        k_id = doc.id
        v = doc.to_dict()
        game_tag = v.get('game_id', 'KHÁC').upper()
        
        is_expired = False
        is_locked = v.get('is_locked', False)
        expiry_str = v.get('expiry', 'N/A')

        try:
            exp = datetime.datetime.strptime(expiry_str, "%Y-%m-%d %H:%M:%S")
            if datetime.datetime.now() > exp: is_expired = True
        except: pass

        if is_locked:
            stt_icon = "🔒 ĐÃ KHÓA"
        elif is_expired:
            stt_icon = "🔴 Hết hạn"
        else:
            if v.get('type') == 'vip':
                if v.get('hwid'):
                    raw_info = v.get('info', '')
                    dev_name = raw_info.replace("Active ", "").strip() if "Active" in raw_info else "Online"
                    stt_icon = f"🟢 {dev_name}"
                else:
                    stt_icon = "⚪ Chưa dùng"
            else:
                used = len(v.get('hwids', []))
                mx = v.get('max_devices', 0)
                stt_icon = f"🎁 {used}/{mx} Slot"

        key_line = f"  ▪ `{k_id}`\n    └ {stt_icon} | ⏳ {expiry_str}"
        
        if game_tag not in grouped_keys:
            grouped_keys[game_tag] = []
        grouped_keys[game_tag].append(key_line)

    if total_count == 0:
        bot.reply_to(m, "📭 Database trống.")
        return

    msg = "📊 *DANH SÁCH KEY THEO GAME*\n"
    sorted_games = sorted(grouped_keys.keys())
    
    for game in sorted_games:
        key_list = grouped_keys[game]
        msg += f"\n➖➖➖➖➖➖➖➖➖➖\n🎮 *{game}* ({len(key_list)} key)\n"
        for line in key_list:
            msg += line + "\n"

    if len(msg) > 4000:
        part_1 = msg[:4000] + "\n\n⚠️ *Danh sách quá dài...*"
        bot.reply_to(m, part_1, parse_mode="Markdown")
    else:
        bot.reply_to(m, msg, parse_mode="Markdown")

@bot.message_handler(commands=['delete'])
def delete_key(m):
    if not check_admin(m.from_user.id): return
    try:
        key = m.text.split()[1]
        db.collection('keys').document(key).delete()
        bot.reply_to(m, f"🗑️ Đã xóa key: `{key}`", parse_mode="Markdown")
    except: pass

@bot.message_handler(commands=['reset'])
def reset_key(m):
    if not check_admin(m.from_user.id): return
    try:
        key = m.text.split()[1]
        ref = db.collection('keys').document(key)
        doc = ref.get()
        if doc.exists:
            dt = doc.to_dict()
            if dt['type'] == 'vip': ref.update({"hwid": None, "info": "Đã Reset"})
            else: ref.update({"hwids": []})
            bot.reply_to(m, f"♻️ Đã Reset: `{key}`", parse_mode="Markdown")
        else:
            bot.reply_to(m, "❌ Key không tồn tại (Nhập đúng ID hệ thống).")
    except: pass

@bot.message_handler(commands=['lockkey'])
def lock_key_cmd(m):
    if not check_admin(m.from_user.id): return
    try:
        args = m.text.split()
        if len(args) < 2: return bot.reply_to(m, "⚠️ Nhập tên key!")
        key = args[1]
        ref = db.collection('keys').document(key)
        if ref.get().exists:
            ref.update({"is_locked": True})
            bot.reply_to(m, f"🔒 Đã KHÓA key: `{key}`\n(User sẽ bị đá sau 30s)", parse_mode="Markdown")
        else:
            bot.reply_to(m, "❌ Key không tồn tại.")
    except Exception as e: bot.reply_to(m, f"Lỗi: {e}")

@bot.message_handler(commands=['unlockkey'])
def unlock_key_cmd(m):
    if not check_admin(m.from_user.id): return
    try:
        args = m.text.split()
        if len(args) < 2: return bot.reply_to(m, "⚠️ Nhập tên key!")
        key = args[1]
        ref = db.collection('keys').document(key)
        if ref.get().exists:
            ref.update({"is_locked": False})
            bot.reply_to(m, f"🔓 Đã MỞ KHÓA key: `{key}`", parse_mode="Markdown")
        else:
            bot.reply_to(m, "❌ Key không tồn tại.")
    except Exception as e: bot.reply_to(m, f"Lỗi: {e}")

@bot.message_handler(commands=['blockmodel'])
def block_model_cmd(m):
    if not check_admin(m.from_user.id): return
    try:
        cmd_text = m.text.replace("/blockmodel", "").strip()
        if not cmd_text: return bot.reply_to(m, "⚠️ Nhập tên Model!")
        ref = db.collection('settings').document('blacklist')
        if not ref.get().exists:
            ref.set({"models": [cmd_text]})
        else:
            ref.update({"models": firestore.ArrayUnion([cmd_text])})
        bot.reply_to(m, f"🚫 Đã thêm Model vào Blacklist:\n`{cmd_text}`", parse_mode="Markdown")
    except Exception as e: bot.reply_to(m, f"Lỗi: {e}")

@bot.message_handler(commands=['unlockmodel'])
def unlock_model_cmd(m):
    if not check_admin(m.from_user.id): return
    try:
        cmd_text = m.text.replace("/unlockmodel", "").strip()
        if not cmd_text: return bot.reply_to(m, "⚠️ Nhập tên Model!")
        ref = db.collection('settings').document('blacklist')
        if ref.get().exists:
            ref.update({"models": firestore.ArrayRemove([cmd_text])})
            bot.reply_to(m, f"✅ Đã gỡ chặn Model:\n`{cmd_text}`", parse_mode="Markdown")
        else:
            bot.reply_to(m, "❌ Chưa có danh sách chặn nào.")
    except Exception as e: bot.reply_to(m, f"Lỗi: {e}")

@bot.message_handler(commands=['listblock'])
def list_block_cmd(m):
    if not check_admin(m.from_user.id): return
    try:
        ref = db.collection('settings').document('blacklist').get()
        if not ref.exists: return bot.reply_to(m, "📋 Danh sách chặn trống.")
        models = ref.to_dict().get('models', [])
        if not models: return bot.reply_to(m, "📋 Danh sách chặn trống.")
        msg = "🚫 **DANH SÁCH BỊ CHẶN** 🚫\n"
        for mod in models: msg += f"- `{mod}`\n"
        bot.reply_to(m, msg, parse_mode="Markdown")
    except Exception as e: bot.reply_to(m, f"Lỗi: {e}")

# ================= STARTUP =================
def start_services():
    if not any(t.name == "AutoCleanThread" for t in threading.enumerate()):
        t = threading.Thread(target=auto_clean_expired_keys, name="AutoCleanThread", daemon=True)
        t.start()

start_services()




# ================= API GỬI ẢNH FEEDBACK TOP 1 =================
@server.route('/send_top1', methods=['POST'])
def send_top1():
    base64_image = request.form.get('base64_image')
    caption = request.form.get('caption')
    
    # 1. Nhận thêm Key VIP và HWID từ Game gửi lên
    vip_key = request.form.get('vip_key', '').strip()
    hwid = request.form.get('hwid', '').strip()

    if not base64_image or not caption or not vip_key or not hwid:
        return jsonify({"status": False, "msg": "Thiếu dữ liệu hoặc nghi ngờ Spam"}), 400

    # ===============================================================
    # LỚP KHIÊN 1.5: CHỐNG CHỈNH SỬA TEXT BẬY BẠ (TAMPER PROTECTION)
    # ===============================================================
    import re
    # Xóa ký tự xuống dòng rác \r nếu có để so sánh cho chuẩn
    clean_caption = caption.replace('\r', '')
    
    # Cái khuôn sắt: Dấu ^ là bắt đầu, dấu $ là kết thúc. 
    # .* đại diện cho biến số thay đổi. Khóa chết toàn bộ cấu trúc!
    safe_pattern = r"^🏆 <b>PAK LUA VIP AKMODPUBG</b> 🏆\n🔥 <b>AUTO FEEDBACK GROUP VIP</b> 🔥\n⏰ <b>Thời gian: .*</b>\n👤 <b>Tên nhân vật: \*\*\*\*\*</b>\n🔑 <b>UID: \*\*\*.*</b>\n🔫 <b>Số Kill: \d+</b>\n🎖 <b>Rank: .*</b>\n💬 <b>MUA MOD VIP IB ADMIN @nanamod96</b>$"
    
    if not re.match(safe_pattern, clean_caption):
        # Trừng phạt: Khóa luôn Key của thằng dám xài HttpCanary sửa Text
        try:
            doc, real_key_id = get_key_document(vip_key, "LUAPAK")
            if doc:
                db.collection('keys').document(real_key_id).update({
                    "is_locked": True,
                    "info": "Auto Ban: Dùng tool sửa bậy Text Feedback"
                })
                # Báo cáo về cho Admin
                notify_msg = (
                    f"🚫 *AUTO BAN SỬA TEXT BẬY BẠ* 🚫\n"
                    f"🔑 *Key:* `{vip_key}`\n"
                    f"📱 *HWID:* `{hwid}`\n"
                    f"⚠️ *Lý do:* Cố tình sửa đoạn Text gửi ảnh.\n"
                )
                use_token = FEEDBACK_BOT_TOKEN if FEEDBACK_BOT_TOKEN else BOT_TOKEN
                requests.post(f"https://api.telegram.org/bot{use_token}/sendMessage", data={
                    "chat_id": REAL_ADMIN_ID,
                    "text": notify_msg,
                    "parse_mode": "Markdown"
                })
        except Exception:
            pass
        return jsonify({"status": False, "msg": "Phát hiện sửa đổi dữ liệu! Key đã bị khóa."}), 403
    # ===============================================================

    # ===============================================================
    # LỚP KHIÊN 1: KIỂM TRA KEY VIP VÀ HWID CÓ HỢP LỆ KHÔNG
    # ===============================================================
    if not db:
        return jsonify({"status": False, "msg": "Lỗi DB"}), 500
        
    doc, real_key_id = get_key_document(vip_key, "LUAPAK")
    if not doc:
        return jsonify({"status": False, "msg": "Lỗi kết nối"}), 403
        
    key_data = doc.to_dict()
    if key_data.get('is_locked', False):
        return jsonify({"status": False, "msg": "Lỗi kết nối"}), 403
        
    # Check Hạn sử dụng
    try:
        expiry_dt = datetime.datetime.strptime(key_data.get('expiry', ''), "%Y-%m-%d %H:%M:%S")
        if datetime.datetime.now() > expiry_dt:
            return jsonify({"status": False, "msg": "Key đã hết hạn!"}), 403
    except:
        pass

    # Check HWID chống leak link
    if key_data.get('type') == 'vip':
        if key_data.get('hwid') and key_data.get('hwid') != hwid:
            return jsonify({"status": False, "msg": "ERROR"}), 403
    else:
        if hwid not in key_data.get('hwids', []):
            return jsonify({"status": False, "msg": "ERROR"}), 403
    # ===============================================================

    # ===============================================================
    # LỚP KHIÊN 2: CHỐNG SPAM & AUTO BAN (TRẢM THỦ)
    # ===============================================================
    current_time = time.time()
    if hwid in LAST_FEEDBACK_TIME:
        time_passed = current_time - LAST_FEEDBACK_TIME[hwid]
        if time_passed < COOLDOWN_SECONDS:
            # Phát hiện Spam -> KHÓA KEY VĨNH VIỄN TRÊN FIREBASE
            try:
                # 1. Cập nhật trạng thái khóa trên Database
                db.collection('keys').document(real_key_id).update({
                    "is_locked": True,
                    "info": "Auto Ban: Spam API Feedback"
                })
                
                # 2. Gửi thông báo mật về thẳng inbox của Admin qua Bot Feedback
                notify_msg = (
                    f"🚫 *AUTO BAN SPAM FEEDBACK* 🚫\n"
                    f"🔑 *Key:* `{vip_key}`\n"
                    f"📱 *HWID:* `{hwid}`\n"
                    f"⚠️ *Lý do:* Cố tình dùng Tool Spam gửi ảnh liên tục."
                )
                
                use_token = FEEDBACK_BOT_TOKEN if FEEDBACK_BOT_TOKEN else BOT_TOKEN
                noti_url = f"https://api.telegram.org/bot{use_token}/sendMessage"
                
                # Gửi thẳng vào tin nhắn riêng của Admin (REAL_ADMIN_ID), không gửi ra Group
                requests.post(noti_url, data={
                    "chat_id": REAL_ADMIN_ID,
                    "text": notify_msg,
                    "parse_mode": "Markdown"
                })
            except Exception as e:
                print(f"Lỗi khi Auto Ban: {e}")
                
            return jsonify({"status": False, "msg": "Phát hiện Spam! Key của bạn đã bị khóa vĩnh viễn."}), 429

    # Cập nhật lại mốc thời gian gửi thành công
    LAST_FEEDBACK_TIME[hwid] = current_time
    # ===============================================================

    # BƯỚC 1: Server Python tự động tải ảnh lên ImgBB
    imgbb_url = "https://api.imgbb.com/1/upload"
    imgbb_payload = {
        "key": IMGBB_KEY,
        "image": base64_image
    }
    
    try:
        r_img = requests.post(imgbb_url, data=imgbb_payload)
        r_json = r_img.json()
        if not r_json.get('success'):
            return jsonify({"status": False, "msg": "Lỗi Upload ImgBB"}), 500
        
        image_url = r_json['data'].get('display_url') or r_json['data'].get('url')
    except Exception as e:
        return jsonify({"status": False, "msg": f"Lỗi kết nối ImgBB: {str(e)}"}), 500

    # BƯỚC 2: Server ném Link vào Telegram
    use_token = FEEDBACK_BOT_TOKEN if FEEDBACK_BOT_TOKEN else BOT_TOKEN
    tg_url = f"https://api.telegram.org/bot{use_token}/sendPhoto"
    
    tg_payload = {
        "chat_id": FEEDBACK_CHAT_ID,  # Đã fix đồng bộ tên biến với đầu file
        "photo": image_url,
        "caption": caption,
        "parse_mode": "HTML"
    }
    
    try:
        r_tg = requests.post(tg_url, data=tg_payload)
        if r_tg.status_code == 200:
            return jsonify({"status": True, "msg": "Đã gửi vào Group VIP!"})
        else:
            return jsonify({"status": False, "msg": f"Lỗi Telegram: {r_tg.text}"}), 500
    except Exception as e:
        return jsonify({"status": False, "msg": f"Lỗi kết nối Telegram: {str(e)}"}), 500


if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    server.run(host="0.0.0.0", port=port)
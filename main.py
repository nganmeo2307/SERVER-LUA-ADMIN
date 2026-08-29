import telebot
import json
import uuid
import datetime
import os
import logging
import threading
import time
import requests
import base64
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

# Đã bỏ IMGBB_KEY vì không cần xài nữa

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
server.json.ensure_ascii = False

# Khóa mõm hacker gửi file rác. Nâng lên 20MB cho ảnh iOS
server.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024 

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
                now_str = now.strftime("%Y-%m-%d %H:%M:%S")
                
                expired_docs = db.collection('keys').where('expiry', '<', now_str).stream()
                
                for doc in expired_docs:
                    key_id = doc.id
                    print(f"🗑️ Deleting expired key: {key_id}")
                    db.collection('keys').document(key_id).delete()
                    msg = f"🗑️ *Deleted expired key*\n`{key_id}`"
                    send_admin_notify(msg)
        except Exception as e:
            print(f"⚠️ Auto Clean thread error: {e}")
        
        time.sleep(7200)

# ================= API CHECK KEY =================
@server.route('/check_vip_key', methods=['POST'])
def api_check_key():
    try:
        if not db: return jsonify({"status": False, "msg": "Lỗi Server Database"})
        
        client_key = request.form.get('key', '').strip()
        client_hwid = request.form.get('hwid', '').strip()
        client_game_id = request.form.get('game_id', '').strip().lower()

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

        # --- 3. CHECK GAME ID ---
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
                    "info": f"Active {client_hwid}"  
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
   `/resetallkey `- *Reset thiết bị TOÀN BỘ key*
   `/lockkey `- *Khóa key*
   `/unlockkey `- *Mở khóa key*
   `/listlockkey `- *Xem danh sách Key bị khóa*

3️⃣ *CHẶN THIẾT BỊ*
   `/blockmodel `- *Chặn thiết bị*
   `/unlockmodel `- *Mở chặn thiết bị*
   `/listblock `- *Xem danh sách chặn*
""", parse_mode="Markdown")


@bot.message_handler(commands=['start', 'help'])
def send_welcome(m):
    if check_admin(m.from_user.id): show_menu(m)

# --- Các lệnh tạo, xóa, reset key giữ nguyên ---
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
        data = { "type": "vip", "game_id": game_id, "hwid": None, "expiry": expiry.strftime("%Y-%m-%d %H:%M:%S"), "info": "Chưa kích hoạt", "is_locked": False, "created_at": firestore.SERVER_TIMESTAMP }
        db.collection('keys').document(key).set(data)
        bot.reply_to(m, f"👑 *TẠO KEY VIP {game_id.upper()} ({label})*\n\n🔑 *KEY*: `{key}`", parse_mode="Markdown")
    except Exception as e: bot.reply_to(m, f"❌ Error: {e}")

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
        data = { "type": "free", "game_id": game_id, "max_devices": max_d, "hwids": [], "expiry": expiry.strftime("%Y-%m-%d %H:%M:%S"), "info": f"Free ({max_d} slots)", "is_locked": False, "created_at": firestore.SERVER_TIMESTAMP }
        db.collection('keys').document(key).set(data)
        bot.reply_to(m, f"🎁 *TẠO KEY FREE {game_id.upper()} ({max_d} SLOT - {label})*\n\n🔑 *KEY:* `{key}`", parse_mode="Markdown")
    except Exception as e: bot.reply_to(m, f"❌ Error: {e}")

@bot.message_handler(commands=['custom'])
def create_custom(m):
    if not check_admin(m.from_user.id): return
    try:
        args = m.text.split()
        type_k = args[1].lower()
        if type_k == 'vip':
            game_id = args[2].lower()
            expiry, label = calculate_expiry(args[3])
            user_key_name = args[4].strip()
            db_id = f"{game_id.upper()}-{user_key_name}"
            if db.collection('keys').document(db_id).get().exists: return bot.reply_to(m, f"⚠️ Key `{user_key_name}` cho game {game_id} đã tồn tại!")
            data = { "type": "vip", "game_id": game_id, "hwid": None, "expiry": expiry.strftime("%Y-%m-%d %H:%M:%S"), "info": "Chưa kích hoạt", "is_locked": False }
            db.collection('keys').document(db_id).set(data)
            bot.reply_to(m, f"👑 *CUSTOM VIP {game_id.upper()} ({label})*\n\n🔑 *KEY:* `{user_key_name}`\n(ID Hệ thống: `{db_id}`)", parse_mode="Markdown")
        elif type_k == 'free':
            game_id = args[2].lower()
            max_d = int(args[3])
            expiry, label = calculate_expiry(args[4])
            user_key_name = args[5].strip()
            db_id = f"{game_id.upper()}-{user_key_name}"
            if db.collection('keys').document(db_id).get().exists: return bot.reply_to(m, f"⚠️ Key `{user_key_name}` cho game {game_id} đã tồn tại!")
            data = { "type": "free", "game_id": game_id, "max_devices": max_d, "hwids": [], "expiry": expiry.strftime("%Y-%m-%d %H:%M:%S"), "info": f"Free ({max_d} slots)", "is_locked": False }
            db.collection('keys').document(db_id).set(data)
            bot.reply_to(m, f"🎁 *CUSTOM FREE {game_id.upper()} ({max_d} SLOT - {label})*\n\n🔑 *KEY:* `{user_key_name}`", parse_mode="Markdown")
    except: bot.reply_to(m, "⚠️ Sai cú pháp custom.\nVIP: `/custom vip pubg 1d KEYNAME`\nFREE: `/custom free pubg 10 1d KEYNAME`")

@bot.message_handler(commands=['list'])
def list_keys(m):
    if not check_admin(m.from_user.id): return
    docs = db.collection('keys').stream()
    grouped_keys = {}
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

        if is_locked: stt_icon = "🔒 ĐÃ KHÓA"
        elif is_expired: stt_icon = "🔴 Hết hạn"
        else:
            if v.get('type') == 'vip':
                if v.get('hwid'):
                    raw_info = v.get('info', '')
                    dev_name = raw_info.replace("Active ", "").strip() if "Active" in raw_info else "Online"
                    stt_icon = f"🟢 {dev_name}"
                else: stt_icon = "⚪ Chưa dùng"
            else:
                used = len(v.get('hwids', []))
                mx = v.get('max_devices', 0)
                stt_icon = f"🎁 {used}/{mx} Slot"

        key_line = f"  ▪ `{k_id}`\n    └ {stt_icon} | ⏳ {expiry_str}"
        if game_tag not in grouped_keys: grouped_keys[game_tag] = []
        grouped_keys[game_tag].append(key_line)

    if total_count == 0: return bot.reply_to(m, "📭 Database trống.")

    msg = "📊 *DANH SÁCH KEY THEO GAME*\n"
    sorted_games = sorted(grouped_keys.keys())
    for game in sorted_games:
        key_list = grouped_keys[game]
        msg += f"\n➖➖➖➖➖➖➖➖➖➖\n🎮 *{game}* ({len(key_list)} key)\n"
        for line in key_list: msg += line + "\n"

    # Fix lỗi quá giới hạn 4000 ký tự cắt gãy markdown
    if len(msg) > 4000:
        safe_part = msg[:4000].replace("`", "").replace("*", "") 
        part_1 = safe_part + "\n\n⚠️ Danh sách quá dài, chỉ hiển thị một phần..."
        bot.reply_to(m, part_1)
    else:
        try:
            bot.reply_to(m, msg, parse_mode="Markdown")
        except Exception:
            bot.reply_to(m, msg.replace("`", "").replace("*", ""))

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
        else: bot.reply_to(m, "❌ Key không tồn tại (Nhập đúng ID hệ thống).")
    except: pass

@bot.message_handler(commands=['resetallkey'])
def reset_all_vip_keys(m):
    if not check_admin(m.from_user.id): return
    msg = bot.reply_to(m, "⏳ *Đang tiến hành reset thiết bị cho toàn bộ KEY VIP. Vui lòng đợi...*", parse_mode="Markdown")
    try:
        docs = db.collection('keys').where('type', '==', 'vip').stream()
        count = 0
        for doc in docs:
            ref = db.collection('keys').document(doc.id)
            ref.update({"hwid": None, "info": "Đã Reset"})
            count += 1
        bot.edit_message_text(f"✅ *Hoàn tất!*\nĐã reset thành công thiết bị cho `{count}` KEY VIP trên hệ thống.", chat_id=m.chat.id, message_id=msg.message_id, parse_mode="Markdown")
    except Exception as e:
        bot.edit_message_text(f"❌ *Có lỗi xảy ra trong quá trình reset:*\n`{e}`", chat_id=m.chat.id, message_id=msg.message_id, parse_mode="Markdown")

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
        else: bot.reply_to(m, "❌ Key không tồn tại.")
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
        else: bot.reply_to(m, "❌ Key không tồn tại.")
    except Exception as e: bot.reply_to(m, f"Lỗi: {e}")
    
@bot.message_handler(commands=['listlockkey'])
def list_locked_keys_cmd(m):
    if not check_admin(m.from_user.id): return
    try:
        docs = db.collection('keys').where('is_locked', '==', True).stream()
        count = 0
        msg = "🔒 *DANH SÁCH KEY ĐANG BỊ KHÓA* 🔒\n\n"
        for doc in docs:
            count += 1
            k_id = doc.id
            v = doc.to_dict()
            game_tag = v.get('game_id', 'KHÁC').upper()
            key_type = v.get('type', 'Unknown').upper()
            info = v.get('info', 'Không có ghi chú')
            hwid = v.get('hwid', 'Chưa Active')
            msg += f"▪ `{k_id}` ({game_tag} - {key_type})\n  ├ 📱 HWID: `{hwid}`\n  └ ⚠️ Lý do: {info}\n\n"
            
        if count == 0: return bot.reply_to(m, "📭 Hiện tại không có Key nào bị khóa.")
        
        final_msg = f"📊 *Tổng cộng:* {count} Key bị khóa.\n" + "="*20 + "\n\n" + msg
        
        # Fix lỗi quá giới hạn
        if len(final_msg) > 4000:
            safe_part = final_msg[:4000].replace("`", "").replace("*", "") 
            part_1 = safe_part + "\n\n⚠️ Danh sách quá dài, chỉ hiển thị một phần..."
            bot.reply_to(m, part_1)
        else:
            try: bot.reply_to(m, final_msg, parse_mode="Markdown")
            except Exception: bot.reply_to(m, final_msg.replace("`", "").replace("*", ""))
            
    except Exception as e: bot.reply_to(m, f"Lỗi: {e}")

@bot.message_handler(commands=['blockmodel'])
def block_model_cmd(m):
    if not check_admin(m.from_user.id): return
    try:
        cmd_text = m.text.replace("/blockmodel", "").strip()
        if not cmd_text: return bot.reply_to(m, "⚠️ Nhập tên Model!")
        ref = db.collection('settings').document('blacklist')
        if not ref.get().exists: ref.set({"models": [cmd_text]})
        else: ref.update({"models": firestore.ArrayUnion([cmd_text])})
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
        else: bot.reply_to(m, "❌ Chưa có danh sách chặn nào.")
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
@server.route('/send_feedback', methods=['POST'])
def send_top1():
    base64_image = request.form.get('base64_image')
    caption = request.form.get('caption')
    
    vip_key = request.form.get('vip_key', '').strip()
    hwid = request.form.get('hwid', '').strip()

    if not base64_image or not caption or not vip_key or not hwid:
        return jsonify({"status": False, "msg": "Thiếu dữ liệu hoặc nghi ngờ Spam"}), 400

    # ===============================================================
    # LỚP KHIÊN 1.5: CHỐNG CHỈNH SỬA TEXT BẬY BẠ (TAMPER PROTECTION)
    # ===============================================================
    import re
    clean_caption = caption.replace('\r', '')
    
    safe_pattern = r"^🏆 <b>PAK LUA VIP AKMODPUBG</b> 🏆\n🔥 <b>AUTO FEEDBACK GROUP VIP</b> 🔥\n⏰ <b>Thời gian: .*</b>\n👤 <b>Tên nhân vật: \*\*\*\*\*</b>\n🔑 <b>UID: .*</b>\n🔫 <b>Số Kill: \d+</b>\n🎖 <b>Rank: .*</b>\n💬 <b>MUA MOD VIP IB ADMIN @nanamod96</b>$"
    
    if not re.match(safe_pattern, clean_caption):
        try:
            doc, real_key_id = get_key_document(vip_key, "LUAPAK")
            if doc:
                db.collection('keys').document(real_key_id).update({
                    "is_locked": True,
                    "info": "Auto Ban: Dùng tool sửa bậy Text Feedback"
                })
                
                if hwid:
                    blacklist_ref = db.collection('settings').document('blacklist')
                    if not blacklist_ref.get().exists:
                        blacklist_ref.set({"models": [hwid]})
                    else:
                        blacklist_ref.update({"models": firestore.ArrayUnion([hwid])})

                notify_msg = (
                    f"🚫 *AUTO BAN SỬA TEXT BẬY BẠ* 🚫\n"
                    f"🔑 *Key:* `{vip_key}`\n"
                    f"📱 *HWID:* `{hwid}` (Đã Auto thêm vào Blacklist)\n"
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
        return jsonify({"status": False, "msg": "Phát hiện sửa đổi dữ liệu! Thiết bị và Key đã bị khóa vĩnh viễn."}), 403

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
        
    try:
        expiry_dt = datetime.datetime.strptime(key_data.get('expiry', ''), "%Y-%m-%d %H:%M:%S")
        if datetime.datetime.now() > expiry_dt:
            return jsonify({"status": False, "msg": "Key đã hết hạn!"}), 403
    except:
        pass

    if key_data.get('type') == 'vip':
        if key_data.get('hwid') and key_data.get('hwid') != hwid:
            return jsonify({"status": False, "msg": "ERROR"}), 403
    else:
        if hwid not in key_data.get('hwids', []):
            return jsonify({"status": False, "msg": "ERROR"}), 403

    # ===============================================================
    # LỚP KHIÊN 2: CHỐNG SPAM & AUTO BAN (TRẢM THỦ)
    # ===============================================================
    current_time = time.time()
    if hwid in LAST_FEEDBACK_TIME:
        time_passed = current_time - LAST_FEEDBACK_TIME[hwid]
        if time_passed < COOLDOWN_SECONDS:
            try:
                db.collection('keys').document(real_key_id).update({
                    "is_locked": True,
                    "info": "Auto Ban: Spam API Feedback"
                })
                
                notify_msg = (
                    f"🚫 *AUTO BAN SPAM FEEDBACK* 🚫\n"
                    f"🔑 *Key:* `{vip_key}`\n"
                    f"📱 *HWID:* `{hwid}`\n"
                    f"⚠️ *Lý do:* Cố tình dùng Tool Spam gửi ảnh liên tục."
                )
                
                use_token = FEEDBACK_BOT_TOKEN if FEEDBACK_BOT_TOKEN else BOT_TOKEN
                noti_url = f"https://api.telegram.org/bot{use_token}/sendMessage"
                
                requests.post(noti_url, data={
                    "chat_id": REAL_ADMIN_ID,
                    "text": notify_msg,
                    "parse_mode": "Markdown"
                })
            except Exception as e:
                print(f"Lỗi khi Auto Ban: {e}")
                
            return jsonify({"status": False, "msg": "Phát hiện Spam! Key của bạn đã bị khóa vĩnh viễn."}), 429

    LAST_FEEDBACK_TIME[hwid] = current_time

    # ===============================================================
    # BƯỚC 1: GIẢI MÃ ẢNH TRỰC TIẾP TRÊN RAM (BỎ QUA IMGBB)
    # ===============================================================
    try:
        image_bytes = base64.b64decode(base64_image)
    except Exception as e:
        return jsonify({"status": False, "msg": "Lỗi giải mã ảnh từ Game"}), 400

    # ===============================================================
    # BƯỚC 2: BẮN THẲNG ẢNH LÊN TELEGRAM BẰNG MULTIPART FILE UPLOAD
    # ===============================================================
    use_token = FEEDBACK_BOT_TOKEN if FEEDBACK_BOT_TOKEN else BOT_TOKEN
    tg_url = f"https://api.telegram.org/bot{use_token}/sendPhoto"
    
    tg_data = {
        "chat_id": FEEDBACK_CHAT_ID,
        "caption": caption,
        "parse_mode": "HTML"
    }
    
    tg_files = {
        "photo": ("top1_akmod.jpg", image_bytes, "image/jpeg")
    }
    
    try:
        r_tg = requests.post(tg_url, data=tg_data, files=tg_files)
        r_json = r_tg.json()
        
        if r_tg.status_code == 200 and r_json.get('ok'):
            return jsonify({"status": True, "msg": "Đã gửi vào Group VIP!"})
        else:
            err_desc = r_json.get('description', 'Unknown Error')
            return jsonify({"status": False, "msg": f"Lỗi Telegram: {err_desc}"}), 500
    except Exception as e:
        return jsonify({"status": False, "msg": f"Lỗi kết nối Telegram: {str(e)}"}), 500


# ===============================================================
# HỆ THỐNG RADAR TOÀN CẦU (ĐỒNG BỘ NGƯỜI CHƠI TRONG TRẬN)
# ===============================================================
MATCH_SESSIONS = {}

@server.route('/match_radar', methods=['POST'])
def match_radar():
    match_id = request.form.get('match_id', '').strip()
    uid = request.form.get('uid', '').strip()
    status = request.form.get('status', 'alive').strip() # alive / dead

    if not match_id or not uid or match_id == "LOBBY":
        return jsonify({"status": False})

    now = time.time()

    if match_id not in MATCH_SESSIONS:
        MATCH_SESSIONS[match_id] = {}

    if uid not in MATCH_SESSIONS[match_id]:
        MATCH_SESSIONS[match_id][uid] = {"status": status, "last_ping": now, "notified_death": False}
    else:
        MATCH_SESSIONS[match_id][uid]["last_ping"] = now
        if status == "dead" and MATCH_SESSIONS[match_id][uid]["status"] == "alive":
            MATCH_SESSIONS[match_id][uid]["status"] = "dead"
            MATCH_SESSIONS[match_id][uid]["notified_death"] = False

    alive_count = 0
    new_deaths = 0
    uids_to_remove = []
    mod_uids_list = []

    for p_uid, p_data in MATCH_SESSIONS[match_id].items():
        if now - p_data["last_ping"] > 120:
            uids_to_remove.append(p_uid)
        else:
            mod_uids_list.append(str(p_uid))
            if p_data["status"] == "alive":
                alive_count += 1
            elif p_data["status"] == "dead" and not p_data["notified_death"]:
                new_deaths += 1
                p_data["notified_death"] = True

    for p_uid in uids_to_remove:
        del MATCH_SESSIONS[match_id][p_uid]
        
    mod_uids_string = ",".join(mod_uids_list)

    return jsonify({
        "status": True,
        "alive_count": alive_count,
        "new_deaths": new_deaths,
        "mod_uids": mod_uids_string 
    })

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    server.run(host="0.0.0.0", port=port)

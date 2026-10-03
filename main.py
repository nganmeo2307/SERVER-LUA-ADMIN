import telebot
from telebot.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault
import json
import uuid
import datetime
import os
import logging
import threading
import time
import requests
import base64
import io
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

# Bộ nhớ đệm lưu thời gian gửi ảnh cuối cùng của từng HWID
LAST_FEEDBACK_TIME = {}
# Khoảng thời gian cấm gửi liên tiếp (Tính bằng giây, 600 giây = 10 phút)
COOLDOWN_SECONDS = 600 

# Xử lý ADMIN_ID (Đây là Root Admin)
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

# ================= KẾT NỐI FIREBASE & TẢI ADMIN =================
db = None
EXTRA_ADMINS = []

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
        
        # Tải danh sách Admin phụ từ Database
        try:
            admin_doc = db.collection('settings').document('admins').get()
            if admin_doc.exists:
                EXTRA_ADMINS = admin_doc.to_dict().get('admin_ids', [])
        except Exception as e:
            print(f"⚠ Lỗi tải danh sách Admin phụ: {e}")

except Exception as e:
    print(f"⚠️ Lỗi kết nối Firebase: {e}")

# ================= HÀM HỖ TRỢ =================
def get_vn_now():
    """Lấy thời gian hiện tại chuẩn theo múi giờ Việt Nam (UTC+7)"""
    return datetime.datetime.utcnow() + datetime.timedelta(hours=7)

def calculate_expiry(duration_str):
    """Tính toán thời gian hết hạn từ chuỗi (vd: 1d, 2h)"""
    now = get_vn_now()
    try:
        val = int(''.join(filter(str.isdigit, duration_str)))
        if 'd' in duration_str: return now + datetime.timedelta(days=val), f"{val} Ngày"
        if 'h' in duration_str: return now + datetime.timedelta(hours=val), f"{val} Giờ"
        if 'm' in duration_str: return now + datetime.timedelta(minutes=val), f"{val} Phút"
    except: pass
    return None, None

def check_root_admin(user_id):
    """Kiểm tra xem có phải là Root Admin (Chủ sở hữu) không"""
    return str(user_id) == REAL_ADMIN_ID

def check_admin(user_id):
    """Kiểm tra xem có phải là Admin (Root hoặc Sub-Admin) không"""
    user_str = str(user_id)
    if user_str == REAL_ADMIN_ID:
        return True
    return user_str in EXTRA_ADMINS

def get_creator_name(m):
    """Lấy tên người tạo và lọc bỏ ký tự dễ lỗi Markdown"""
    name = m.from_user.first_name
    if not name: return "Admin"
    return name.replace("*", "").replace("_", "").replace("`", "")

def send_admin_notify(message):
    """Gửi thông báo về Telegram Root Admin"""
    try:
        if REAL_ADMIN_ID:
            bot.send_message(REAL_ADMIN_ID, message, parse_mode="Markdown")
    except Exception as e:
        print(f"⚠️ Lỗi gửi tin nhắn: {e}")

def get_key_document(client_key, game_id):
    """Tìm key trong DB (Chính xác hoặc theo Game ID)"""
    try:
        doc_ref = db.collection('keys').document(client_key)
        doc = doc_ref.get()
        if doc.exists:
            return doc, client_key

        if game_id:
            prefixed_key = f"{game_id.upper()}-{client_key}"
            doc_ref_p = db.collection('keys').document(prefixed_key)
            doc_p = doc_ref_p.get()
            if doc_p.exists:
                return doc_p, prefixed_key
    except Exception:
        pass
    return None, None

# ================= HỆ THỐNG MENU ĐỘNG TỪNG USER =================
def setup_user_menu(user_id):
    """Tạo menu Telegram dựa theo quyền của User"""
    try:
        user_id_str = str(user_id)
        
        # 1. MENU CHO USER BÌNH THƯỜNG
        cmd_user = [
            BotCommand("start", "Khởi động lại Bot"),
            BotCommand("help", "Xem hướng dẫn tự reset Key"),
            BotCommand("reset", "Tự động reset Key VIP (1 lần/ngày)")
        ]
        
        # 2. MENU CHO ADMIN PHỤ
        cmd_sub_admin = [
            BotCommand("start", "Xem Menu hệ thống"),
            BotCommand("help", "Xem cẩm nang Admin phụ"),
            BotCommand("vip", "Tạo 1 Key VIP"),
            BotCommand("list", "Xem danh sách Key bạn tạo"),
            BotCommand("reset", "Reset thiết bị cho Key của bạn")
        ]
        
        # 3. MENU CHO ROOT ADMIN (FULL)
        cmd_root = [
            BotCommand("start", "Xem Menu hệ thống"),
            BotCommand("help", "Cẩm nang toàn tập"),
            BotCommand("vip", "Tạo 1 Key VIP"),
            BotCommand("vipkey", "Tạo SLL Key VIP xuất file .txt"),
            BotCommand("free", "Tạo 1 Key FREE nhiều thiết bị"),
            BotCommand("custom", "Tạo Key theo tên tự chọn"),
            BotCommand("list", "Xem danh sách toàn bộ Key"),
            BotCommand("delete", "Xóa Key khỏi hệ thống"),
            BotCommand("reset", "Reset thiết bị cho 1 Key"),
            BotCommand("resetallkey", "Reset thiết bị TOÀN BỘ Key VIP"),
            BotCommand("lockkey", "Khóa không cho Key hoạt động"),
            BotCommand("unlockkey", "Mở khóa cho Key"),
            BotCommand("listlockkey", "Xem danh sách Key bị khóa"),
            BotCommand("blockmodel", "Đưa thiết bị vào Blacklist"),
            BotCommand("unlockmodel", "Xóa thiết bị khỏi Blacklist"),
            BotCommand("listblock", "Xem danh sách máy bị chặn"),
            BotCommand("addadmin", "Thêm Admin phụ (Chỉ ROOT)"),
            BotCommand("deladmin", "Xóa Admin phụ (Chỉ ROOT)"),
            BotCommand("listadmin", "Xem danh sách Admin phụ")
        ]

        if check_root_admin(user_id_str):
            bot.set_my_commands(cmd_root, scope=BotCommandScopeChat(user_id_str))
        elif check_admin(user_id_str):
            bot.set_my_commands(cmd_sub_admin, scope=BotCommandScopeChat(user_id_str))
        else:
            bot.set_my_commands(cmd_user, scope=BotCommandScopeChat(user_id_str))
    except Exception as e:
        print(f"Lỗi set menu cho {user_id}: {e}")

# ================= AUTO CLEAN EXPIRED KEYS =================
def auto_clean_expired_keys():
    """Background thread: Scan and delete expired keys"""
    print("⏳ Waiting for server stabilization (30s)...")
    time.sleep(30)
    
    print("🧹 Starting expired key cleaner...")
    while True:
        try:
            if db:
                now = get_vn_now()
                now_str = now.strftime("%Y-%m-%d %H:%M:%S")
                
                expired_docs = db.collection('keys').where('expiry', '<', now_str).stream()
                
                for doc in expired_docs:
                    key_id = doc.id
                    print(f"🗑️ Deleting expired key: {key_id}")
                    db.collection('keys').document(key_id).delete()
                    msg = f"🗑 *Deleted expired key*\n`{key_id}`"
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
            if db.collection('blocked_models').document(client_hwid).get().exists:
                return jsonify({"status": False, "msg": f"Thiết bị của bạn đã bị Admin chặn!"})
            
            blacklist_doc = db.collection('settings').document('blacklist').get()
            if blacklist_doc.exists:
                blocked_models = blacklist_doc.to_dict().get('models', [])
                if client_hwid in blocked_models:
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
            now = get_vn_now()
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
    return f"✅ Server is Running... Time: {get_vn_now()}", 200

@server.route("/set_webhook")
def set_webhook():
    bot.remove_webhook()
    clean_url = SERVER_URL.strip().rstrip('/')
    webhook_url = f"{clean_url}/webhook"
    bot.set_webhook(url=webhook_url)
    return f"✅ Webhook set to: {webhook_url}", 200

# ================= BOT COMMANDS =================
def show_menu(m):
    menu_msg = (
        "🔥 *AKMODPUBG - SERVER QUẢN LÝ KEY* 🔥\n\n"
        "✅ *Xác thực Admin thành công!*\n"
        "Hệ thống máy chủ đang hoạt động ổn định.\n\n"
        "👉 *Hướng dẫn:* Hãy nhấn vào nút **Menu** ở góc dưới bên trái thanh chat (hoặc gõ `/help`) để xem và sử dụng nhanh các tính năng quản lý."
    )
    bot.reply_to(m, menu_msg, parse_mode="Markdown")

@bot.message_handler(commands=['start'])
def send_welcome(m):
    # KÍCH HOẠT VÀ LÀM MỚI MENU ĐỘNG KHI USER GÕ /START
    setup_user_menu(m.from_user.id)
    
    if check_admin(m.from_user.id): 
        show_menu(m)
    else:
        welcome_msg = (
            "👋 *CHÀO MỪNG BẠN ĐẾN VỚI HỆ THỐNG QUẢN LÝ KEY*\n\n"
            "🤖 *Hỗ trợ tự động Reset thiết bị KEY VIP (1 ngày/lần)*\n"
            "Để tự reset key, vui lòng nhấn vào menu bên góc trái hoặc gõ lệnh:\n"
            "`/reset <tên_key_vip_của_bạn>`\n\n"
            "📌 *Ví dụ:* `/reset LUATOOL-VIP-0AF3F8`\n\n"
            "💡 Gõ lệnh `/help` để xem hướng dẫn chi tiết hơn."
        )
        bot.reply_to(m, welcome_msg, parse_mode="Markdown")

@bot.message_handler(commands=['help'])
def send_detailed_help(m):
    if check_root_admin(m.from_user.id):
        help_text = """
📖 *HƯỚNG DẪN SỬ DỤNG BOT* 📖

*1️⃣ LỆNH TẠO KEY*
▪️ `/vip <LUAPAK> <thời_gian>`
Tạo 1 key VIP ngẫu nhiên cho 1 máy. 
_VD: `/vip LUAPAK 30d` (30 ngày), `/vip LUAPAK 12h` (12 giờ)_

▪ `/free <tool> <số_máy> <thời_gian>`
Tạo 1 key Free dùng chung cho nhiều máy. 
_VD: `/free LUAFREE 10 7d` (10 máy, 7 ngày)_

▪️ `/vipkey <tool> <số_lượng> <thời_gian>`
Tạo nhiều key VIP cùng lúc và xuất ra file .txt. 
_VD: `/vipkey LUAPAK 50 30d` (Tạo 50 key, mỗi key 30 ngày)_

▪️ `/custom vip <tool> <thời_gian> <tên_key_muốn_tạo>`
Tạo key VIP với TÊN tự chọn. 
_VD: `/custom vip LUAPAK 30d AKMOD-PRO`_

▪️ `/custom free <tool> <số_máy> <thời_gian> <tên_key_muốn_tạo>`
Tạo key Free với TÊN tự chọn. 
_VD: `/custom free LUAPAK 100 30d AKMOD-FREE`_

*2️⃣ LỆNH QUẢN LÝ KEY*
▪️ `/list` : Xem toàn bộ danh sách Key trên hệ thống.
▪️ `/delete <Tên_Key>` : Xóa vĩnh viễn key khỏi hệ thống.
▪️ `/reset <Tên_Key>` : Reset key thiết bị, cho phép key đăng nhập vào máy mới.
▪️ `/resetallkey` : Reset thiết bị cho TOÀN BỘ key VIP.
▪️ `/lockkey <Tên_Key>` : Khóa ngay lập tức 1 key.
▪️ `/unlockkey <Tên_Key>` : Mở khóa lại key đã bị khóa.
▪️ `/listlockkey` : Liệt kê tất cả các key đang bị khóa.

*3️⃣ LỆNH BLACKLIST (CHẶN MÁY)*
▪️ `/blockmodel <HWID>` : Đưa 1 thiết bị vào danh sách chặn.
▪️ `/unlockmodel <HWID>` : Gỡ thiết bị ra khỏi danh sách chặn.
▪️ `/listblock` : Xem danh sách các HWID đang bị chặn.

*4️⃣ QUẢN LÝ ADMIN (CHỈ DÀNH CHO ROOT)*
▪ `/addadmin <ID_Telegram>` : Cấp quyền Admin cho người khác.
▪️ `/deladmin <ID_Telegram>` : Thu hồi quyền Admin phụ.
▪️ `/listadmin` : Xem danh sách Admin phụ đang hoạt động.
"""
        bot.reply_to(m, help_text, parse_mode="Markdown")
        
    elif check_admin(m.from_user.id):
         help_text = """
📖 *HƯỚNG DẪN SỬ DỤNG BOT* 📖

*1️⃣ LỆNH TẠO KEY*
▪ `/vip <LUAPAK> <thời_gian>`
Tạo 1 key VIP ngẫu nhiên cho 1 máy. 
_VD: `/vip LUAPAK 30d` (30 ngày), `/vip LUAPAK 12h` (12 giờ)_

*2️⃣ LỆNH QUẢN LÝ KEY (Chỉ quản lý Key của bạn tạo)*
▪️ `/list` : Xem toàn bộ danh sách Key do bạn tạo.
▪️ `/reset <Tên_Key>` : Reset key thiết bị, cho phép key đăng nhập vào máy mới.
"""
         bot.reply_to(m, help_text, parse_mode="Markdown")
         
    else:
        user_help_msg = (
            "📖 *HƯỚNG DẪN DÀNH CHO KHÁCH HÀNG*\n\n"
            "🤖 *Tính năng: Tự động Reset thiết bị KEY VIP*\n"
            "Khi bạn đổi điện thoại, cài lại ROM, hoặc xóa dữ liệu game, ID máy (HWID) sẽ bị thay đổi khiến Key không nhận diện được thiết bị cũ. Lúc này bạn cần dùng lệnh Reset.\n\n"
            "👉 *Cú pháp thực hiện:*\n"
            "`/reset <tên_key_vip_của_bạn>`\n\n"
            "📌 *Ví dụ:*\n`/reset LUATOOL-VIP-0AF3F8`\n\n"
            "⚠️ *Quy định của hệ thống:*\n"
            "- Tính năng này *chỉ áp dụng cho Key VIP*.\n"
            "- Mỗi Key VIP chỉ được phép tự reset *1 lần duy nhất trong vòng 24 giờ* (Tính từ lần reset gần nhất).\n"
            "- Nếu gặp lỗi hoặc bị khóa, vui lòng liên hệ trực tiếp với Admin để được hỗ trợ."
        )
        bot.reply_to(m, user_help_msg, parse_mode="Markdown")

# --- LỆNH QUẢN LÝ ADMIN PHỤ (CHỈ ROOT MỚI XÀI ĐƯỢC) ---
@bot.message_handler(commands=['addadmin'])
def add_admin(m):
    if not check_root_admin(m.from_user.id):
        return bot.reply_to(m, "❌ *TỪ CHỐI:* Chỉ có ROOT ADMIN (Chủ sở hữu) mới có quyền thêm Admin khác!", parse_mode="Markdown")
    try:
        args = m.text.split()
        if len(args) < 2:
            return bot.reply_to(m, "⚠ *Sai cú pháp!* Ví dụ: `/addadmin 123456789`", parse_mode="Markdown")
        new_admin = args[1].strip()
        
        if new_admin in EXTRA_ADMINS or new_admin == REAL_ADMIN_ID:
            return bot.reply_to(m, "⚠️ Admin ID này đã tồn tại trong hệ thống!")
            
        EXTRA_ADMINS.append(new_admin)
        db.collection('settings').document('admins').set({"admin_ids": EXTRA_ADMINS}, merge=True)
        bot.reply_to(m, f"✅ Đã thêm Admin ID: `{new_admin}` thành công!", parse_mode="Markdown")
        
        # Cập nhật ngay Menu cho Admin phụ vừa được thêm
        setup_user_menu(new_admin)
        
    except Exception as e:
        bot.reply_to(m, f"❌ Lỗi: {e}")

@bot.message_handler(commands=['deladmin'])
def del_admin(m):
    if not check_root_admin(m.from_user.id):
        return bot.reply_to(m, "❌ *TỪ CHỐI:* Chỉ có ROOT ADMIN mới có quyền xóa Admin!", parse_mode="Markdown")
    try:
        args = m.text.split()
        if len(args) < 2:
            return bot.reply_to(m, "⚠️ *Sai cú pháp!* Ví dụ: `/deladmin 123456789`", parse_mode="Markdown")
        del_id = args[1].strip()
        
        if del_id in EXTRA_ADMINS:
            EXTRA_ADMINS.remove(del_id)
            db.collection('settings').document('admins').set({"admin_ids": EXTRA_ADMINS}, merge=True)
            bot.reply_to(m, f"🗑️ Đã xóa Admin ID: `{del_id}` khỏi hệ thống!", parse_mode="Markdown")
            
            # Khôi phục Menu về trạng thái User bình thường cho người bị xóa
            setup_user_menu(del_id)
            
        else:
            bot.reply_to(m, "⚠ ID này không có trong danh sách Admin phụ.")
    except Exception as e:
        bot.reply_to(m, f"❌ Lỗi: {e}")

@bot.message_handler(commands=['listadmin'])
def list_admin(m):
    if not check_root_admin(m.from_user.id):
        return
    if not EXTRA_ADMINS:
        return bot.reply_to(m, "📋 Hiện tại chưa có Admin phụ nào được thêm.")
    
    msg = "👑 *DANH SÁCH ADMIN PHỤ:*\n\n"
    for ad_id in EXTRA_ADMINS:
        msg += f"🔹 `{ad_id}`\n"
    bot.reply_to(m, msg, parse_mode="Markdown")

# --- LỆNH TẠO KEY VIP (ADMIN & SUB-ADMIN) ---
@bot.message_handler(commands=['vip'])
def create_vip(m):
    if not check_admin(m.from_user.id): return
    try:
        args = m.text.split()
        if len(args) < 3: return bot.reply_to(m, "⚠️ Sai cú pháp. Ví dụ: `/vip LUAPAK 1d`")
        game_id = args[1].lower()
        expiry, label = calculate_expiry(args[2])
        if not expiry: return bot.reply_to(m, "⚠️ Sai định dạng thời gian.")
        
        key = f"{game_id.upper()}-VIP-{str(uuid.uuid4())[:6].upper()}"
        creator = get_creator_name(m)
        user_id = str(m.from_user.id)
        
        data = { "type": "vip", "game_id": game_id, "hwid": None, "expiry": expiry.strftime("%Y-%m-%d %H:%M:%S"), "info": "Chưa kích hoạt", "is_locked": False, "created_at": firestore.SERVER_TIMESTAMP, "creator": creator, "creator_id": user_id }
        db.collection('keys').document(key).set(data)
        
        file_data = io.BytesIO(key.encode('utf-8'))
        file_data.name = "AKMOD_VIP_KEY.txt"
        
        caption = (
            f"👑 *TẠO KEY VIP {game_id.upper()} ({label})*\n"
            f"🔑 *Key:* `{key}`\n"
            f"👤 *Người tạo:* `{creator}`\n\n"
            f"📱 *Dán key Android:*\n`/storage/emulated/0/Android/data/com.vng.pubgmobile/files/UE4Game/ShadowTrackerExtra/ShadowTrackerExtra/Saved/Paks/AKMOD_VIP_KEY.txt`\n\n"
            f"🍏 *Dán key IOS:*\n`/Documents/ShadowTrackerExtra/Saved/Paks/AKMOD_VIP_KEY.txt`"
        )
        
        bot.send_document(m.chat.id, document=file_data, caption=caption, parse_mode="Markdown")
        
        if not check_root_admin(m.from_user.id):
            send_admin_notify(
                f"⚠️ *ADMIN PHỤ TẠO KEY VIP*\n"
                f"👤 Tên: `{creator}` (ID: `{user_id}`)\n"
                f"🎮 Game: `{game_id.upper()}`\n"
                f"🔑 Key: `{key}`\n"
                f"⏳ Hạn: `{label}`"
            )
            
    except Exception as e: bot.reply_to(m, f"❌ Error: {e}")

# --- LỆNH TẠO KEY SLL (CHỈ ROOT) ---
@bot.message_handler(commands=['vipkey'])
def create_bulk_vip(m):
    if not check_root_admin(m.from_user.id): 
         return bot.reply_to(m, "❌ *TỪ CHỐI:* Chỉ có ROOT ADMIN mới được sử dụng tính năng này!", parse_mode="Markdown")
    try:
        args = m.text.split()
        if len(args) < 4: 
            return bot.reply_to(m, "⚠️ Sai cú pháp. Ví dụ: `/vipkey LUAPAK 50 60d`", parse_mode="Markdown")
        
        game_id = args[1].lower()
        creator = get_creator_name(m)
        user_id = str(m.from_user.id)
        
        try:
            amount = int(args[2])
            if amount <= 0 or amount > 1000:
                return bot.reply_to(m, "⚠️ Số lượng key hợp lệ từ 1 đến 1000 để tránh máy chủ bị quá tải.")
        except ValueError:
            return bot.reply_to(m, "⚠️ Số lượng phải là một số nguyên.")
            
        expiry, label = calculate_expiry(args[3])
        if not expiry: 
            return bot.reply_to(m, "⚠️ Sai định dạng thời gian.")
        
        msg_process = bot.reply_to(m, f"⏳ Đang tiến hành tạo {amount} key VIP cho {game_id.upper()}, vui lòng đợi một chút...")
        
        generated_keys = []
        batch = db.batch()
        batch_count = 0
        
        for _ in range(amount):
            key = f"{game_id.upper()}-VIP-{str(uuid.uuid4())[:8].upper()}"
            doc_ref = db.collection('keys').document(key)
            data = {
                "type": "vip", 
                "game_id": game_id,
                "hwid": None, 
                "expiry": expiry.strftime("%Y-%m-%d %H:%M:%S"), 
                "info": "Chưa kích hoạt",
                "is_locked": False,
                "created_at": firestore.SERVER_TIMESTAMP,
                "creator": creator,
                "creator_id": user_id
            }
            batch.set(doc_ref, data)
            generated_keys.append(key)
            batch_count += 1
            
            if batch_count == 500:
                batch.commit()
                batch = db.batch()
                batch_count = 0
                
        if batch_count > 0:
            batch.commit()
            
        file_content = "\n".join(generated_keys)
        file_data = io.BytesIO(file_content.encode('utf-8'))
        file_data.name = f"List_Key_VIP_{game_id.upper()}_{amount}Keys_{label.replace(' ', '')}.txt"
        
        caption = (
            f"✅ *ĐÃ TẠO THÀNH CÔNG {amount} KEY VIP*\n\n"
            f"🎮 *Tool:* `{game_id.upper()}`\n"
            f"⏳ *Hạn sử dụng:* `{label}`\n"
            f"⚙️ *Thiết bị:* `1 Thiết bị (VIP)`\n"
            f"👤 *Người tạo:* `{creator}`\n\n"
            f"⬇ _Tải file đính kèm bên dưới để lấy danh sách key._"
        )
        bot.send_document(m.chat.id, document=file_data, caption=caption, parse_mode="Markdown")
        
        try: bot.delete_message(m.chat.id, msg_process.message_id)
        except: pass

    except Exception as e: 
        bot.reply_to(m, f"❌ Lỗi hệ thống: {e}")

# --- LỆNH TẠO KEY FREE (CHỈ ROOT) ---
@bot.message_handler(commands=['free'])
def create_free(m):
    if not check_root_admin(m.from_user.id): 
         return bot.reply_to(m, "❌ *TỪ CHỐI:* Chỉ có ROOT ADMIN mới được sử dụng tính năng này!", parse_mode="Markdown")
    try:
        args = m.text.split()
        if len(args) < 4: return bot.reply_to(m, "⚠️ Sai cú pháp. Ví dụ: `/free LUAPAK 10 1d`")
        game_id = args[1].lower()
        max_d = int(args[2])
        expiry, label = calculate_expiry(args[3])
        if not expiry: return bot.reply_to(m, "⚠️ Sai định dạng thời gian.")
        
        key = f"{game_id.upper()}-FREE-{str(uuid.uuid4())[:6].upper()}"
        creator = get_creator_name(m)
        user_id = str(m.from_user.id)
        
        data = { "type": "free", "game_id": game_id, "max_devices": max_d, "hwids": [], "expiry": expiry.strftime("%Y-%m-%d %H:%M:%S"), "info": f"Free ({max_d} slots)", "is_locked": False, "created_at": firestore.SERVER_TIMESTAMP, "creator": creator, "creator_id": user_id }
        db.collection('keys').document(key).set(data)
        
        file_data = io.BytesIO(key.encode('utf-8'))
        file_data.name = "AKMOD_VIP_KEY.txt"
        
        caption = (
            f"🎁 *TẠO KEY FREE {game_id.upper()} ({max_d} SLOT - {label})*\n"
            f"🔑 *Key:* `{key}`\n"
            f"👤 *Người tạo:* `{creator}`\n\n"
            f"📱 *Dán key Android:*\n`/storage/emulated/0/Android/data/com.vng.pubgmobile/files/UE4Game/ShadowTrackerExtra/ShadowTrackerExtra/Saved/Paks/AKMOD_VIP_KEY.txt`\n\n"
            f"🍏 *Dán key IOS:*\n`/Documents/ShadowTrackerExtra/Saved/Paks/AKMOD_VIP_KEY.txt`"
        )
        
        bot.send_document(m.chat.id, document=file_data, caption=caption, parse_mode="Markdown")

    except Exception as e: bot.reply_to(m, f"❌ Error: {e}")

# --- LỆNH TẠO CUSTOM KEY (CHỈ ROOT) ---
@bot.message_handler(commands=['custom'])
def create_custom(m):
    if not check_root_admin(m.from_user.id): 
         return bot.reply_to(m, "❌ *TỪ CHỐI:* Chỉ có ROOT ADMIN mới được sử dụng tính năng này!", parse_mode="Markdown")
    try:
        args = m.text.split()
        type_k = args[1].lower()
        creator = get_creator_name(m)
        user_id = str(m.from_user.id)
        
        instructions = (
            f"\n\n📱 *Dán key Android:*\n`/storage/emulated/0/Android/data/com.vng.pubgmobile/files/UE4Game/ShadowTrackerExtra/ShadowTrackerExtra/Saved/Paks/AKMOD_VIP_KEY.txt`\n\n"
            f"🍏 *Dán key IOS:*\n`/Documents/ShadowTrackerExtra/Saved/Paks/AKMOD_VIP_KEY.txt`"
        )

        if type_k == 'vip':
            game_id = args[2].lower()
            expiry, label = calculate_expiry(args[3])
            user_key_name = args[4].strip()
            db_id = f"{game_id.upper()}-{user_key_name}"
            
            if db.collection('keys').document(db_id).get().exists: 
                return bot.reply_to(m, f"⚠️ Key `{user_key_name}` cho game {game_id} đã tồn tại!")
                
            data = { "type": "vip", "game_id": game_id, "hwid": None, "expiry": expiry.strftime("%Y-%m-%d %H:%M:%S"), "info": "Chưa kích hoạt", "is_locked": False, "creator": creator, "creator_id": user_id }
            db.collection('keys').document(db_id).set(data)
            
            file_data = io.BytesIO(user_key_name.encode('utf-8'))
            file_data.name = "AKMOD_VIP_KEY.txt"
            
            caption = f"👑 *CUSTOM VIP {game_id.upper()} ({label})*\n🔑 *Key:* `{user_key_name}`\n(Hệ thống: `{db_id}`)\n👤 *Người tạo:* `{creator}`" + instructions
            bot.send_document(m.chat.id, document=file_data, caption=caption, parse_mode="Markdown")

        elif type_k == 'free':
            game_id = args[2].lower()
            max_d = int(args[3])
            expiry, label = calculate_expiry(args[4])
            user_key_name = args[5].strip()
            db_id = f"{game_id.upper()}-{user_key_name}"
            
            if db.collection('keys').document(db_id).get().exists: 
                return bot.reply_to(m, f"⚠️ Key `{user_key_name}` cho game {game_id} đã tồn tại!")
                
            data = { "type": "free", "game_id": game_id, "max_devices": max_d, "hwids": [], "expiry": expiry.strftime("%Y-%m-%d %H:%M:%S"), "info": f"Free ({max_d} slots)", "is_locked": False, "creator": creator, "creator_id": user_id }
            db.collection('keys').document(db_id).set(data)
            
            file_data = io.BytesIO(user_key_name.encode('utf-8'))
            file_data.name = "AKMOD_VIP_KEY.txt"
            
            caption = f"🎁 *CUSTOM FREE {game_id.upper()} ({max_d} SLOT - {label})*\n🔑 *Key:* `{user_key_name}`\n(Hệ thống: `{db_id}`)\n👤 *Người tạo:* `{creator}`" + instructions
            bot.send_document(m.chat.id, document=file_data, caption=caption, parse_mode="Markdown")
            
    except: bot.reply_to(m, "⚠️ Sai cú pháp custom.\nVIP: `/custom vip LUAPAK 1d KEYNAME`\nFREE: `/custom free LUAPAK 10 1d KEYNAME`")

# --- LỆNH XEM DANH SÁCH (ADMIN & SUB-ADMIN) ---
@bot.message_handler(commands=['list'])
def list_keys(m):
    if not check_admin(m.from_user.id): return
    docs = db.collection('keys').stream()
    grouped_keys = {}
    total_count = 0
    
    user_id = str(m.from_user.id)
    is_root = check_root_admin(user_id)
    
    for doc in docs:
        v = doc.to_dict()
        
        # Nếu là Admin phụ, chỉ xem key do chính mình tạo
        if not is_root and v.get('creator_id') != user_id:
            continue
            
        total_count += 1
        k_id = doc.id
        game_tag = v.get('game_id', 'KHÁC').upper()
        creator = v.get('creator', 'Admin')
        is_expired = False
        is_locked = v.get('is_locked', False)
        expiry_str = v.get('expiry', 'N/A')
        
        try:
            exp = datetime.datetime.strptime(expiry_str, "%Y-%m-%d %H:%M:%S")
            if get_vn_now() > exp: is_expired = True
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

        key_line = f"  ▪ `{k_id}`\n    ├ {stt_icon} | ⏳ {expiry_str}\n    └ 👤 Tạo bởi: {creator}"
        if game_tag not in grouped_keys: grouped_keys[game_tag] = []
        grouped_keys[game_tag].append(key_line)

    if total_count == 0: return bot.reply_to(m, "📭 Danh sách Key trống (Hoặc bạn chưa tạo Key nào).")

    msg = "📊 *DANH SÁCH KEY THEO GAME*\n"
    sorted_games = sorted(grouped_keys.keys())
    for game in sorted_games:
        key_list = grouped_keys[game]
        msg += f"\n➖➖➖➖➖➖➖➖➖➖\n🎮 *{game}* ({len(key_list)} key)\n"
        for line in key_list: msg += line + "\n"

    if len(msg) > 4000:
        safe_part = msg[:4000].replace("`", "").replace("*", "") 
        part_1 = safe_part + "\n\n⚠ Danh sách quá dài, chỉ hiển thị một phần..."
        bot.reply_to(m, part_1)
    else:
        try: bot.reply_to(m, msg, parse_mode="Markdown")
        except Exception: bot.reply_to(m, msg.replace("`", "").replace("*", ""))

# --- LỆNH XÓA KEY (CHỈ ROOT) ---
@bot.message_handler(commands=['delete'])
def delete_key(m):
    if not check_root_admin(m.from_user.id): 
         return bot.reply_to(m, "❌ *TỪ CHỐI:* Chỉ có ROOT ADMIN mới được sử dụng tính năng này!", parse_mode="Markdown")
    try:
        key = m.text.split()[1]
        doc_ref = db.collection('keys').document(key)
        doc = doc_ref.get()
        
        if not doc.exists:
            return bot.reply_to(m, "❌ Key không tồn tại.", parse_mode="Markdown")
            
        doc_ref.delete()
        bot.reply_to(m, f"🗑️ Đã xóa key: `{key}`", parse_mode="Markdown")
        
    except: pass

# --- LỆNH RESET KEY (ADMIN & SUB-ADMIN & USER) ---
@bot.message_handler(commands=['reset'])
def reset_key(m):
    try:
        args = m.text.split()
        if len(args) < 2:
            return bot.reply_to(m, "⚠️ *Sai cú pháp!*\nVui lòng nhập key cần reset.\nVí dụ: `/reset LUATOOL-VIP-0AF3F8`", parse_mode="Markdown")
        
        key = args[1].strip()
        ref = db.collection('keys').document(key)
        doc = ref.get()
        
        if not doc.exists:
            return bot.reply_to(m, "❌ *Lỗi:* Key không tồn tại trên hệ thống. Vui lòng kiểm tra lại chính xác tên Key của bạn.", parse_mode="Markdown")
        
        dt = doc.to_dict()
        is_admin_user = check_admin(m.from_user.id)
        now = get_vn_now()

        if not is_admin_user:
            if dt.get('type') != 'vip':
                return bot.reply_to(m, "❌ *Từ chối:* Tính năng tự reset thiết bị chỉ áp dụng cho **KEY VIP**.", parse_mode="Markdown")

            last_reset_str = dt.get('last_reset_time')
            if last_reset_str:
                try:
                    last_reset = datetime.datetime.strptime(last_reset_str, "%Y-%m-%d %H:%M:%S")
                    next_reset = last_reset + datetime.timedelta(days=1)
                    if now < next_reset:
                        time_str = next_reset.strftime("%H:%M, ngày %d tháng %m năm %Y")
                        return bot.reply_to(m, f"⏳ *Key này đã được reset trước đó!*\nBạn chỉ có thể reset 1 lần/ngày.\n\n👉 Vui lòng quay lại vào lúc: *{time_str}*", parse_mode="Markdown")
                except Exception:
                    pass
        else:
            # Kiểm tra cô lập Admin phụ đối với Reset
            if not check_root_admin(m.from_user.id):
                if dt.get('creator_id') != str(m.from_user.id):
                    return bot.reply_to(m, "❌ *TỪ CHỐI:* Bạn không có quyền Reset Key của người khác!", parse_mode="Markdown")

        update_data = {
            "last_reset_time": now.strftime("%Y-%m-%d %H:%M:%S")
        }
        
        if dt.get('type') == 'vip':
            update_data["hwid"] = None
            update_data["info"] = "Đã Reset (Admin)" if is_admin_user else "Đã Reset (User tự Reset)"
        else:
            update_data["hwids"] = []
            
        ref.update(update_data)
        
        bot.reply_to(m, f"✅ *Thành công!*\nĐã Reset thiết bị cho Key:\n`{key}`\n\nBây giờ bạn có thể đăng nhập vào thiết bị mới.", parse_mode="Markdown")
        
        if not is_admin_user:
            user_name = get_creator_name(m)
            notify_msg = (
                f"♻️ *USER TỰ RESET KEY VIP*\n"
                f"🔑 Key: `{key}`\n"
                f"👤 Khách hàng: `{user_name}` (ID: `{m.from_user.id}`)\n"
                f"🕒 Thời gian: `{now.strftime('%H:%M:%S %d/%m/%y')}`"
            )
            send_admin_notify(notify_msg)
        elif not check_root_admin(m.from_user.id):
            admin_name = get_creator_name(m)
            send_admin_notify(
                f"⚠️ *ADMIN PHỤ RESET KEY*\n"
                f"👤 Tên: `{admin_name}` (ID: `{m.from_user.id}`)\n"
                f"🔑 Key: `{key}`"
            )
            
    except Exception as e:
        bot.reply_to(m, f"❌ Có lỗi hệ thống xảy ra: {e}")

# --- LỆNH RESET TOÀN BỘ KEY (CHỈ ROOT) ---
@bot.message_handler(commands=['resetallkey'])
def reset_all_vip_keys(m):
    if not check_root_admin(m.from_user.id): 
         return bot.reply_to(m, "❌ *TỪ CHỐI:* Chỉ có ROOT ADMIN mới được sử dụng tính năng này!", parse_mode="Markdown")
    msg = bot.reply_to(m, "⏳ *Đang tiến hành reset thiết bị. Vui lòng đợi...*", parse_mode="Markdown")
    try:
        docs = db.collection('keys').where('type', '==', 'vip').stream()
        count = 0
        user_id = str(m.from_user.id)
        is_root = check_root_admin(user_id)
        
        for doc in docs:
            v = doc.to_dict()
            if not is_root and v.get('creator_id') != user_id:
                continue
                
            ref = db.collection('keys').document(doc.id)
            ref.update({"hwid": None, "info": "Đã Reset"})
            count += 1
            
        bot.edit_message_text(f"✅ *Hoàn tất!*\nĐã reset thành công thiết bị cho `{count}` KEY VIP.", chat_id=m.chat.id, message_id=msg.message_id, parse_mode="Markdown")
            
    except Exception as e:
        bot.edit_message_text(f"❌ *Có lỗi xảy ra:*\n`{e}`", chat_id=m.chat.id, message_id=msg.message_id, parse_mode="Markdown")

# --- LỆNH KHÓA KEY (CHỈ ROOT) ---
@bot.message_handler(commands=['lockkey'])
def lock_key_cmd(m):
    if not check_root_admin(m.from_user.id): 
         return bot.reply_to(m, "❌ *TỪ CHỐI:* Chỉ có ROOT ADMIN mới được sử dụng tính năng này!", parse_mode="Markdown")
    try:
        args = m.text.split()
        if len(args) < 2: return bot.reply_to(m, "⚠️ Nhập tên key!")
        key = args[1]
        ref = db.collection('keys').document(key)
        doc = ref.get()
        if doc.exists:
            ref.update({"is_locked": True})
            bot.reply_to(m, f"🔒 Đã KHÓA key: `{key}`\n(User sẽ bị đá sau 30s)", parse_mode="Markdown")
                
        else: bot.reply_to(m, "❌ Key không tồn tại.")
    except Exception as e: bot.reply_to(m, f"Lỗi: {e}")

# --- LỆNH MỞ KHÓA KEY (CHỈ ROOT) ---
@bot.message_handler(commands=['unlockkey'])
def unlock_key_cmd(m):
    if not check_root_admin(m.from_user.id): 
         return bot.reply_to(m, "❌ *TỪ CHỐI:* Chỉ có ROOT ADMIN mới được sử dụng tính năng này!", parse_mode="Markdown")
    try:
        args = m.text.split()
        if len(args) < 2: return bot.reply_to(m, "⚠️ Nhập tên key!")
        key = args[1]
        ref = db.collection('keys').document(key)
        doc = ref.get()
        if doc.exists:
            ref.update({"is_locked": False})
            bot.reply_to(m, f"🔓 Đã MỞ KHÓA key: `{key}`", parse_mode="Markdown")
                
        else: bot.reply_to(m, "❌ Key không tồn tại.")
    except Exception as e: bot.reply_to(m, f"Lỗi: {e}")

# --- LỆNH DANH SÁCH KEY BỊ KHÓA (CHỈ ROOT) ---    
@bot.message_handler(commands=['listlockkey'])
def list_locked_keys_cmd(m):
    if not check_root_admin(m.from_user.id): 
         return bot.reply_to(m, "❌ *TỪ CHỐI:* Chỉ có ROOT ADMIN mới được sử dụng tính năng này!", parse_mode="Markdown")
    try:
        docs = db.collection('keys').where('is_locked', '==', True).stream()
        count = 0
        msg = "🔒 *DANH SÁCH KEY ĐANG BỊ KHÓA* 🔒\n\n"
        
        user_id = str(m.from_user.id)
        is_root = check_root_admin(user_id)
        
        for doc in docs:
            v = doc.to_dict()
            if not is_root and v.get('creator_id') != user_id:
                continue
                
            count += 1
            k_id = doc.id
            game_tag = v.get('game_id', 'KHÁC').upper()
            key_type = v.get('type', 'Unknown').upper()
            info = v.get('info', 'Không có ghi chú')
            hwid = v.get('hwid', 'Chưa Active')
            msg += f"▪ `{k_id}` ({game_tag} - {key_type})\n  ├ 📱 HWID: `{hwid}`\n  └ ⚠ Lý do: {info}\n\n"
            
        if count == 0: return bot.reply_to(m, "📭 Hiện tại không có Key nào của bạn bị khóa.")
        
        final_msg = f"📊 *Tổng cộng:* {count} Key bị khóa.\n" + "="*20 + "\n\n" + msg
        
        if len(final_msg) > 4000:
            safe_part = final_msg[:4000].replace("`", "").replace("*", "") 
            part_1 = safe_part + "\n\n⚠️ Danh sách quá dài, chỉ hiển thị một phần..."
            bot.reply_to(m, part_1)
        else:
            try: bot.reply_to(m, final_msg, parse_mode="Markdown")
            except Exception: bot.reply_to(m, final_msg.replace("`", "").replace("*", ""))
            
    except Exception as e: bot.reply_to(m, f"Lỗi: {e}")

# --- LỆNH CHẶN THIẾT BỊ (CHỈ ROOT) ---
@bot.message_handler(commands=['blockmodel'])
def block_model_cmd(m):
    if not check_root_admin(m.from_user.id): 
         return bot.reply_to(m, "❌ *TỪ CHỐI:* Chỉ có ROOT ADMIN mới được sử dụng tính năng này!", parse_mode="Markdown")
    try:
        cmd_text = m.text.replace("/blockmodel", "").strip()
        if not cmd_text: return bot.reply_to(m, "⚠️ Nhập HWID Thiết bị!")
        
        # Lưu vào Collection bị chặn mới có định danh
        db.collection('blocked_models').document(cmd_text).set({
            "creator_id": str(m.from_user.id),
            "creator": get_creator_name(m),
            "created_at": firestore.SERVER_TIMESTAMP
        })
        
        bot.reply_to(m, f"🚫 Đã thêm Model vào Blacklist:\n`{cmd_text}`", parse_mode="Markdown")
            
    except Exception as e: bot.reply_to(m, f"Lỗi: {e}")

# --- LỆNH MỞ CHẶN THIẾT BỊ (CHỈ ROOT) ---
@bot.message_handler(commands=['unlockmodel'])
def unlock_model_cmd(m):
    if not check_root_admin(m.from_user.id): 
         return bot.reply_to(m, "❌ *TỪ CHỐI:* Chỉ có ROOT ADMIN mới được sử dụng tính năng này!", parse_mode="Markdown")
    try:
        cmd_text = m.text.replace("/unlockmodel", "").strip()
        if not cmd_text: return bot.reply_to(m, "⚠️ Nhập HWID Thiết bị!")
        
        user_id = str(m.from_user.id)
        is_root = check_root_admin(user_id)
        
        doc_ref = db.collection('blocked_models').document(cmd_text)
        doc = doc_ref.get()
        
        # Check fallback từ legacy
        legacy_ref = db.collection('settings').document('blacklist')
        legacy_doc = legacy_ref.get()
        in_legacy = False
        if legacy_doc.exists and cmd_text in legacy_doc.to_dict().get('models', []):
            in_legacy = True
            
        if not doc.exists and not in_legacy:
            return bot.reply_to(m, "❌ Thiết bị này không có trong danh sách chặn.")

        if doc.exists:
            doc_ref.delete()
            
        if in_legacy:
            legacy_ref.update({"models": firestore.ArrayRemove([cmd_text])})

        bot.reply_to(m, f"✅ Đã gỡ chặn Model:\n`{cmd_text}`", parse_mode="Markdown")
            
    except Exception as e: bot.reply_to(m, f"Lỗi: {e}")

# --- LỆNH DANH SÁCH THIẾT BỊ BỊ CHẶN (CHỈ ROOT) ---
@bot.message_handler(commands=['listblock'])
def list_block_cmd(m):
    if not check_root_admin(m.from_user.id): 
         return bot.reply_to(m, "❌ *TỪ CHỐI:* Chỉ có ROOT ADMIN mới được sử dụng tính năng này!", parse_mode="Markdown")
    try:
        user_id = str(m.from_user.id)
        is_root = check_root_admin(user_id)
        
        msg = "🚫 **DANH SÁCH THIẾT BỊ BỊ CHẶN** 🚫\n"
        count = 0
        
        if is_root:
            # Root xem toàn bộ chặn mới
            docs = db.collection('blocked_models').stream()
            for doc in docs:
                count += 1
                msg += f"- `{doc.id}` (Bởi {doc.to_dict().get('creator', 'Admin')})\n"
                
            # Root xem cả chặn cũ legacy
            legacy_ref = db.collection('settings').document('blacklist').get()
            if legacy_ref.exists:
                for mod in legacy_ref.to_dict().get('models', []):
                    count += 1
                    msg += f"- `{mod}` (Hệ thống cũ)\n"
        else:
            # Sub-admin chỉ xem chặn của họ
            docs = db.collection('blocked_models').where('creator_id', '==', user_id).stream()
            for doc in docs:
                count += 1
                msg += f"- `{doc.id}`\n"
                
        if count == 0:
            return bot.reply_to(m, "📋 Danh sách chặn của bạn trống.")
            
        bot.reply_to(m, msg, parse_mode="Markdown")
    except Exception as e: bot.reply_to(m, f"Lỗi: {e}")

# ================= STARTUP =================
def start_services():
    # Set Menu mặc định cho những ai chưa từng nhắn tin bot (Menu User thường)
    try:
        cmd_user = [
            BotCommand("start", "Khởi động lại Bot"),
            BotCommand("help", "Xem hướng dẫn tự reset Key"),
            BotCommand("reset", "Tự động reset Key VIP (1 lần/ngày)")
        ]
        bot.set_my_commands(cmd_user, scope=BotCommandScopeDefault())
    except Exception as e:
        print(f"Lỗi set menu mặc định: {e}")

    # Set Menu cho Root Admin lúc server mới lên
    try:
        if REAL_ADMIN_ID:
            setup_user_menu(REAL_ADMIN_ID)
    except: pass

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
    # Thêm lấy game_id (từ Lua gửi lên), nếu không có thì mặc định là luapak
    game_id = request.form.get('game_id', 'luapak').strip().lower()

    if not base64_image or not caption or not vip_key or not hwid:
        return jsonify({"status": False, "msg": "Thiếu dữ liệu hoặc nghi ngờ Spam"}), 400

    import re
    clean_caption = caption.replace('\r', '')
    # LƯU Ý: \*\*\*\*\* yêu cầu text từ Lua gửi lên MẶC ĐỊNH phải là 5 dấu sao. Nếu đổi thành tên thật sẽ bị Ban.
    safe_pattern = r"^🏆 <b>PAK LUA VIP AKMODPUBG</b> 🏆\n🔥 <b>AUTO FEEDBACK GROUP VIP</b> 🔥\n⏰ <b>Thời gian: .*</b>\n👤 <b>Tên nhân vật: \*\*\*\*\*</b>\n🔑 <b>UID: .*</b>\n🔫 <b>Số Kill: \d+</b>\n🎖 <b>Rank: .*</b>\n💬 <b>MUA MOD VIP IB ADMIN @nanamod96</b>$"
    
    if not re.match(safe_pattern, clean_caption):
        try:
            # Fix lỗi Hardcode "LUAPAK" thành biến game_id
            doc, real_key_id = get_key_document(vip_key, game_id)
            if doc:
                db.collection('keys').document(real_key_id).update({
                    "is_locked": True,
                    "info": "Auto Ban: Dùng tool sửa bậy Text Feedback"
                })
                
                if hwid:
                    db.collection('blocked_models').document(hwid).set({
                        "creator_id": "AUTO_BAN",
                        "creator": "Hệ Thống",
                        "created_at": firestore.SERVER_TIMESTAMP
                    })

                notify_msg = (
                    f"🚫 *AUTO BAN SỬA TEXT* 🚫\n"
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

    if not db:
        return jsonify({"status": False, "msg": "Lỗi DB"}), 500
        
    doc, real_key_id = get_key_document(vip_key, game_id) # Fix lỗi ở đây
    if not doc:
        return jsonify({"status": False, "msg": "Lỗi kết nối hoặc Key không hợp lệ"}), 403
        
    key_data = doc.to_dict()
    if key_data.get('is_locked', False):
        return jsonify({"status": False, "msg": "Lỗi kết nối"}), 403
        
    try:
        expiry_dt = datetime.datetime.strptime(key_data.get('expiry', ''), "%Y-%m-%d %H:%M:%S")
        if get_vn_now() > expiry_dt:
            return jsonify({"status": False, "msg": "Key đã hết hạn!"}), 403
    except:
        pass

    if key_data.get('type') == 'vip':
        if key_data.get('hwid') and key_data.get('hwid') != hwid:
            return jsonify({"status": False, "msg": "ERROR"}), 403
    else:
        if hwid not in key_data.get('hwids', []):
            return jsonify({"status": False, "msg": "ERROR"}), 403

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
                pass
                
            return jsonify({"status": False, "msg": "Phát hiện Spam! Key của bạn đã bị khóa vĩnh viễn."}), 429

    LAST_FEEDBACK_TIME[hwid] = current_time

    try:
        base64_image = base64_image.replace(" ", "+")
        padding_needed = len(base64_image) % 4
        if padding_needed:
            base64_image += '=' * (4 - padding_needed)

        raw_image_bytes = base64.b64decode(base64_image)

        # Xả RAM ngay lập tức sau khi xử lý ảnh bằng "with"
        from PIL import Image
        with Image.open(io.BytesIO(raw_image_bytes)) as img:
            width, height = img.size
            crop_height = int(height * 0.95)
            cropped_img = img.crop((0, 0, width, crop_height))
            
            output_io = io.BytesIO()
            cropped_img.save(output_io, format='JPEG', quality=85)
            final_image_bytes = output_io.getvalue()
        
    except Exception as e:
        return jsonify({"status": False, "msg": f"Lỗi xử lý ảnh: {str(e)}"}), 400

    use_token = FEEDBACK_BOT_TOKEN if FEEDBACK_BOT_TOKEN else BOT_TOKEN
    tg_data = {
        "chat_id": FEEDBACK_CHAT_ID,
        "caption": caption,
        "parse_mode": "HTML"
    }
    
    if len(final_image_bytes) > 9.5 * 1024 * 1024:
        tg_url = f"https://api.telegram.org/bot{use_token}/sendDocument"
        tg_files = {"document": ("top1_akmod_safe.jpg", final_image_bytes, "image/jpeg")}
    else:
        tg_url = f"https://api.telegram.org/bot{use_token}/sendPhoto"
        tg_files = {"photo": ("top1_akmod_safe.jpg", final_image_bytes, "image/jpeg")}
    
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
    status = request.form.get('status', 'alive').strip()

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

    # Xóa UID bị disconnect
    for p_uid in uids_to_remove:
        del MATCH_SESSIONS[match_id][p_uid]
        
    # CHỐNG TRÀN RAM: Xóa Match ID nếu trận đấu rỗng
    if not MATCH_SESSIONS[match_id]:
        del MATCH_SESSIONS[match_id]
        
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

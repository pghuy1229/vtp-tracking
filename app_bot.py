import json
import os
import re
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests
from telegram import Update, BotCommand
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK - Bot ViettelPost is running")

    def log_message(self, format, *args):
        pass  # Không in log rác

def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

# ================= CONFIG =================
TELEGRAM_BOT_TOKEN = "8793779707:AAHcoKeeDN_CGc6XUbuWSc7We-SqyxnUtG8"
CAPMONSTER_KEY = "ab2f6127c53e698230580f9f1d31aa6a"

RECAPTCHA_KEY = "6LciQq8eAAAAAIFSqZTSd6P8wrBYoilzdvudW3Nc"
PAGE_URL = "https://viettelpost.vn/tra-cuu-hanh-trinh-don"
TRACKING_API_URL = "https://api.viettelpost.vn/api/orders/viewTrackingOrders3"
DB_FILE = "orders_db.json"

# Proxy xoay DataImpulse
PROXY_HOST = "gw.dataimpulse.com"
PROXY_PORT = 823
PROXY_USER = "a275cc8d5d7d29b73177"
PROXY_PASS = "466361f8240a1b26"
PROXIES = {
    "http": f"http://{PROXY_USER}:{PROXY_PASS}@{PROXY_HOST}:{PROXY_PORT}",
    "https": f"http://{PROXY_USER}:{PROXY_PASS}@{PROXY_HOST}:{PROXY_PORT}"
}
# ==========================================

def load_db():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_db(data):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def solve_recaptcha() -> str:
    """Giải reCAPTCHA v2 qua CapMonster bằng Proxy xoay DataImpulse"""
    task_payload = {
        "clientKey": CAPMONSTER_KEY,
        "task": {
            "type": "NoCaptchaTask",
            "websiteURL": PAGE_URL,
            "websiteKey": RECAPTCHA_KEY,
            "proxyType": "http",
            "proxyAddress": PROXY_HOST,
            "proxyPort": PROXY_PORT,
            "proxyLogin": PROXY_USER,
            "proxyPassword": PROXY_PASS,
            "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
    }
    res = requests.post("https://api.capmonster.cloud/createTask", json=task_payload, timeout=20).json()
    if res.get("errorId", 0) != 0:
        raise Exception(f"Lỗi tạo task CapMonster: {res.get('errorDescription')}")
    task_id = res["taskId"]

    for _ in range(35):
        time.sleep(3)
        check = requests.post("https://api.capmonster.cloud/getTaskResult", json={"clientKey": CAPMONSTER_KEY, "taskId": task_id}, timeout=20).json()
        if check.get("status") == "ready":
            return check["solution"]["gRecaptchaResponse"]
        if check.get("errorId", 0) != 0:
            raise Exception(f"Lỗi giải task CapMonster: {check.get('errorDescription')}")
    raise TimeoutError("CapMonster timeout khi chờ giải Captcha.")

def fetch_order_info(order_code: str) -> dict:
    """Gửi request tra cứu đến ViettelPost thông qua Proxy DataImpulse để tránh WAF 403"""
    token = solve_recaptcha()
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Origin": "https://viettelpost.vn",
        "Referer": "https://viettelpost.vn/"
    }
    payload = {"captcha": token, "orders": order_code.strip()}
    res = requests.post(TRACKING_API_URL, json=payload, headers=headers, proxies=PROXIES, timeout=25).json()
    if res.get("data") and len(res["data"]) > 0:
        return res["data"][0]
    return None

def extract_price(order_data: dict) -> str:
    cuoc_str = ""
    hang_str = ""
    tracking_orders = order_data.get("TRACKING_ORDERS", [])
    for grp in tracking_orders:
        for sub in grp.get("TRACKINGS", []):
            nd = sub.get("NOI_DUNG", "")
            if not cuoc_str:
                m = re.search(r"Tiền cước cần thu hộ:\s*([\d,.]+VNĐ)", nd)
                if m: cuoc_str = m.group(1).strip()
            if not hang_str:
                m = re.search(r"Tiền hàng cần thu hộ:\s*([\d,.]+VNĐ)", nd)
                if m and m.group(1) != "0VNĐ": hang_str = m.group(1).strip()
            if not cuoc_str:
                m = re.search(r"Thu hộ:\s*([\d,.]+VNĐ)", nd)
                if m: cuoc_str = m.group(1).strip()

    price_parts = []
    if cuoc_str and cuoc_str != "0VNĐ": price_parts.append(f"Cước phí: {cuoc_str}")
    if hang_str and hang_str != "0VNĐ": price_parts.append(f"Tiền hàng COD: {hang_str}")
    return " | ".join(price_parts) if price_parts else "0 VNĐ"

def is_completed_status(status_name: str, status_code) -> bool:
    if status_code in [501, "501"]: return True
    if not status_name: return False
    s = status_name.lower()
    return any(k in s for k in ["giao thành công", "đã giao hàng", "phát thành công", "hoàn tất"])

def get_routing_info(order_data: dict):
    raw_status = order_data.get("TRANGTHAI", "Không rõ")
    is_done = is_completed_status(raw_status, order_data.get("MA_TRANGTHAI"))
    curr_status = "Giao thành công" if is_done else raw_status

    if not is_done and curr_status in ["Đang kết nối", "Đã nhận hàng", "Đang nhận hàng"]:
        curr_status = "Đang nhận hàng"

    hien_tai = "Chưa rõ"
    tiep_theo = "Chưa rõ"
    nguoi_nhan = ""

    tracking_orders = order_data.get("TRACKING_ORDERS", [])
    if tracking_orders and tracking_orders[0].get("TRACKINGS"):
        latest = tracking_orders[0]["TRACKINGS"][0]
        bc_di = [latest.get("TEN_BUUCUC_DI"), latest.get("QUAN_HUYEN_BUU_CUC_DI"), latest.get("TINH_BUU_CUC_DI")]
        di_parts = [p for p in bc_di if p]
        if di_parts: hien_tai = " - ".join(di_parts)

        if is_done:
            tiep_theo = "Đã phát tới tay người nhận"
            nguoi_nhan = latest.get("RECEIVER_FULLNAME") or order_data.get("RECEIVER_FULLNAME") or ""
            nd = latest.get("NOI_DUNG", "")
            if "Người nhận:" in nd:
                m = re.search(r"Người nhận:\s*([^.]+)", nd)
                if m: nguoi_nhan = m.group(1).strip()
        else:
            bc_den = latest.get("TEN_BUUCUC_DEN")
            ma_den = latest.get("MA_BUUCUC_DEN")
            den_name = f"{bc_den} ({ma_den})" if (bc_den and ma_den) else (bc_den or "")
            den_parts = [p for p in [den_name, latest.get("QUAN_HUYEN_BUU_CUC_DEN"), latest.get("TINH_BUU_CUC_DEN")] if p]
            if den_parts: tiep_theo = " - ".join(den_parts)

    return curr_status, hien_tai, tiep_theo, nguoi_nhan, is_done

def format_order_msg(order_data: dict, is_update=False, note="") -> str:
    mavandon = order_data.get("MAVANDON", "N/A")
    trangthai, hien_tai, tiep_theo, nguoi_nhan, is_done = get_routing_info(order_data)
    price_info = extract_price(order_data)

    if is_done:
        header = "🎉 *ĐƠN HÀNG ĐÃ GIAO THÀNH CÔNG!* (Đã dừng theo dõi)"
    elif is_update:
        header = "🔔 *CẬP NHẬT TRẠNG THÁI MỚI!*"
    else:
        header = "📦 *THÔNG TIN ĐƠN HÀNG*"

    note_line = f"📝 *Ghi chú đơn:* `{note}`\n" if note else ""
    nguoi_nhan_line = f"👤 *Người đã nhận:* {nguoi_nhan}\n" if nguoi_nhan else ""
    gui = f"{order_data.get('SENDER_DISTRICT', '')}, {order_data.get('SENDER_PROVINCE', '')}".strip(", ")
    nhan = f"{order_data.get('RECEIVER_DISTRICT', '')}, {order_data.get('RECEIVER_PROVINCE', '')}".strip(", ")

    msg = (
        f"{header}\n"
        f"Mã vận đơn: `{mavandon}`\n"
        f"{note_line}━━━━━━━━━━━━━━━━━━━\n"
        f"📌 *Trạng thái:* `{trangthai}`\n"
        f"{nguoi_nhan_line}"
        f"📍 *Bưu cục phát:* {hien_tai}\n"
        f"➡️ *Điểm đến:* {tiep_theo}\n"
        f"💵 *Đơn giá / Cước:* {price_info}\n"
        f"🕒 *Cập nhật:* {order_data.get('NGAY_TRANGTHAI', 'N/A')}\n"
        f"📅 *Dự kiến giao:* {order_data.get('EXPECTED_TIME') or 'Chưa rõ'}\n"
        f"⚖️ *Khối lượng:* {order_data.get('TRONG_LUONG', 0)}g\n"
        f"🚚 *Dịch vụ:* {order_data.get('DICH_VU', 'N/A')}\n"
        f"🏠 *Tuyến:* {gui or 'N/A'} ➔ {nhan or 'N/A'}"
    )
    return msg

async def post_init(application):
    commands = [
        BotCommand("check", "Tra cứu nhanh (/check <mã>)"),
        BotCommand("add", "Theo dõi kèm tên (/add <mã>|<tên>)"),
        BotCommand("list", "Xem danh sách đơn theo dõi"),
        BotCommand("del", "Hủy theo dõi đơn (/del <mã>)"),
        BotCommand("start", "Hướng dẫn sử dụng")
    ]
    await application.bot.set_my_commands(commands)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    help_text = (
        f"🤖 *Viettel Post Tracking*\n"
        f"ID Chat: `{chat_id}`\n\n"
        f"*Menu lệnh nhanh (hoặc bấm nút Menu góc dưới):*\n"
        f"• `/check <mã>`: Tra cứu ngay đơn hàng\n"
        f"• `/add <mã>|<tên>`: Thêm theo dõi & tra cứu ngay (Vd: `/add 154559781545|Đơn áo thun`)\n"
        f"• `/list`: Xem danh sách đơn đang theo dõi\n"
        f"• `/del <mã>`: Hủy theo dõi đơn\n\n"
        f"*(Đơn giao thành công sẽ tự động dừng theo dõi và xóa khỏi danh sách).*"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")

async def add_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    if not context.args:
        await update.message.reply_text("⚠️ Nhập mã đơn. Ví dụ: `/add 154559781545|Đơn áo`", parse_mode="Markdown")
        return

    raw_input = " ".join(context.args)
    code = raw_input
    note = ""
    if "|" in raw_input:
        parts = raw_input.split("|", 1)
        code = parts[0].strip()
        note = parts[1].strip()

    wait_msg = await update.message.reply_text(f"⏳ Đang giải Captcha & tra cứu `{code}`{f' ({note})' if note else ''}...", parse_mode="Markdown")
    try:
        data = fetch_order_info(code)
        curr_status = ""
        curr_time = ""

        if data:
            curr_status, _, _, _, is_done = get_routing_info(data)
            curr_time = data.get("NGAY_TRANGTHAI", "")
            await wait_msg.edit_text(format_order_msg(data, False, note), parse_mode="Markdown")

            if is_done:
                return
        else:
            await wait_msg.edit_text(f"❌ Không tìm thấy thông tin trên ViettelPost cho mã `{code}`.")
            return

        db = load_db()
        if chat_id not in db: db[chat_id] = {}
        db[chat_id][code] = {
            "note": note,
            "last_status": curr_status,
            "last_time": curr_time
        }
        save_db(db)
    except Exception as e:
        await wait_msg.edit_text(f"❌ Lỗi: {str(e)}")

async def list_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    db = load_db()
    user_orders = db.get(chat_id, {})
    if not user_orders:
        await update.message.reply_text("📭 Bạn chưa theo dõi mã đơn nào.")
        return

    msg = "📋 *DANH SÁCH ĐƠN ĐANG THEO DÕI:*\n\n"
    for code, info in user_orders.items():
        note_str = f" ({info.get('note')})" if info.get("note") else ""
        st = info.get("last_status") or "Chờ quét lần đầu"
        msg += f"• `{code}`{note_str} - `{st}`\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def del_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    if not context.args:
        await update.message.reply_text("⚠️ Cú pháp: `/del <mã_vận_đơn>`")
        return
    code = context.args[0].strip()
    db = load_db()
    if chat_id in db and code in db[chat_id]:
        del db[chat_id][code]
        save_db(db)
        await update.message.reply_text(f"🗑️ Đã xóa mã `{code}`.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"⚠️ Mã `{code}` không tồn tại trong danh sách.")

async def check_single(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Cú pháp: `/check <mã_vận_đơn>`")
        return
    code = context.args[0].strip()
    wait_msg = await update.message.reply_text(f"⏳ Đang giải Captcha & tra cứu `{code}`...", parse_mode="Markdown")
    try:
        data = fetch_order_info(code)
        if data:
            await wait_msg.edit_text(format_order_msg(data), parse_mode="Markdown")
        else:
            await wait_msg.edit_text(f"❌ Không tìm thấy thông tin cho mã `{code}`.")
    except Exception as e:
        await wait_msg.edit_text(f"❌ Lỗi: {str(e)}")

async def hourly_tracking_job(context: ContextTypes.DEFAULT_TYPE):
    db = load_db()
    for chat_id, orders in list(db.items()):
        for code, info in list(orders.items()):
            try:
                data = fetch_order_info(code)
                if not data:
                    continue

                curr_status, _, _, _, is_done = get_routing_info(data)
                curr_time = data.get("NGAY_TRANGTHAI", "")

                if is_done:
                    # Giao thành công: Báo tin & tự xóa khỏi danh sách theo dõi
                    del db[chat_id][code]
                    save_db(db)
                    msg = format_order_msg(data, is_update=True, note=info.get("note", ""))
                    await context.bot.send_message(chat_id=int(chat_id), text=msg, parse_mode="Markdown")
                elif curr_status != info.get("last_status") or curr_time != info.get("last_time"):
                    db[chat_id][code]["last_status"] = curr_status
                    db[chat_id][code]["last_time"] = curr_time
                    save_db(db)
                    msg = format_order_msg(data, is_update=True, note=info.get("note", ""))
                    await context.bot.send_message(chat_id=int(chat_id), text=msg, parse_mode="Markdown")

                time.sleep(3)
            except Exception as e:
                print(f"Error checking {code}: {e}")

def main():
    # Khởi động web server mini để Render detect port mở (tránh Deploy Failed trên Render Free Web Service)
    t = threading.Thread(target=run_health_server, daemon=True)
    t.start()
    port = os.environ.get("PORT", "10000")
    print(f"[+] Health-check web server dang lang nghe tai port {port}...")

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add_orders))
    app.add_handler(CommandHandler("list", list_orders))
    app.add_handler(CommandHandler("del", del_order))
    app.add_handler(CommandHandler("check", check_single))

    app.job_queue.run_repeating(hourly_tracking_job, interval=3600, first=10)

    print("[+] Bot ViettelPost dang chay kem Proxy DataImpulse (Polling & Auto check 1h)...")
    app.run_polling()

if __name__ == "__main__":
    main()

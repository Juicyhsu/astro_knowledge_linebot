import sys
import os
import re
from datetime import datetime, timedelta, time as datetime_time
import json
import threading
import time

from dotenv import load_dotenv
load_dotenv()

import google.generativeai as genai
import PIL
from google.generativeai.types import HarmCategory, HarmBlockThreshold

from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent,
    ImageMessageContent,
    JoinEvent,
)
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    MessagingApiBlob,
    ReplyMessageRequest,
    TextMessage,
    PushMessageRequest,
)

# ── Gemini 設定 ──────────────────────────────────────────────
gemini_key = os.environ.get("GEMINI_API_KEY")
if not gemini_key or gemini_key == "YOUR_GEMINI_API_KEY":
    print("Warning: GEMINI_API_KEY is not set. Gemini chat will not work.")
    genai.configure(api_key="")
else:
    genai.configure(api_key=gemini_key)

ASTROLOGY_SYSTEM_PROMPT = """
你是一位專業的西洋占星助理。

專長範圍：十二星座、行星意義、宮位涵義、行星相位、本命盤解讀、行運推運、星座相容性。

回答風格：
- 使用繁體中文，語氣親切專業
- 回答有深度但不艱澀，善用例子
- 若問題超出占星範疇，溫和引導回占星主題
- 可主動補充相關延伸知識
- 若收到星盤圖片但資訊不清晰，請告知使用者並請他補充文字說明

【重要】每次回答字數控制在 100～150 字之間，不可超過。
"""

model = genai.GenerativeModel(
    model_name="gemini-2.5-flash",
    safety_settings={
        HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
    },
    generation_config={
        "temperature": 0.7,
        "top_p": 0.95,
        "top_k": 40,
        "max_output_tokens": 8192,
    },
    system_instruction=ASTROLOGY_SYSTEM_PROMPT,
)

UPLOAD_FOLDER = "static"

app = Flask(__name__)

channel_access_token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
channel_secret = os.environ.get("LINE_CHANNEL_SECRET")
if not channel_secret:
    print("Specify LINE_CHANNEL_SECRET as environment variable.")
    sys.exit(1)
if not channel_access_token:
    print("Specify LINE_CHANNEL_ACCESS_TOKEN as environment variable.")
    sys.exit(1)

handler = WebhookHandler(channel_secret)
configuration = Configuration(access_token=channel_access_token)

# 取得 Bot 自己的 user_id (使用 lazy load 避免啟動時連線或金鑰錯誤導致 Crash)
BOT_USER_ID = None

def get_bot_user_id():
    global BOT_USER_ID
    if BOT_USER_ID is None:
        try:
            with ApiClient(configuration) as api_client:
                BOT_USER_ID = MessagingApi(api_client).get_bot_info().user_id
        except Exception as e:
            print(f"Warning: Could not retrieve bot user ID: {e}")
    return BOT_USER_ID

chat_sessions = {}
last_activity = {}
user_images = {}
SESSION_TIMEOUT = timedelta(days=7)

# ── 工具函數：儲存群組 ID ──────────────────────────────────────
def save_group_id(group_id):
    """保存有效的群組或聊天室 ID 以供定時推播使用"""
    if not group_id:
        return
    # 嚴格防護：只保存以 'C' (群組) 或 'R' (多人聊天室) 開頭的 ID，排除以 'U' (個人使用者) 開頭的 ID，確保私聊絕對不會被推播！
    if not (group_id.startswith("C") or group_id.startswith("R")):
        return
    try:
        group_ids = set()
        if os.path.exists("group_ids.txt"):
            with open("group_ids.txt", "r", encoding="utf-8") as f:
                group_ids = {line.strip() for line in f if line.strip()}
        if group_id not in group_ids:
            with open("group_ids.txt", "a", encoding="utf-8") as f:
                f.write(f"{group_id}\n")
            print(f"[Scheduler] Saved new group ID: {group_id}")
    except Exception as e:
        print(f"Error saving group ID: {e}")

# ── 工具函數 ──────────────────────────────────────────────────
def is_mentioned_in_text(event):
    """文字訊息的 @ 判斷"""
    mention = getattr(event.message, "mention", None)
    bot_id = get_bot_user_id()
    return (
        mention
        and mention.mentionees
        and bot_id
        and any(m.user_id == bot_id for m in mention.mentionees)
    )

# ── Webhook ───────────────────────────────────────────────────
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

# ── 加入群組事件 ──────────────────────────────────────────────
@handler.add(JoinEvent)
def handle_join(event):
    # 保存群組 ID
    if event.source.type == "group":
        save_group_id(event.source.group_id)
    elif event.source.type == "room":
        save_group_id(event.source.room_id)

    # 當機器人加入群組時發送歡迎訊息
    try:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message_with_http_info(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="大家好！我是西洋占星助理。🔮\n在群組中，請 @我 並輸入您的問題，我就會為您解答占星知識喔！✨")],
                )
            )
    except Exception as e:
        print(f"Error handling JoinEvent: {e}")

# ── 文字訊息 ──────────────────────────────────────────────────
@handler.add(MessageEvent, message=TextMessageContent)
def message_text(event):
    # 保存群組 ID
    if event.source.type == "group":
        save_group_id(event.source.group_id)
        if not is_mentioned_in_text(event):
            return
    elif event.source.type == "room":
        save_group_id(event.source.room_id)
        if not is_mentioned_in_text(event):
            return

    user_id = event.source.user_id
    clean_text = re.sub(r"@\S+\s*", "", event.message.text).strip()
    if not clean_text:
        clean_text = "你好"

    result = gemini_chat(clean_text, user_id)

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=result)],
            )
        )

# ── 圖片訊息 ──────────────────────────────────────────────────
@handler.add(MessageEvent, message=ImageMessageContent)
def message_image(event):
    # 保存群組 ID
    if event.source.type == "group":
        save_group_id(event.source.group_id)
    elif event.source.type == "room":
        save_group_id(event.source.room_id)

    user_id = event.source.user_id

    try:
        os.makedirs(UPLOAD_FOLDER, exist_ok=True)
        with ApiClient(configuration) as api_client:
            blob_api = MessagingApiBlob(api_client)
            message_content = blob_api.get_message_content(message_id=event.message.id)

        image_path = os.path.join(UPLOAD_FOLDER, f"{user_id}_image.jpg")
        with open(image_path, "wb") as f:
            f.write(message_content)

        user_images[user_id] = image_path

        # 如果是群組/多人聊天，我們不主動回覆「收到圖片」以避免洗版，僅在私聊時進行提示
        if event.source.type not in ("group", "room"):
            with ApiClient(configuration) as api_client:
                line_bot_api = MessagingApi(api_client)
                line_bot_api.reply_message_with_http_info(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="星盤圖片收到了 🌙 請問你想了解哪個部分呢？")],
                    )
                )

    except Exception as e:
        print(f"Image upload error: {e}")
        if event.source.type not in ("group", "room"):
            try:
                with ApiClient(configuration) as api_client:
                    line_bot_api = MessagingApi(api_client)
                    line_bot_api.reply_message_with_http_info(
                        ReplyMessageRequest(
                            reply_token=event.reply_token,
                            messages=[TextMessage(text="圖片上傳失敗，請再試一次。")],
                        )
                    )
            except Exception as reply_err:
                print(f"Failed to send failure message: {reply_err}")

# ── Gemini 對話（含記憶） ─────────────────────────────────────
def gemini_chat(user_input, user_id):
    global chat_sessions, last_activity, user_images

    # 檢查 Gemini API Key 是否存在
    gemini_key = os.environ.get("GEMINI_API_KEY")
    if not gemini_key or gemini_key == "YOUR_GEMINI_API_KEY":
        return "提醒您，目前的 Gemini API Key 還沒有設定完成。請通知管理員在伺服器端填寫金鑰！✨"

    # 清除記憶
    clear_keywords = ["重新開始", "清除記憶", "忘記我", "重置對話", "清空記憶", "reset", "重來"]
    if any(kw in user_input for kw in clear_keywords):
        chat_sessions.pop(user_id, None)
        last_activity.pop(user_id, None)
        user_images.pop(user_id, None)
        return "好的，記憶已清除，讓我們重新開始！有什麼占星問題想問我嗎？✨"

    # 過期檢查
    now = datetime.now()
    if user_id in last_activity:
        if now - last_activity[user_id] > SESSION_TIMEOUT:
            chat_sessions.pop(user_id, None)
            user_images.pop(user_id, None)

    if user_id not in chat_sessions:
        chat_sessions[user_id] = model.start_chat(history=[])

    last_activity[user_id] = now
    chat = chat_sessions[user_id]

    now_str = datetime.now().strftime("%Y年%m月%d日")
    time_context = f"【系統提示：當前的真實地球日期是 {now_str}。請以此作為「今天、現在、當下」來計算與分析流年行運，絕對不要搞錯年份！】\n"

    try:
        if user_id in user_images:
            upload_image = PIL.Image.open(user_images[user_id])
            # 修正引導詞：強烈指示 Gemini 該圓形圖表為西洋占星座星盤/本命盤，而非蛋糕或其它非占星物件
            prompt_with_context = (
                f"{time_context}"
                f"【使用者上傳了一張圖片。這是一張西洋占星座星盤/本命盤（Natal Chart）圖。"
                f"請仔細識別星盤上的星座、行星、宮位與相位，並結合以下問題回答。】\n"
                f"問題：{user_input}"
            )
            response = chat.send_message([prompt_with_context, upload_image])
            # 修正記憶洩漏：回答後立即移除圖片記憶，避免後續文字對話重複傳送該圖片
            user_images.pop(user_id, None)
        else:
            prompt_with_context = f"{time_context}問題：{user_input}"
            response = chat.send_message(prompt_with_context)

        print(f"[Q] {user_input}")
        print(f"[A] {response.text}")
        return response.text

    except Exception as e:
        print(f"Gemini error: {e}")
        return "占星助理暫時出了點問題，請稍後再試 🌙"

# ── 定時推播占星知識邏輯 ───────────────────────────────────────────
def generate_astrology_knowledge():
    """使用 Gemini 生成一則約 200~250 字的占星知識"""
    try:
        push_model = genai.GenerativeModel(
            model_name="gemini-2.5-flash",
            safety_settings={
                HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
                HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
                HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
                HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
            },
            generation_config={
                "temperature": 0.85,
                "top_p": 0.95,
                "max_output_tokens": 1024,
            },
            system_instruction=(
                "你是一位專業且溫暖的西洋占星助理。請撰寫一則有趣、深入且好懂的占星知識分享。"
                "主題可以包含：星座神話、行星相位意涵、宮位解析小技巧或行運對日常生活的影響等。"
                "使用繁體中文。請控制字數在 200~250 字之間，不可超過 250 字。語氣要親切專業。"
            )
        )
        response = push_model.generate_content("請提供一則西洋占星知識分享，字數在 200 至 250 字之間。")
        return response.text.strip()
    except Exception as e:
        print(f"[Scheduler] Error generating astrology knowledge: {e}")
        return (
            "【早安！今日占星知識分享】🔮\n"
            "您知道嗎？在西洋占星中，月亮星座代表了我們潛意識的真實情感需求與安全感來源。"
            "例如，月亮在牡羊座的人，情緒來得快去得也快，需要直接且坦率的情感表達；"
            "而月亮在金牛座的人則渴望穩定與感官的舒適，常常透過美食或安靜的個人空間來療癒自我。"
            "理解自己的月亮星座，能幫助我們在面對壓力和負面情緒時，找到最適合自己的心理排解與自我照顧方式喔！✨"
        )

def push_to_groups(message):
    """將訊息推播到 group_ids.txt 中的所有群組"""
    if not os.path.exists("group_ids.txt"):
        print("[Scheduler] No group IDs file found. Skipping push.")
        return
    
    with open("group_ids.txt", "r", encoding="utf-8") as f:
        group_ids = [line.strip() for line in f if line.strip()]

    if not group_ids:
        print("[Scheduler] No group IDs saved. Skipping push.")
        return

    print(f"[Scheduler] Starting push to {len(group_ids)} groups...")
    failed_groups = []

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        for gid in group_ids:
            try:
                line_bot_api.push_message(
                    PushMessageRequest(
                        to=gid,
                        messages=[TextMessage(text=message)]
                    )
                )
                print(f"[Scheduler] Successfully pushed to {gid}")
            except Exception as e:
                print(f"[Scheduler] Failed to push to {gid}: {e}")
                # 判斷是否為無效/已退出的群組 ID
                if "Failed to send messages" in str(e) or "400" in str(e) or "404" in str(e):
                    failed_groups.append(gid)
    
    # 清除失效的群組 ID
    if failed_groups:
        try:
            current_ids = [g for g in group_ids if g not in failed_groups]
            with open("group_ids.txt", "w", encoding="utf-8") as f:
                for cid in current_ids:
                    f.write(f"{cid}\n")
            print(f"[Scheduler] Cleaned up {len(failed_groups)} invalid group IDs.")
        except Exception as e:
            print(f"[Scheduler] Error cleaning invalid group IDs: {e}")

def get_next_push_time(base_time, interval_days):
    """計算基於 base_time 加上指定天數後的早上 9 點"""
    target_date = base_time + timedelta(days=interval_days)
    return datetime.combine(target_date.date(), datetime_time(9, 0, 0))

def run_scheduler():
    """後台排程器主循環"""
    print("[Scheduler] Scheduler thread started.")
    STATE_FILE = "scheduler_state.json"
    
    while True:
        try:
            state = {}
            if os.path.exists(STATE_FILE):
                try:
                    with open(STATE_FILE, "r", encoding="utf-8") as f:
                        state = json.load(f)
                except Exception as je:
                    print(f"[Scheduler] Error loading state JSON: {je}")

            now = datetime.now()

            # 初始化狀態
            if not state or "next_push_time" not in state:
                today_9am = datetime.combine(now.date(), datetime_time(9, 0, 0))
                if now < today_9am:
                    next_push = today_9am
                else:
                    next_push = today_9am + timedelta(days=1)
                
                state = {
                    "last_push_time": None,
                    "next_interval": 2, # 交替模式：2 -> 3 -> 2 -> 3
                    "next_push_time": next_push.isoformat()
                }
                with open(STATE_FILE, "w", encoding="utf-8") as f:
                    json.dump(state, f, indent=4)
                print(f"[Scheduler] Initialized state. Next push: {next_push.isoformat()}")

            next_push_time = datetime.fromisoformat(state["next_push_time"])
            next_interval = state.get("next_interval", 2)

            # 判斷是否抵達或超過下次推播時間
            if now >= next_push_time:
                print(f"[Scheduler] Current time {now.isoformat()} >= Next push time {next_push_time.isoformat()}. Triggering push!")
                
                # 1. 產生並推送知識
                knowledge = generate_astrology_knowledge()
                push_to_groups(knowledge)

                # 2. 交替間隔天數並計算下一次時間
                # 交替頻率為：2天 -> 3天 -> 2天 -> 3天
                current_interval = next_interval
                new_next_interval = 3 if current_interval == 2 else 2
                
                # 計算下一次推送日期
                new_next_push_time = get_next_push_time(next_push_time, current_interval)
                
                # 更新狀態
                state["last_push_time"] = now.isoformat()
                state["next_interval"] = new_next_interval
                state["next_push_time"] = new_next_push_time.isoformat()
                
                with open(STATE_FILE, "w", encoding="utf-8") as f:
                    json.dump(state, f, indent=4)
                print(f"[Scheduler] Push finished. Next interval: {new_next_interval} days. Next push time: {new_next_push_time.isoformat()}")

        except Exception as e:
            print(f"[Scheduler] Error in scheduler loop: {e}")
        
        # 每分鐘檢查一次
        time.sleep(60)

def check_single_instance():
    """綁定本地特定連接埠，確保在多程序（如 Gunicorn worker）部署時只有一個實例在跑排程"""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(('127.0.0.1', 12999))
        check_single_instance.lock_socket = s
        return True
    except socket.error:
        return False

# ── 啟動後台排程器線程 ──────────────────────────────────────────────────
# 只有在主進程，或是被成功分配到 socket 鎖的 worker 進程才會啟動排程
if __name__ == "__main__" or check_single_instance():
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

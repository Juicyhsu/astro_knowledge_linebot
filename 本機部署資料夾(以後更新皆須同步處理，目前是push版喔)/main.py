import sys
import os
import re
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
load_dotenv()

from flatlib.datetime import Datetime
from flatlib.geopos import GeoPos
from flatlib.chart import Chart
from flatlib import const

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
    # 群組：沒被 @ 就不回應
    if event.source.type in ("group", "room"):
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

def get_current_astrology_context():
    """使用 flatlib 計算當下（即時）所有行星在黃道十二星座的精確位置"""
    try:
        now = datetime.now(timezone.utc)
        date_str = now.strftime('%Y/%m/%d')
        time_str = now.strftime('%H:%M')
        
        date = Datetime(date_str, time_str, '+00:00')
        pos = GeoPos('25n03', '121e30')  # 預設台北座標
        chart = Chart(date, pos, IDs=const.LIST_OBJECTS)
        
        planets = [
            (const.SUN, '太陽'),
            (const.MOON, '月亮'),
            (const.MERCURY, '水星'),
            (const.VENUS, '金星'),
            (const.MARS, '火星'),
            (const.JUPITER, '木星'),
            (const.SATURN, '土星'),
            (const.URANUS, '天王星'),
            (const.NEPTUNE, '海王星'),
            (const.PLUTO, '冥王星')
        ]
        
        sign_map = {
            'Aries': '牡羊座', 'Taurus': '金牛座', 'Gemini': '雙子座', 'Cancer': '巨蟹座',
            'Leo': '獅子座', 'Virgo': '處女座', 'Libra': '天秤座', 'Scorpio': '天蠍座',
            'Sagittarius': '射手座', 'Capricorn': '摩羯座', 'Aquarius': '水瓶座', 'Pisces': '雙魚座'
        }
        
        out = []
        for pid, name in planets:
            obj = chart.getObject(pid)
            if obj:
                sign_zh = sign_map.get(obj.sign, obj.sign)
                deg_in_sign = obj.lon % 30
                out.append(f"{name}在{sign_zh} ({deg_in_sign:.1f}°)")
                
        return "，".join(out)
    except Exception as e:
        print(f"Error calculating astrology context: {e}")
        return None

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
    astro_positions = get_current_astrology_context()
    
    time_context = f"【系統提示：當前的真實地球日期是 {now_str}。請以此作為「今天、現在、當下」來計算與分析流年行運，絕對不要搞錯年份！】\n"
    if astro_positions:
        time_context += f"【系統提供天文星曆（當前真實星體位置）：{astro_positions}。請以此作為目前天上的實時行運（Transit）星象數據來解讀星盤與回答問題！】\n"

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

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
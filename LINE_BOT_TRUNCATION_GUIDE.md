# LINE Bot 訊息截斷問題（50字截斷）關鍵修復參考手冊

本手冊記載了在建置 LINE Bot（尤其是對接 LLM 大型語言模型，如 Gemini、ChatGPT 等）時，可能會遇到的 **「訊息在特定長度（例如 50 字左右）被強制截斷、後面字數完全無法顯示」** 的詭異 Bug，以及其背後的成因與通用解決方案。

---

## 1. 問題現象 (The Issue)
在開發 LINE Bot 時，如果讓機器人主動推播（Push Message）或被動回覆（Reply Message）較長篇幅的文字，會發現有些訊息**在特定字數（最常見的是 50 字左右）後面被強行腰斬**，但後台日誌（Log）顯示生成的文字是完整的，且 LINE API 也回傳 `200 OK` 沒有報錯。

---

## 2. 錯誤成因 (The Cause)
### 關鍵：看不見的「Unicode 控制/格式字元」
LINE 的訊息解析器（Message Parser）在處理 API 傳入的 JSON 文本時，對某些 **Unicode 控制字元（Control Characters）** 或 **格式字元（Format Characters）** 非常敏感。如果文本中包含這些字元，LINE 的解析器會判定訊息結束，或直接在該處中斷解析，進而導致使用者手機端看到的訊息被截斷。

常見的隱形禍首字元包括：
1. **`\u200b` (Zero-Width Space, 零寬空格)**：這是最主要的元兇！LLM 在生成中文或處理排版時，常常會為了分詞或內部的 Token 邊界在文本中夾帶這個看不見的字元。
2. **其他 C 類別（Control/Format）字元**：如 `\u200e` (Left-to-Right Mark)、`\u0000`–`\u001f` 的控制碼。

當 LLM 生成的一段話在第 50 個字後面剛好夾帶了一個 `\u200b`，LINE Bot 就會在第 50 個字處被截斷。這就是為什麼「有時候 50 字被切斷，有時候又正常」的隨機截斷原因。

---

## 3. 通用修復方案 (The Solution)
在將任何字串傳送給 LINE API 之前，**必須強制對字串進行過濾與清洗**，將所有的 Unicode `C` 類別（控制/格式字元）全部剔除。

### 注意事項：保留 Emoji 的相容性
直接過濾所有 `C` 類別字元可能會破壞一些複雜的表情符號（Emoji）。例如：
* `\u200d` (Zero-Width Joiner, ZWJ)：用於組合複合型 Emoji（如 👨‍👩‍👧‍👦 家族、🏃‍♀️ 運動女性）。
* `\u200c` (Zero-Width Non-Joiner, ZWNJ)。

因此，過濾函數必須**特別保留 `\u200c` 與 `\u200d`**，並同時放行換行符號 `\n`。

---

## 4. 通用 Python 修復程式碼 (Code Snippet)
您可以直接將以下 Python 函數複製到您未來的任何 LINE Bot 專案中：

```python
import unicodedata

def clean_text_for_line(text: str) -> str:
    """
    清洗文本，移除非必要的 Unicode 控制/格式字元，防止 LINE 訊息截斷 Bug。
    同時保留換行符號 (\n) 與 Emoji 渲染所需的 ZWNJ (\\u200c) / ZWJ (\\u200d)。
    """
    if not text:
        return ""
        
    cleaned = []
    for ch in text:
        # 1. 允許換行符號
        if ch == '\n':
            cleaned.append(ch)
            continue
        # 2. 忽略 Carriage Return
        elif ch == '\r':
            continue
            
        # 3. 檢查 Unicode 類別
        category = unicodedata.category(ch)
        if category.startswith('C'):
            # C 類別包含 Cc (Control), Cf (Format), Cs (Surrogate), Co (Private Use), Cn (Unassigned)
            # 必須特別保留 Emoji 的黏合字元 ZWNJ (\u200c) 與 ZWJ (\u200d)
            if ch in ('\u200c', '\u200d'):
                cleaned.append(ch)
            else:
                # 排除其他所有看不見的控制字元 (例如 \u200b)
                continue
        else:
            cleaned.append(ch)
            
    return "".join(cleaned).strip()
```

### 使用方式：
```python
# 1. LLM 生成原始回覆
llm_reply = response.text

# 2. 通過過濾器清洗
safe_reply = clean_text_for_line(llm_reply)

# 3. 發送給 LINE API
line_bot_api.reply_message(
    ReplyMessageRequest(
        reply_token=event.reply_token,
        messages=[TextMessage(text=safe_reply)]
    )
)
```

---

## 5. 開發建議與最佳實踐
1. **一律過濾**：不管是 Gemini、OpenAI 還是 Claude，只要是由 AI 生成的文字，在丟給 LINE 發送前**一律先過濾**。
2. **推播 (Push) 與回覆 (Reply) 都要處理**：推播與回覆呼叫的是不同的 API 端點，但後端的解析器是同一個，所以兩邊的訊息傳送點都必須包上 `clean_text_for_line`。
3. **字數保底**：LINE 單一文字訊息（TextMessage）的上限是 5000 字，但若發生隨機截斷，通常是這類隱形字元引起，而非真正超出字數限制。
4. **函數定義順序：必須放在所有呼叫點之前**（⚠️ 進階陷阱，見下方說明）。

---

## 6. ⚠️ 進階陷阱：函數定義順序與 Gunicorn 多進程 (Function Definition Order)

### 問題描述
即使正確實作了 `clean_text_for_line`，如果**函數的定義位置排在呼叫它的函數之後**，在 Gunicorn 多進程部署環境（如 Zeabur、Heroku、Railway）下，截斷問題仍然可能**間歇性復發**。

### 範例：有問題的排列方式（❌ 錯誤）
```python
# ❌ 錯誤：gemini_chat 在第 338 行呼叫 clean_text_for_line
def gemini_chat(user_input, user_id):
    ...
    return clean_text_for_line(response.text)  # 在這裡呼叫

# ❌ 錯誤：但 clean_text_for_line 的定義卻在第 401 行，排在後面！
def clean_text_for_line(text):
    ...
```

### 為什麼有問題？
- Python 本身在一般執行下沒問題（函數是執行期才查找的）。
- 但在 Gunicorn 啟動時，多個 worker 進程會並行進行模組 import。若某個 worker 的載入時序剛好在 `clean_text_for_line` 定義完成前就觸發了呼叫（例如在處理一個啟動時的健康檢查請求），就會找不到這個函數，靜默失敗，回傳**未清洗的原始文字**，截斷就又出現了。
- 這也解釋了為何截斷問題「時好時壞」——因為它取決於 Gunicorn worker 的啟動時序，是**隨機性的 race condition（競態條件）**。

### 正確作法（✅ 確保萬無一失）
**將 `clean_text_for_line` 定義移到程式碼最頂端，緊接在 `import` 之後，所有其他函數之前：**

```python
import unicodedata
from linebot.v3.messaging import ...  # 所有 import 完成後

# ✅ 正確：在所有函數定義之前，先定義這個工具函數
def clean_text_for_line(text: str) -> str:
    """Remove invisible control characters that cause LINE message truncation."""
    cleaned = []
    for ch in text:
        if ch == '\n':
            cleaned.append(ch)
            continue
        elif ch == '\r':
            continue
        category = unicodedata.category(ch)
        if category.startswith('C'):
            if ch in ('\u200c', '\u200d'):
                cleaned.append(ch)
            else:
                continue
        else:
            cleaned.append(ch)
    return "".join(cleaned).strip()

# 然後才是其他函數定義...
def gemini_chat(...):
    ...
    return clean_text_for_line(response.text)  # 此時一定已經定義好了 ✅
```

### 重點提醒
> 「先寫後用」是最安全的原則。`clean_text_for_line` 應視為整個 LINE Bot 最底層的基礎工具函數，永遠排在第一個被定義。

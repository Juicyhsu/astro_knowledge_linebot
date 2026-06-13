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

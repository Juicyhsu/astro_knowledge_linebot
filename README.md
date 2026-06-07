# 占星知識機器人 🌙 (Astro Knowledge Linebot)

一個基於 LINE Messaging API 與 Google Gemini 2.5 Flash 的專業西洋占星對話機器人。

## 功能特色
- 🔮 **專業西洋占星對話**：專長於十二星座、本命盤解讀、行星相位、行運推運等。
- 🗺️ **星盤圖片分析**：支持上傳星盤圖片，結合文字提問，提供精確的占星建議。
- 💬 **上下文記憶**：具備對話記憶功能（時效為 7 天），支援關鍵字清除記憶（如「重新開始」、「重置對話」）。
- 👥 **群組 @ 提及響應**：在群組中，只有在被 @ 提及時才會觸發回覆，避免干擾日常對話。
- 🛡️ **安全防護**：敏感資訊與金鑰儲存於環境變數中，避免上傳至公開代碼庫。

---

## 快速開始

### 1. 取得專案
請先將專案 Clone 到您的本機環境：
```bash
git clone https://github.com/Juicyhsu/astro_knowledge_linebot.git
cd astro_knowledge_linebot
```

### 2. 安裝依賴套件
建議在虛擬環境中安裝所需的 Python 套件：
```bash
pip install -r requirements.txt
```

### 3. 設定環境變數 (`.env`)
專案根目錄下已包含 `.env` 檔案（此檔案已被加入 `.gitignore`，不會上傳到 GitHub）。請在其中設定您的 API 金鑰：

- **LINE_CHANNEL_SECRET**: LINE Developers 主控台中的 Channel Secret。
- **LINE_CHANNEL_ACCESS_TOKEN**: LINE Developers 主控台中的 Channel Access Token。
- **GEMINI_API_KEY**: 從 [Google AI Studio](https://aistudio.google.com/) 申請的 Gemini API Key。

`.env` 內容格式如下：
```env
LINE_CHANNEL_SECRET=1ea33f072142830141baddee692da7ab
LINE_CHANNEL_ACCESS_TOKEN=GBjZ+OKnVNl...
GEMINI_API_KEY=您的_GEMINI_API_KEY
PORT=5000
```

### 4. 啟動本機伺服器
執行以下指令來啟動 Flask 伺服器：
```bash
python main.py
```
伺服器將在 `http://localhost:5000` 啟動。

---

## 本機測試與 Webhook 設定

由於 LINE Webhook 需要使用 HTTPS 網址，您可以使用 `ngrok` 等穿透工具將本機服務公開至公網：

1. 安裝並啟動 `ngrok`（對應您的 Flask 埠號）：
   ```bash
   ngrok http 5000
   ```
2. 複製 `ngrok` 產生的 HTTPS 轉發網址（例如：`https://xxxx.ngrok-free.app`）。
3. 前往 **LINE Developers Console** -> **Messaging API settings**：
   - 將 Webhook URL 設定為：`https://xxxx.ngrok-free.app/callback`
   - 點擊 **Verify** 測試連線是否成功。
   - 開啟 **Use webhook** 功能。

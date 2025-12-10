import os
import time
import threading
import requests

from flask import Flask, request, abort

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

# 從環境變數讀取 LINE 設定（部署到雲端時在平台上設定）
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")

if not CHANNEL_ACCESS_TOKEN or not CHANNEL_SECRET:
    print("請先設定環境變數 LINE_CHANNEL_ACCESS_TOKEN 與 LINE_CHANNEL_SECRET")
else:
    print("LINE 設定已載入")

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

app = Flask(__name__)

# 監控結構：
# watches = {
#   "user_id": {
#       "2330": {
#           "base_price": 150.0,
#           "up_threshold": 157.5,   # base_price * 1.05
#           "down_threshold": 142.5  # base_price * 0.95
#       },
#       "2603": {...}
#   },
#   ...
# }
watches = {}


def get_tw_stock_price(stock_id: str):
    """
    從證交所 MIS API 取得即時股價
    stock_id: "2330" 這種四碼股票代號
    回傳: float 價格 或 None 代表失敗
    """
    try:
        url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_{stock_id}.tw"
        resp = requests.get(url, timeout=3)
        data = resp.json()

        msgArray = data.get("msgArray", [])
        if not msgArray:
            return None

        price_str = msgArray[0].get("z")  # 最新成交價
        if price_str is None or price_str == "-" or price_str == "0":
            return None

        return float(price_str)
    except Exception as e:
        print(f"[get_tw_stock_price] Error: {e}")
        return None


def alert_loop():
    """
    背景輪詢所有使用者監控的股票，
    當股價相對 base_price 漲/跌超過 5% 時發送提示，並重置 base_price。
    """
    while True:
        try:
            # 複製一份避免迭代時被修改
            current_watches = dict(watches)

            for user_id, stocks in current_watches.items():
                for stock_id, info in stocks.items():
                    price = get_tw_stock_price(stock_id)
                    if price is None:
                        print(f"無法取得 {stock_id} 的價格")
                        continue

                    base_price = info.get("base_price")
                    up_threshold = info.get("up_threshold")
                    down_threshold = info.get("down_threshold")

                    if base_price is None:
                        # 理論上不會發生，但防呆
                        watches[user_id][stock_id]["base_price"] = price
                        watches[user_id][stock_id]["up_threshold"] = price * 1.05
                        watches[user_id][stock_id]["down_threshold"] = price * 0.95
                        continue

                    triggered = False
                    direction = None

                    # 上漲 >= 5%
                    if price >= up_threshold:
                        triggered = True
                        direction = "up"
                    # 下跌 <= -5%
                    elif price <= down_threshold:
                        triggered = True
                        direction = "down"

                    if triggered:
                        if direction == "up":
                            text = (
                                f"{stock_id} 現價 {price:.2f}，"
                                f"相較基準價 {base_price:.2f} 已上漲超過 5%。\n"
                                f"已為你重置新的基準價。"
                            )
                        else:
                            text = (
                                f"{stock_id} 現價 {price:.2f}，"
                                f"相較基準價 {base_price:.2f} 已下跌超過 5%。\n"
                                f"已為你重置新的基準價。"
                            )

                        try:
                            line_bot_api.push_message(user_id, TextSendMessage(text))
                        except Exception as e:
                            print(f"[push_message error] user={user_id}, stock={stock_id}, err={e}")

                        # 重置基準價與下一個 5% 門檻
                        watches[user_id][stock_id]["base_price"] = price
                        watches[user_id][stock_id]["up_threshold"] = price * 1.05
                        watches[user_id][stock_id]["down_threshold"] = price * 0.95

        except Exception as e:
            print(f"[alert_loop] Error: {e}")

        # 每 10 秒檢查一次
        time.sleep(10)


# 啟動背景 Thread
threading.Thread(target=alert_loop, daemon=True).start()


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        print("Invalid signature. Check your channel access token/channel secret.")
        abort(400)

    return "OK"


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event: MessageEvent):
    """
    支援指令：
    1) 新增股票監控（每 ±5% 提醒）：
       2330
    2) 刪除股票：
       del 2330
       刪除 2330
    3) 查看列表：
       list
    4) 說明：
       help / 說明
    """
    user_id = event.source.user_id
    text = event.message.text.strip()

    # help / 說明
    if text.lower() in ["help", "說明"]:
        reply = (
            "台股 5% 變動提醒使用說明：\n"
            "1) 新增監控股票（每漲跌 5% 提醒一次）：\n"
            "   2330\n"
            "2) 刪除某檔股票監控：\n"
            "   del 2330  或  刪除 2330\n"
            "3) 查看目前監控列表：\n"
            "   list\n"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(reply))
        return

    # list：列出目前監控
    if text.lower() == "list":
        user_watches = watches.get(user_id, {})
        if not user_watches:
            reply = "你目前沒有任何正在監控的股票。\n直接輸入四碼股票代號即可開始監控，例如：2330"
        else:
            lines = ["你目前監控中的股票（每 ±5% 提醒）："]
            for stock_id, info in user_watches.items():
                base = info.get("base_price", 0.0)
                lines.append(f"  - {stock_id}（目前基準價：約 {base:.2f}）")
            reply = "\n".join(lines)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(reply))
        return

    # 刪除：del 2330 / 刪除 2330
    lower_text = text.lower().replace("　", " ").strip()
    if lower_text.startswith("del ") or lower_text.startswith("刪除 "):
        parts = lower_text.split()
        if len(parts) >= 2:
            stock_id = parts[1]
            user_watches = watches.get(user_id, {})
            if stock_id in user_watches:
                del user_watches[stock_id]
                reply = f"已停止監控 {stock_id}。"
            else:
                reply = f"你目前沒有監控 {stock_id}。"
        else:
            reply = "請輸入：del 2330 或 刪除 2330"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(reply))
        return

    # 若是純數字（例如 2330），視為新增監控
    if text.isdigit():
        stock_id = text

        price = get_tw_stock_price(stock_id)
        if price is None:
            reply = f"無法取得 {stock_id} 的即時股價，請確認股票代號是否正確且在上市市場。"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(reply))
            return

        if user_id not in watches:
            watches[user_id] = {}

        watches[user_id][stock_id] = {
            "base_price": price,
            "up_threshold": price * 1.05,
            "down_threshold": price * 0.95,
        }

        reply = (
            f"已開始監控 {stock_id}。\n"
            f"當股價相對目前基準價 {price:.2f} 每漲或跌超過 5% 時會提醒你一次，"
            f"並自動重置新的基準價。"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(reply))
        return

    # 其他無法解析的文字
    reply = (
        "無法解析你的指令。\n"
        "你可以：\n"
        "1) 直接輸入四碼股票代號開始監控，例如：2330\n"
        "2) 輸入：list  查看目前監控列表\n"
        "3) 輸入：del 2330  或  刪除 2330  取消監控\n"
        "4) 輸入：help  看完整說明"
    )
    line_bot_api.reply_message(event.reply_token, TextSendMessage(reply))


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

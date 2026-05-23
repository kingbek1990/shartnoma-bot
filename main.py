import logging
import os
import json
import re
import base64
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

import anthropic
import gspread
from google.oauth2.service_account import Credentials

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")

# === SUMMA SO'Z BILAN ===
def summa_soz_bilan(n):
    try:
        n = int(float(str(n).replace(" ", "").replace(",", "")))
    except:
        return ""
    if n == 0:
        return "nol so'm"
    birliklar = ["", "bir", "ikki", "uch", "to'rt", "besh", "olti", "yetti", "sakkiz", "to'qqiz"]
    o_nlar = ["", "o'n", "yigirma", "o'ttiz", "qirq", "ellik", "oltmish", "yetmish", "sakson", "to'qson"]
    def uch_xona(num):
        result = ""
        if num >= 100:
            result += birliklar[num // 100] + " yuz "
            num %= 100
        if num >= 10:
            result += o_nlar[num // 10] + " "
            num %= 10
        if num > 0:
            result += birliklar[num] + " "
        return result.strip()
    darajalar = [(10**12, "trillion"), (10**9, "milliard"), (10**6, "million"), (10**3, "ming")]
    result = ""
    for daraja, nom in darajalar:
        if n >= daraja:
            result += uch_xona(n // daraja) + " " + nom + " "
            n %= daraja
    if n > 0:
        result += uch_xona(n)
    return result.strip() + " so'm"

# === TELEGRAM FOYDALANUVCHI ISMI ===
def get_user_name(update: Update):
    user = update.message.from_user
    if user.first_name and user.last_name:
        return f"{user.first_name} {user.last_name}"
    elif user.first_name:
        return user.first_name
    elif user.username:
        return f"@{user.username}"
    else:
        return f"ID:{user.id}"

# === GOOGLE SHEETS ===
def get_sheet():
    creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(GOOGLE_SHEET_ID).sheet1

def setup_headers(sheet):
    headers = sheet.row_values(1)
    if not headers or headers[0] == "":
        sheet.update("A1:K1", [["№", "Birja turi", "Shartnoma №", "Sana",
            "Yetkazuvchi nomi", "Tovar nomi", "O'lchov birligi",
            "Miqdori", "Shartnoma summasi", "Summa so'z bilan", "Yuklovchi"]])
        sheet.format("A1:K1", {"textFormat": {"bold": True}})

def get_next_row(sheet):
    values = sheet.col_values(1)
    last_num = 0
    for v in values[1:]:
        try:
            last_num = int(v)
        except:
            pass
    return len(values) + 1, last_num + 1

# === AI TAHLIL ===
def analyze_contract(pdf_bytes):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    pdf_base64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")
    prompt = """Bu shartnoma PDFidan quyidagi ma'lumotlarni aniq topib ber. 
Faqat JSON formatda javob ber, hech qanday qo'shimcha matn yozma.

{"shartnoma_raqami":"","sana":"DD.MM.YYYY","sotuvchi_nomi":"","tovar_nomi":"","olchov_birligi":"","miqdor":"","summa":"","birja_turi":""}

birja_turi qiymatlari:
- "давлат харидлари электрон тизими" yoki "xt-xarid" -> "Хт-Харид"
- "Электрон дўкон" yoki "uz-ex" -> "Уз-Экс"
- "УзРТСБ" rus tilida -> "СПОТ"
- "O'zRTXB" o'zbek tilida -> "НЮ СПОТ"
- "кооперация" yoki "kooperatsiya" -> "Кооперацион"
- boshqa -> "Тугридан-тугри"

summa va miqdor: faqat raqam, hech qanday belgi yo'q."""

    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=500,
        messages=[{"role": "user", "content": [
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_base64}},
            {"type": "text", "text": prompt}
        ]}],
    )
    text = response.content[0].text.strip()
    text = re.sub(r'```[a-z]*\s*', '', text).strip()
    return json.loads(text)

# === SHEETGA YOZISH ===
def write_to_sheet(sheet, data, row_num, serial_num, yuklovchi):
    try:
        summa = int(float(str(data.get("summa", "0")).replace(" ", "").replace(",", "")))
    except:
        summa = 0
    try:
        miqdor = float(str(data.get("miqdor", "0")).replace(" ", "").replace(",", ""))
        if miqdor == int(miqdor):
            miqdor = int(miqdor)
    except:
        miqdor = 0
    soz_bilan = summa_soz_bilan(summa)
    sheet.update(f"A{row_num}:K{row_num}", [[
        serial_num, data.get("birja_turi", ""), data.get("shartnoma_raqami", ""),
        data.get("sana", ""), data.get("sotuvchi_nomi", ""), data.get("tovar_nomi", ""),
        data.get("olchov_birligi", ""), miqdor, summa, soz_bilan, yuklovchi
    ]])

# === HANDLERS ===
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    document = message.document
    if not document.file_name.lower().endswith('.pdf'):
        await message.reply_text("❌ Faqat PDF formatdagi shartnomalar qabul qilinadi.")
        return
    await message.reply_text("⏳ Shartnoma tahlil qilinmoqda...")
    try:
        yuklovchi = get_user_name(update)
        file = await context.bot.get_file(document.file_id)
        pdf_bytes = await file.download_as_bytearray()
        data = analyze_contract(bytes(pdf_bytes))
        sheet = get_sheet()
        setup_headers(sheet)
        next_row, serial_num = get_next_row(sheet)
        write_to_sheet(sheet, data, next_row, serial_num, yuklovchi)
        try:
            summa = int(float(str(data.get("summa", "0")).replace(",", "")))
            summa_fmt = "{:,}".format(summa).replace(",", " ")
        except:
            summa_fmt = data.get("summa", "")
        soz = summa_soz_bilan(data.get("summa", 0))
        text = (f"✅ *Shartnoma qo'shildi! ({serial_num}-qator)*\n\n"
                f"🏪 *Birja:* {data.get('birja_turi', '-')}\n"
                f"📄 *Shartnoma №:* {data.get('shartnoma_raqami', '-')}\n"
                f"📅 *Sana:* {data.get('sana', '-')}\n"
                f"🏢 *Yetkazuvchi:* {data.get('sotuvchi_nomi', '-')}\n"
                f"📦 *Tovar:* {data.get('tovar_nomi', '-')}\n"
                f"⚖️ *O'lchov:* {data.get('olchov_birligi', '-')}\n"
                f"🔢 *Miqdor:* {data.get('miqdor', '-')}\n"
                f"💰 *Summa:* {summa_fmt} so'm\n"
                f"📝 *So'z bilan:* {soz}\n"
                f"👤 *Yuklovchi:* {yuklovchi}\n\n"
                f"📊 Google Sheetga yozildi!")
        await message.reply_text(text, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Error: {e}")
        await message.reply_text(f"❌ Xatolik: {str(e)}")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    yuklovchi = get_user_name(update)
    await update.message.reply_text(
        f"👋 Salom, {yuklovchi}!\n\n"
        "📄 Shartnoma PDF faylini yuboring — avtomatik Google Sheetga yozaman.\n\n"
        "✅ Birjalar: Хт-Харид, Уз-Экс, СПОТ, НЮСПОТ, Кооперацион, Тугридан-тугри"
    )

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT, handle_text))
    logger.info("Bot ishga tushdi!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

import logging
import os
import json
import re
from datetime import datetime
import anthropic
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === SOZLAMALAR ===
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")

# === GOOGLE SHEETS ULANISH ===
def get_sheet():
    creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(GOOGLE_SHEET_ID).sheet1
    return sheet

# === KEYINGI BO'SH QATOR TOPISH ===
def get_next_row(sheet):
    values = sheet.col_values(1)
    # Raqamli qatorlarni top
    last_num = 0
    for v in values:
        try:
            last_num = int(v)
        except:
            pass
    return len(values) + 1, last_num + 1

# === BIRJA TURINI ANIQLASH ===
def detect_birja(text):
    text_lower = text.lower()
    if "xt-xarid" in text_lower or "хт-харид" in text_lower or "давлат харидлари электрон тизимида" in text_lower:
        return "Хт-Харид"
    elif "uz-ex" in text_lower or "уз-экс" in text_lower or "uz-eks" in text_lower:
        return "Уз-Экс"
    elif "o'zrtxb" in text_lower or "узртхб" in text_lower or "узртсб" in text_lower or "uzrtxb" in text_lower:
        return "НЮ СПОТ"
    elif "узрТСБ" in text or "uzrtсб" in text_lower or "биржевых торгов" in text_lower:
        return "СПОТ"
    elif "кооперация" in text_lower or "kooperatsiya" in text_lower or "ny" in text_lower and "1000" in text:
        return "Кооперацион"
    else:
        return "Прямой"

# === AI ORQALI SHARTNOMA TAHLIL QILISH ===
def analyze_contract(pdf_bytes, filename):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    
    import base64
    pdf_base64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")
    
    prompt = """Bu shartnoma PDFidan quyidagi ma'lumotlarni aniq topib ber. 
Faqat JSON formatda javo ber, boshqa hech narsa yozma.

{
  "shartnoma_raqami": "shartnoma raqami (masalan: 7437970.2.1 yoki 4475028 yoki 8469431)",
  "sana": "shartnoma sanasi DD.MM.YYYY formatda",
  "sotuvchi_nomi": "ijrochi yoki sotuvchi tashkilot nomi",
  "tovar_nomi": "tovar yoki xizmat nomi (qisqa, asosiy)",
  "olchov_birligi": "o'lchov birligi (dona, tonna, m3, kg, xizmat va h.k.)",
  "miqdor": "tovar miqdori (faqat son)",
  "summa": "shartnoma umumiy summasi (faqat son, so'm belgisisiz)",
  "birja_turi": "birja turi: Хт-Харид, Уз-Экс, СПОТ, НЮ СПОТ, Кооперацион, yoki Прямой"
}

Muhim qoidalar:
- Sanani DD.MM.YYYY formatda yoz (masalan: 22.05.2026)
- Summada faqat raqam bo'lsin (masalan: 30250000)
- Miqdorda faqat raqam bo'lsin (masalan: 10)
- Agar biror ma'lumot topilmasa, "" (bo'sh) qoldir"""

    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1000,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": pdf_base64,
                        },
                    },
                    {
                        "type": "text",
                        "text": prompt
                    }
                ],
            }
        ],
    )
    
    response_text = response.content[0].text.strip()
    # JSON tozalash
    response_text = re.sub(r'```json\s*', '', response_text)
    response_text = re.sub(r'```\s*', '', response_text)
    
    data = json.loads(response_text)
    return data

# === GOOGLE SHEETGA YOZISH ===
def write_to_sheet(sheet, data, row_num, serial_num):
    # Summa formatlash
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

    row_data = [
        serial_num,                          # A: №
        data.get("birja_turi", ""),          # B: Вид торга
        data.get("shartnoma_raqami", ""),    # C: № договора
        data.get("sana", ""),                # D: Дата
        data.get("sotuvchi_nomi", ""),       # E: Наименование Поставщика
        data.get("tovar_nomi", ""),          # F: Наименование товара
        data.get("olchov_birligi", ""),      # G: Ед.изм.
        miqdor,                              # H: Количество
        summa,                               # I: Сумма договора
    ]
    
    sheet.update(f"A{row_num}:I{row_num}", [row_data])

# === TELEGRAM BOT HANDLERI ===
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    document = message.document
    
    # Faqat PDF qabul qilish
    if not document.file_name.lower().endswith('.pdf'):
        await message.reply_text("❌ Faqat PDF formatdagi shartnomalar qabul qilinadi.")
        return
    
    await message.reply_text("⏳ Shartnoma tahlil qilinmoqda, biroz kuting...")
    
    try:
        # PDF yuklab olish
        file = await context.bot.get_file(document.file_id)
        pdf_bytes = await file.download_as_bytearray()
        
        # AI tahlil
        data = analyze_contract(bytes(pdf_bytes), document.file_name)
        
        # Google Sheetga yozish
        sheet = get_sheet()
        next_row, serial_num = get_next_row(sheet)
        write_to_sheet(sheet, data, next_row, serial_num)
        
        # Summa formatlash (ko'rsatish uchun)
        try:
            summa_fmt = "{:,.0f}".format(float(str(data.get("summa","0")).replace(",",""))).replace(",", " ")
        except:
            summa_fmt = data.get("summa", "")
        
        # Muvaffaqiyat xabari
        response_text = f"""✅ *Shartnoma muvaffaqiyatli qo'shildi!*

📋 *{serial_num}-qator ma'lumotlari:*
━━━━━━━━━━━━━━━━━━━━
🏪 *Birja:* {data.get('birja_turi', '-')}
📄 *Shartnoma №:* {data.get('shartnoma_raqami', '-')}
📅 *Sana:* {data.get('sana', '-')}
🏢 *Yetkazuvchi:* {data.get('sotuvchi_nomi', '-')}
📦 *Tovar:* {data.get('tovar_nomi', '-')}
⚖️ *O'lchov:* {data.get('olchov_birligi', '-')}
🔢 *Miqdor:* {data.get('miqdor', '-')}
💰 *Summa:* {summa_fmt} so'm
━━━━━━━━━━━━━━━━━━━━
📊 Google Sheetga yozildi!"""
        
        await message.reply_text(response_text, parse_mode='Markdown')
        
    except json.JSONDecodeError as e:
        logger.error(f"JSON parse error: {e}")
        await message.reply_text("❌ Shartnomadan ma'lumot olishda xatolik. Iltimos qaytadan yuboring.")
    except Exception as e:
        logger.error(f"Error: {e}")
        await message.reply_text(f"❌ Xatolik yuz berdi: {str(e)}")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Salom! Men shartnoma botiman.\n\n"
        "📄 Shartnoma PDF faylini yuboring — men avtomatik Google Sheetga yozaman.\n\n"
        "✅ Quyidagi birjalar qo'llab-quvvatlanadi:\n"
        "• Хт-Харид\n"
        "• Уз-Экс\n"
        "• СПОТ\n"
        "• НЮ СПОТ\n"
        "• Кооперацион\n"
        "• Прямой (to'g'ridan-to'g'ri)"
    )

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT, handle_text))
    
    logger.info("Bot ishga tushdi!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

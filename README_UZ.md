# VidiPay — ishga tushirish

Bu boshlang‘ich loyiha siz yuborgan `bot.py` backend asosida tayyorlandi. Mini App interfeysi `webapp/index.html` ichida.

## Render sozlamalari
1. Loyihani GitHub repository'ga yuklang.
2. Render'da **New > Web Service** tanlang va repository'ni ulang.
3. Build command: `pip install -r requirements.txt`
4. Start command: `python bot.py`
5. Environment variables:
   - `BOT_TOKEN` — BotFather bergan token
   - `ADMIN_IDS` — admin Telegram ID raqami
   - `WEBAPP_URL` — Render'dagi servis URL'i, masalan `https://vidipay.onrender.com`
   - qolgan sozlamalar `.env.example` dagi qiymatlar bilan mos.
6. Deploy qiling. BotFather'da Mini App domenini sozlash talab qilinishi mumkin.

## Admin buyruqlari
- Botga video fayl yuboring (caption: `Sarlavha | 60`) — oddiy rejim uchun.
- `/addvideo 60 https://example.com/video.mp4 Sarlavha` — URL orqali video.
- `/videos`, `/delvideo ID`, `/stats`
- Foydalanuvchi: `/balance`, `/withdraw 5 HAMYON`

## Muhim
- Bu versiyada yechish so‘rovlari admin tomonidan qo‘lda tasdiqlanadi; avtomatik to‘lov provayderi ulanmagan.
- Daromad va bonuslar haqiqiy mablag‘ kafolati emas. To‘lovlarni yoqishdan oldin biznes modeli, video huquqlari, foydalanuvchi shartlari va moliyaviy qonuniy talablarni tekshiring.
- Hozirgi backenddagi SQLite bazasi Render'ning vaqtinchalik diskida saqlanishi mumkin; doimiy ma'lumot uchun persistent disk yoki PostgreSQL sozlash kerak.
- Mini App heartbeat mexanizmi faqat ochiq sahifadagi server so‘rovlariga qarab vaqt hisoblaydi; video haqiqatan ko‘rilganini to‘liq isbotlamaydi. Ishga tushirishdan oldin xavfsizlik auditi tavsiya qilinadi.

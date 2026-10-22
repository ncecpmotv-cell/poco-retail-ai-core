# 📦 poco-retail-ai-core — نظام تحليلات التجزئة بالذكاء الاصطناعي على حافة الشبكة

> النسخة العربية من الدليل. [English version](README.md)

**حوّل هاتف أندرويد اقتصادي (POCO F3 بمعالج Snapdragon 870) إلى صندوق ذكاء اصطناعي محلي يحلّل كاميرات المراقبة (CCTV) داخل المتجر — بتكلفة سحابية صفرية.**

يعمل النظام داخل Termux على الهاتف نفسه: يلتقط لقطات من كاميرات RTSP بالتتابع، ويشغّل نموذج **MiniCPM-V** عبر **llama.cpp** (استدلال محلي 100%)، ويخزّن كل ملاحظة في قاعدة **SQLite** محلية، ويحسب مؤشرات البيع بالتجزئة (Footfall، ساعات الذروة، فجوات التوظيف، مؤشر الازدحام)، ثم ينشر الحالة عبر **قناة تيليجرام خاصة** يقرأها تطبيق اللوحة المصاحب
[`telegram-retail-ai-dashboard`](https://github.com/ncecpmotv-cell/telegram-retail-ai-dashboard).

---

## 🏗️ المعمارية

```
┌─────────────────────── الهاتف (Termux) ───────────────────────┐
│ كاميرا 1 ─┐                                                    │
│ كاميرا 2 ─┼─► stream_manager (تتابعي، BUFFERSIZE=1) ── لقطة ─┐ │
│ كاميرا N ─┘                                                  │ │
│                                                              ▼ │
│                 inference_engine ── llama-mtmd-cli             │
│                       │   (برنامج C++ محلي)                    │
│                       ▼                                        │
│            analytics/db_handler ──► SQLite (وضع WAL)          │
│                       │                                        │
│                       ▼                                        │
│            analytics/kpi_engine ──► المؤشرات الأربعة           │
│                       │                                        │
│                       ▼                                        │
│            cloud_sync ── sendMessage + pinChatMessage ───────┐ │
└───────────────────────────────────────────────────────────────┼─┘
                                                                │
                     ┌──────────────────────────────────────────▼──┐
                     │   قناة تيليجرام الخاصة (ناقل مجاني)          │
                     │   حالة مثبّتة ◄── getChat ── اللوحة          │
                     │   أوامر set_config / set_subscription ──►     │
                     └─────────────────────────────────────────────┘
```

### حلقة العمل (افتراضيًا كل 60 ثانية)

```
مزامنة الأوامر (getUpdates) ← جدار الاشتراك ← التقاط كل كاميرا ←
استدلال MiniCPM-V ← حفظ في SQLite ← حساب المؤشرات كل ساعة ← نشر الحالة وتثبيتها
```

---

## 📋 المتطلبات

| العنصر | المتطلب |
|---|---|
| الهاتف | أندرويد 10+، ذاكرة 6GB+ (المُختبر: POCO F3 / SD870) |
| التطبيقات | [Termux](https://f-droid.org/packages/com.termux/) (نسخة F-Droid)، [Termux:API](https://f-droid.org/packages/com.termux.api/) |
| المساحة | ~5GB (أدوات البناء + llama.cpp + النموذج ~1.8GB) |
| تيليجرام | توكن بوت من [@BotFather](https://t.me/BotFather) |
| الشبكة | وصول لكاميرات RTSP + إنترنت لمزامنة تيليجرام |

---

## 🚀 التثبيت خطوة بخطوة

### 1) تجهيز Termux

```bash
termux-setup-storage
pkg update -y
```

### 2) استنساخ المستودع وتشغيل الإعداد

```bash
git clone https://github.com/ncecpmotv-cell/poco-retail-ai-core.git
cd poco-retail-ai-core
chmod +x setup.sh && ./setup.sh
```

`setup.sh` (قابل لإعادة التشغيل بأمان) يقوم بـ:

1. تثبيت الحزم: `git cmake clang make python opencv python-numpy libavif libheif termux-api termux-services`
2. محاولة إنشاء ملف swap بسعة 2GB (يتطلب روت؛ بدونه يتخطاه مع تنبيه — هاتفك يعمل بدونه كما أثبتنا)
3. بيئة Python افتراضية مع `--system-site-packages` (لأن cv2 يأتي من حزمة النظام)
4. بناء **llama.cpp** بتحسينات المعالج (`armv8.2a+dotprod`) — الهدف `llama-mtmd-cli`
5. تنزيل النموذجين: `MiniCPM-V-4_7-1B-Q4_K_M.gguf` + `mmproj-model-f16.gguf`
   (نصيحة توفير الذاكرة: استخدم `MiniCPM-V-4.7-1B.mmproj-Q8_0.gguf` من مستودع `mradermacher/MiniCPM-V-4.7-1B-GGUF`)
6. إنشاء `config.env` + تثبيت خدمة termux-services + قفل الاستيقاظ

### 3) الإعدادات — `config.env`

```ini
BOT_TOKEN=توكن_البوت
STATE_CHANNEL_ID=@MyStoreRetailAI
ADMIN_ID=معرف_المشرف
USER_ID=معرف_المالك

STORE_CAPACITY=50
STAFFING_THRESHOLD=15
LOOP_INTERVAL_SECONDS=60
LOCAL_CAMERAS=[{"id":"cam1","url":"rtsp://...","role":"entrance"}]
```

### 4) جسر تيليجرام (مرة واحدة)

1. أنشئ **قناة خاصة** وأضف البوت **مشرفًا** بصلاحيتي **نشر الرسائل** و**تثبيت الرسائل**
2. ضع `@username` القناة في `STATE_CHANNEL_ID`
3. هذا كل شيء — خوادم تيليجرام هي الناقل المجاني

### 5) التشغيل

> ⚠️ بعد تثبيت termux-services **أعد فتح تطبيق Termux بالكامل** (إغلاق من التطبيقات الحديثة ثم فتحه) ليعمل المشرف `runsvdir`.

```bash
sv-enable retail-ai && sv up retail-ai
sv status retail-ai
tail -f logs/service.log
```

السطر المنتظر في السجل:

```
camera cam1: count=2 staff=False congestion=low
state posted: Normal (count=2)
```

---

## ✨ الميزات

| الميزة | الوصف |
|---|---|
| **تنبيهات فورية** | رسالة في محادثتك عند الازدحام الحرج أو فجوة توظيف جديدة (بفترة تهدئة `ALERT_COOLDOWN_SECONDS`) |
| **أوامر البوت** | أرسل للبوت في الخاص: `/status` ملخص فوري، `/cameras` قائمة الكاميرات، `/ping` فحص النبض |
| **توقيع HMAC** | عند ضبط `BRIDGE_SECRET` (نفس القيمة في اللوحة) تُرفض أي أوامر غير موقّعة — حماية من تزوير الاشتراكات |
| **مقاييس الجهاز** | الحالة المنشورة تتضمن مدة التشغيل، زمن آخر استدلال، وعدد النجاح/الفشل — اللوحة تعرضها وتكشف انقطاع الجهاز |
| **جدار الاشتراك** | انتهاء `subscription_expiry` يوقف الاستدلال ويحوّل الحالة إلى `Blocked` حتى التجديد |

### الأوامر والتنبيهات

- أرسل `/status` للبوت في الخاص (من حساب المشرف أو المالك) ويرد بملخص فوري
- التنبيهات تصل إلى `ALERT_CHAT_ID` (افتراضيًا `USER_ID`) عند: حالة `Critical` أو زيادة فجوات التوظيف

---

## 🧪 الاختبارات

```bash
python3 -m unittest discover -s tests -v   # 18 اختبارًا — stdlib فقط
```

CI (GitHub Actions) يشغّل الاختبارات تلقائيًا على كل push.

## 🧯 استكشاف الأخطاء

| العَرَض | الحل |
|---|---|
| أندرويد يقتل الخدمة (OOM) | استخدم mmproj بنسخة Q8_0، فعّل "بدون قيود" لبطارية Termux، أبقِ إشعار wakelock، ولا تُغلق Termux |
| `sv` يفشل | أعد فتح Termux بالكامل أولًا (يبدأ المشرف runsvdir) |
| `inference exited 1` | تأكد أن `MINICPMV_CLI_PATH` يشير إلى `llama-mtmd-cli` (النسخة القديمة ملغاة) |
| تعذّر فتح الكاميرا | جرّب الرابط بمتصفح VLC أولًا؛ RTSP عبر TCP مفعّل افتراضيًا |
| `Telegram API unreachable` | تحقق من التوكن والشبكة |

## 📄 الترخيص

MIT — راجع [LICENSE](LICENSE).

---

🔗 المستودع الشقيق: [telegram-retail-ai-dashboard](https://github.com/ncecpmotv-cell/telegram-retail-ai-dashboard) — اللوحة المصاحبة.

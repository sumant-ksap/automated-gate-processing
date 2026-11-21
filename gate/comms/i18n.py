"""Driver message templates in the languages most common among US truck drivers.

The texts are written for short, unambiguous instructions. Before going live,
have each language reviewed by a native speaker from your driver community.
"""
from __future__ import annotations

LANGUAGES = {"en": "English", "es": "Español", "hi": "हिन्दी", "pa": "ਪੰਜਾਬੀ", "ru": "Русский"}

T: dict[str, dict[str, str]] = {
    "en": {
        "greet": "{carrier} truck {plate}: you are checked in.",
        "door": "Go to DOOR {door} ({dtype}).",
        "spot": "Park in yard SPOT {spot} and wait. We will text you when your door is ready.",
        "move": "Your door is ready: move your trailer to DOOR {door} now.",
        "north": "Drive straight (north) {m} m on the main yard road.",
        "left": "Turn left.",
        "right": "Turn right.",
        "continue": "Continue {m} m.",
        "door_final": "Back into door {door}. Stay in your cab until the dock light is green.",
        "spot_left": "Spot {spot} is on your left. Park nose-out.",
        "spot_right": "Spot {spot} is on your right. Park nose-out.",
        "power": "Plug your reefer unit into the shore-power post.",
        "inspection": "Trailer inspection required: after the gate turn right, the inspection bay is 130 m ahead.",
        "hold": "Security hold: stop in the guard lane next to the gate and stay in your cab. An officer is coming.",
        "review": "Please pull into the guard lane next to the gate. A guard will check your paperwork.",
        "denied": "Entry refused: {reason}. Use the turn-around lane on the right to exit. Call your dispatcher.",
        "done": "Unloading finished at door {door}. Pull out and leave through the exit gate. Safe travels!",
        "pre": "Pre-check complete for appointment {appt}. Expected arrival {eta}. At the gate stop at the camera "
               "line - no paperwork needed.",
        "pre_blocked": "Problem with appointment {appt}: {issue}. Contact your dispatcher before arriving.",
        "ack": "Reply OK to confirm.",
        "link": "Live map: {link}",
        "lang_set": "Language set to English.",
        "dt_REEFER": "cold dock", "dt_": "dry dock", "dt_FLATBED": "ground-level ramp", "dt_CROSS_DOCK": "cross-dock",
        "r_NO_APPOINTMENT": "no appointment found", "r_CARRIER_SUSPENDED": "carrier suspended",
        "r_INSURANCE_EXPIRED": "insurance expired", "r_UNKNOWN_CARRIER": "carrier not registered",
    },
    "es": {
        "greet": "{carrier}, camión {plate}: registro completado.",
        "door": "Diríjase a la PUERTA {door} ({dtype}).",
        "spot": "Estacione en el ESPACIO {spot} del patio y espere. Le enviaremos un mensaje cuando su puerta "
                "esté lista.",
        "move": "Su puerta está lista: mueva el remolque a la PUERTA {door} ahora.",
        "north": "Siga recto (hacia el norte) {m} m por el camino principal del patio.",
        "left": "Gire a la izquierda.",
        "right": "Gire a la derecha.",
        "continue": "Continúe {m} m.",
        "door_final": "Retroceda hasta la puerta {door}. Espere en la cabina hasta que la luz del muelle esté en verde.",
        "spot_left": "El espacio {spot} está a su izquierda. Estacione de frente hacia la salida.",
        "spot_right": "El espacio {spot} está a su derecha. Estacione de frente hacia la salida.",
        "power": "Conecte la unidad refrigerada al poste de corriente.",
        "inspection": "Se requiere inspección del remolque: después de la entrada gire a la derecha; el área de "
                      "inspección está a 130 m.",
        "hold": "Retención de seguridad: deténgase en el carril de vigilancia junto a la entrada y permanezca en la "
                "cabina. Un oficial viene en camino.",
        "review": "Por favor, entre al carril de vigilancia junto a la entrada. Un guardia revisará sus documentos.",
        "denied": "Entrada denegada: {reason}. Use el carril de retorno a la derecha para salir. Llame a su "
                  "despachador.",
        "done": "Descarga terminada en la puerta {door}. Salga por la puerta de salida. ¡Buen viaje!",
        "pre": "Pre-registro completo para la cita {appt}. Llegada estimada {eta}. En la entrada, deténgase en la "
               "línea de cámaras; no necesita documentos.",
        "pre_blocked": "Problema con la cita {appt}: {issue}. Comuníquese con su despachador antes de llegar.",
        "ack": "Responda OK para confirmar.",
        "link": "Mapa en vivo: {link}",
        "lang_set": "Idioma cambiado a español.",
        "dt_REEFER": "muelle refrigerado", "dt_": "muelle seco", "dt_FLATBED": "rampa a nivel del suelo",
        "dt_CROSS_DOCK": "muelle de cross-dock",
        "r_NO_APPOINTMENT": "no hay cita programada", "r_CARRIER_SUSPENDED": "transportista suspendido",
        "r_INSURANCE_EXPIRED": "seguro vencido", "r_UNKNOWN_CARRIER": "transportista no registrado",
    },
    "hi": {
        "greet": "{carrier} ट्रक {plate}: आपका चेक-इन हो गया है।",
        "door": "डॉक दरवाज़ा {door} ({dtype}) पर जाएँ।",
        "spot": "यार्ड स्पॉट {spot} पर पार्क करें और प्रतीक्षा करें। दरवाज़ा तैयार होने पर हम आपको संदेश भेजेंगे।",
        "move": "आपका दरवाज़ा तैयार है: ट्रेलर को अभी दरवाज़ा {door} पर ले जाएँ।",
        "north": "मुख्य यार्ड सड़क पर सीधे (उत्तर की ओर) {m} मीटर चलें।",
        "left": "बाएँ मुड़ें।",
        "right": "दाएँ मुड़ें।",
        "continue": "{m} मीटर आगे चलें।",
        "door_final": "ट्रेलर को पीछे करके दरवाज़ा {door} पर लगाएँ। डॉक की बत्ती हरी होने तक केबिन में रहें।",
        "spot_left": "स्पॉट {spot} आपके बाईं ओर है। गाड़ी का मुँह बाहर की ओर रखकर पार्क करें।",
        "spot_right": "स्पॉट {spot} आपके दाईं ओर है। गाड़ी का मुँह बाहर की ओर रखकर पार्क करें।",
        "power": "रीफ़र यूनिट को बिजली के पोस्ट (शोर-पावर) से जोड़ें।",
        "inspection": "ट्रेलर की जाँच ज़रूरी है: गेट के बाद दाएँ मुड़ें, जाँच क्षेत्र 130 मीटर आगे है।",
        "hold": "सुरक्षा रोक: गेट के पास गार्ड लेन में रुकें और केबिन में ही रहें। सुरक्षा अधिकारी आ रहे हैं।",
        "review": "कृपया गेट के पास गार्ड लेन में गाड़ी लगाएँ। गार्ड आपके कागज़ात जाँचेंगे।",
        "denied": "प्रवेश नहीं मिला: {reason}। बाहर निकलने के लिए दाईं ओर टर्न-अराउंड लेन का उपयोग करें। "
                  "अपने डिस्पैचर को कॉल करें।",
        "done": "दरवाज़ा {door} पर माल उतारना पूरा हुआ। बाहर निकलने वाले गेट से जाएँ। शुभ यात्रा!",
        "pre": "अपॉइंटमेंट {appt} की पूर्व-जाँच पूरी हुई। अनुमानित आगमन {eta}। गेट पर कैमरा लाइन पर रुकें - "
               "कागज़ात की ज़रूरत नहीं।",
        "pre_blocked": "अपॉइंटमेंट {appt} में समस्या: {issue}। आने से पहले अपने डिस्पैचर से संपर्क करें।",
        "ack": "पुष्टि के लिए OK लिखकर जवाब दें।",
        "link": "लाइव नक्शा: {link}",
        "lang_set": "भाषा हिन्दी कर दी गई है।",
        "dt_REEFER": "कोल्ड डॉक", "dt_": "ड्राई डॉक", "dt_FLATBED": "ज़मीनी-स्तर रैंप", "dt_CROSS_DOCK": "क्रॉस-डॉक",
        "r_NO_APPOINTMENT": "कोई अपॉइंटमेंट नहीं है", "r_CARRIER_SUSPENDED": "कैरियर निलंबित है",
        "r_INSURANCE_EXPIRED": "बीमा समाप्त हो गया है", "r_UNKNOWN_CARRIER": "कैरियर पंजीकृत नहीं है",
    },
    "pa": {
        "greet": "{carrier} ਟਰੱਕ {plate}: ਤੁਹਾਡਾ ਚੈੱਕ-ਇਨ ਹੋ ਗਿਆ ਹੈ।",
        "door": "ਡੌਕ ਦਰਵਾਜ਼ਾ {door} ({dtype}) 'ਤੇ ਜਾਓ।",
        "spot": "ਯਾਰਡ ਸਪੌਟ {spot} 'ਤੇ ਪਾਰਕ ਕਰੋ ਅਤੇ ਉਡੀਕ ਕਰੋ। ਦਰਵਾਜ਼ਾ ਤਿਆਰ ਹੋਣ 'ਤੇ ਅਸੀਂ ਤੁਹਾਨੂੰ ਸੁਨੇਹਾ ਭੇਜਾਂਗੇ।",
        "move": "ਤੁਹਾਡਾ ਦਰਵਾਜ਼ਾ ਤਿਆਰ ਹੈ: ਟ੍ਰੇਲਰ ਨੂੰ ਹੁਣੇ ਦਰਵਾਜ਼ਾ {door} 'ਤੇ ਲੈ ਜਾਓ।",
        "north": "ਮੁੱਖ ਯਾਰਡ ਸੜਕ 'ਤੇ ਸਿੱਧੇ (ਉੱਤਰ ਵੱਲ) {m} ਮੀਟਰ ਚੱਲੋ।",
        "left": "ਖੱਬੇ ਮੁੜੋ।",
        "right": "ਸੱਜੇ ਮੁੜੋ।",
        "continue": "{m} ਮੀਟਰ ਅੱਗੇ ਚੱਲੋ।",
        "door_final": "ਟ੍ਰੇਲਰ ਨੂੰ ਪਿੱਛੇ ਕਰਕੇ ਦਰਵਾਜ਼ਾ {door} 'ਤੇ ਲਗਾਓ। ਡੌਕ ਦੀ ਬੱਤੀ ਹਰੀ ਹੋਣ ਤੱਕ ਕੈਬਿਨ ਵਿੱਚ ਰਹੋ।",
        "spot_left": "ਸਪੌਟ {spot} ਤੁਹਾਡੇ ਖੱਬੇ ਪਾਸੇ ਹੈ। ਗੱਡੀ ਦਾ ਮੂੰਹ ਬਾਹਰ ਵੱਲ ਰੱਖ ਕੇ ਪਾਰਕ ਕਰੋ।",
        "spot_right": "ਸਪੌਟ {spot} ਤੁਹਾਡੇ ਸੱਜੇ ਪਾਸੇ ਹੈ। ਗੱਡੀ ਦਾ ਮੂੰਹ ਬਾਹਰ ਵੱਲ ਰੱਖ ਕੇ ਪਾਰਕ ਕਰੋ।",
        "power": "ਰੀਫ਼ਰ ਯੂਨਿਟ ਨੂੰ ਬਿਜਲੀ ਦੇ ਪੋਸਟ ਨਾਲ ਜੋੜੋ।",
        "inspection": "ਟ੍ਰੇਲਰ ਦੀ ਜਾਂਚ ਲਾਜ਼ਮੀ ਹੈ: ਗੇਟ ਤੋਂ ਬਾਅਦ ਸੱਜੇ ਮੁੜੋ, ਜਾਂਚ ਖੇਤਰ 130 ਮੀਟਰ ਅੱਗੇ ਹੈ।",
        "hold": "ਸੁਰੱਖਿਆ ਰੋਕ: ਗੇਟ ਦੇ ਕੋਲ ਗਾਰਡ ਲੇਨ ਵਿੱਚ ਰੁਕੋ ਅਤੇ ਕੈਬਿਨ ਵਿੱਚ ਹੀ ਰਹੋ। ਸੁਰੱਖਿਆ ਅਧਿਕਾਰੀ ਆ ਰਹੇ ਹਨ।",
        "review": "ਕਿਰਪਾ ਕਰਕੇ ਗੇਟ ਦੇ ਕੋਲ ਗਾਰਡ ਲੇਨ ਵਿੱਚ ਗੱਡੀ ਲਗਾਓ। ਗਾਰਡ ਤੁਹਾਡੇ ਕਾਗਜ਼ ਚੈੱਕ ਕਰੇਗਾ।",
        "denied": "ਦਾਖ਼ਲਾ ਨਹੀਂ ਮਿਲਿਆ: {reason}। ਬਾਹਰ ਨਿਕਲਣ ਲਈ ਸੱਜੇ ਪਾਸੇ ਟਰਨ-ਅਰਾਊਂਡ ਲੇਨ ਵਰਤੋ। "
                  "ਆਪਣੇ ਡਿਸਪੈਚਰ ਨੂੰ ਫ਼ੋਨ ਕਰੋ।",
        "done": "ਦਰਵਾਜ਼ਾ {door} 'ਤੇ ਮਾਲ ਉਤਾਰਨਾ ਪੂਰਾ ਹੋਇਆ। ਬਾਹਰ ਜਾਣ ਵਾਲੇ ਗੇਟ ਰਾਹੀਂ ਜਾਓ। ਸੁਰੱਖਿਅਤ ਸਫ਼ਰ!",
        "pre": "ਅਪੌਇੰਟਮੈਂਟ {appt} ਦੀ ਪਹਿਲਾਂ-ਜਾਂਚ ਪੂਰੀ ਹੋਈ। ਅੰਦਾਜ਼ਨ ਪਹੁੰਚ {eta}। ਗੇਟ 'ਤੇ ਕੈਮਰਾ ਲਾਈਨ 'ਤੇ ਰੁਕੋ - "
               "ਕਾਗਜ਼ਾਂ ਦੀ ਲੋੜ ਨਹੀਂ।",
        "pre_blocked": "ਅਪੌਇੰਟਮੈਂਟ {appt} ਵਿੱਚ ਸਮੱਸਿਆ: {issue}। ਆਉਣ ਤੋਂ ਪਹਿਲਾਂ ਆਪਣੇ ਡਿਸਪੈਚਰ ਨਾਲ ਸੰਪਰਕ ਕਰੋ।",
        "ack": "ਪੁਸ਼ਟੀ ਲਈ OK ਲਿਖ ਕੇ ਜਵਾਬ ਦਿਓ।",
        "link": "ਲਾਈਵ ਨਕਸ਼ਾ: {link}",
        "lang_set": "ਭਾਸ਼ਾ ਪੰਜਾਬੀ ਕਰ ਦਿੱਤੀ ਗਈ ਹੈ।",
        "dt_REEFER": "ਕੋਲਡ ਡੌਕ", "dt_": "ਡ੍ਰਾਈ ਡੌਕ", "dt_FLATBED": "ਜ਼ਮੀਨੀ-ਪੱਧਰ ਰੈਂਪ", "dt_CROSS_DOCK": "ਕ੍ਰਾਸ-ਡੌਕ",
        "r_NO_APPOINTMENT": "ਕੋਈ ਅਪੌਇੰਟਮੈਂਟ ਨਹੀਂ", "r_CARRIER_SUSPENDED": "ਕੈਰੀਅਰ ਮੁਅੱਤਲ ਹੈ",
        "r_INSURANCE_EXPIRED": "ਬੀਮਾ ਖ਼ਤਮ ਹੋ ਗਿਆ ਹੈ", "r_UNKNOWN_CARRIER": "ਕੈਰੀਅਰ ਰਜਿਸਟਰਡ ਨਹੀਂ",
    },
    "ru": {
        "greet": "{carrier}, грузовик {plate}: регистрация на въезде выполнена.",
        "door": "Следуйте к ВОРОТАМ {door} ({dtype}).",
        "spot": "Встаньте на СТОЯНКУ {spot} и ожидайте. Мы пришлём сообщение, когда ворота освободятся.",
        "move": "Ваши ворота готовы: переставьте прицеп к ВОРОТАМ {door} сейчас.",
        "north": "Двигайтесь прямо (на север) {m} м по главной дороге площадки.",
        "left": "Поверните налево.",
        "right": "Поверните направо.",
        "continue": "Проезжайте {m} м.",
        "door_final": "Подайте прицеп задним ходом к воротам {door}. Оставайтесь в кабине, пока сигнал у ворот не "
                      "станет зелёным.",
        "spot_left": "Место {spot} слева от вас. Паркуйтесь передом к выезду.",
        "spot_right": "Место {spot} справа от вас. Паркуйтесь передом к выезду.",
        "power": "Подключите рефрижераторную установку к электроколонке.",
        "inspection": "Требуется осмотр прицепа: после въезда поверните направо, зона осмотра через 130 м.",
        "hold": "Остановка службой безопасности: встаньте в полосу охраны у въезда и оставайтесь в кабине. "
                "К вам подойдёт сотрудник.",
        "review": "Пожалуйста, заедьте в полосу охраны у въезда. Охранник проверит ваши документы.",
        "denied": "Въезд запрещён: {reason}. Для выезда используйте полосу разворота справа. Позвоните диспетчеру.",
        "done": "Разгрузка у ворот {door} завершена. Выезжайте через выездные ворота. Счастливого пути!",
        "pre": "Предварительная проверка по записи {appt} пройдена. Ожидаемое прибытие {eta}. На въезде "
               "остановитесь у линии камер - документы не нужны.",
        "pre_blocked": "Проблема с записью {appt}: {issue}. Свяжитесь с диспетчером до прибытия.",
        "ack": "Ответьте OK для подтверждения.",
        "link": "Карта онлайн: {link}",
        "lang_set": "Язык изменён на русский.",
        "dt_REEFER": "холодный док", "dt_": "сухой док", "dt_FLATBED": "рампа на уровне земли",
        "dt_CROSS_DOCK": "кросс-док",
        "r_NO_APPOINTMENT": "нет записи на разгрузку", "r_CARRIER_SUSPENDED": "перевозчик приостановлен",
        "r_INSURANCE_EXPIRED": "страховка просрочена", "r_UNKNOWN_CARRIER": "перевозчик не зарегистрирован",
    },
}


def t(lang: str, key: str, **kw) -> str:
    table = T.get(lang) or T["en"]
    return (table.get(key) or T["en"][key]).format(**kw)


# words a driver may reply with to confirm (any language)
ACK_WORDS = {"OK", "OKAY", "YES", "Y", "SI", "SÍ", "ДА", "ОК", "ਠੀਕ", "ਹਾਂ", "ठीक", "हाँ", "हां", "DONE"}

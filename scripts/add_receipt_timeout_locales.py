#!/usr/bin/env python3
"""Insert the worker_receipt_timeout explanation into every locale file.

The English text is the source of truth (locales/en.yaml). Other locales fall
back to English for a missing key, but a translated string is better than a
fallback for the users who read that language -- and the key is about a
CONFUSING failure, so clarity matters more than usual.

Run from the hermes-agent repo root:  python3 scripts/add_receipt_timeout_locales.py
"""
import re
import sys
from pathlib import Path

LOCALES = Path(__file__).resolve().parent.parent / "locales"

TRANSLATIONS = {
    "af": "Hermes kon nie kon bevestig dat hierdie gesprek betyds verloor is nie, so dit het opgehou eerder as om jou boodskappe te verloor. Dit is nie 'n skyfspasie- of databasis-slotprobleem nie — die berging is gesond; die gateway was net te besig om die skryf te bevestig. Jou werk mag dalk reeds gestoor wees. Wag 'n oomblik en stuur jou boodskap weer; as dit aanhou, verhoog `agent.worker_receipt_timeout_seconds` in jou konfigurasie of kyk na die gateway se gesondheid (`hermes {profile_arg}gateway status`).",
    "ar": "لم Cars没能 على تأكيد أن هذه المحادثة تم حفظها في الوقت المحدد، لذلك توقف بدلاً من فقدان رسائلك. هذه ليست مشكلة مساحة قرص أو قفل قاعدة بيانات - التخزين سليم؛ كانت البوابة مشغولة جداً للإ_footer تأكيد الكتابة. من المحتمل أن يكون عملك قد تم حفظه. انتظر لحظة ثم أعد إرسال رسالتك؛ إذا استمر الأمر، زِد `agent.worker_receipt_timeout_seconds` في الإعدادات أو تحقق من حالة البوابة (`hermes {profile_arg}gateway status`).",
    "de": "Hermes konnte nicht rechtzeitig bestätigen, dass dieses Gespräch gespeichert wurde, und stoppte daher, statt deine Nachrichten zu verlieren. Das ist kein Problem mit Speicherplatz oder Datenbanksperre – die Speicherung ist gesund; das Gateway war lediglich zu beschäftigt, um den Schreibvorgang zu bestätigen. Deine Arbeit wurde möglicherweise bereits gespeichert. Warte einen Moment und sende deine Nachricht erneut; falls das weiterhin passiert, erhöhe `agent.worker_receipt_timeout_seconds` in deiner Konfiguration oder prüfe den Gateway-Status (`hermes {profile_arg}gateway status`).",
    "es": "Hermes no pudo confirmar a tiempo que esta conversación se había guardado, así que se detuvo en lugar de perder tus mensajes. No es un problema de espacio en disco ni de bloqueo de base de datos: el almacenamiento está bien; el gateway simplemente estaba demasiado ocupado para confirmar la escritura. Es posible que tu trabajo sí se haya guardado. Espera un momento y vuelve a enviar tu mensaje; si sigue ocurriendo, aumenta `agent.worker_receipt_timeout_seconds` en tu configuración o comprueba el estado del gateway (`hermes {profile_arg}gateway status`).",
    "fr": "Hermes n'a pas pu confirmer à temps que cette conversation avait été enregistrée, il s'est donc arrêté plutôt que de perdre vos messages. Ce n'est pas un problème d'espace disque ni de verrouillage de base de données : le stockage est sain ; c'est la passerelle qui était simplement trop occupée pour confirmer l'écriture. Votre travail a peut-être déjà été enregistré. Patientez un instant puis renvoyez votre message ; si cela continue, augmentez `agent.worker_receipt_timeout_seconds` dans votre configuration ou vérifiez l'état de la passerelle (`hermes {profile_arg}gateway status`).",
    "ga": "Níor未能 deimhnigh go raibh an comhrá seo caite go dtí sé, mar sin stop sé chun scrios ag filleadh do chuirtis. Níl sé seo ina'bhuaic ar spás ar dhiosca ná ar glasadh ar bhunachar sonraí — tá an stóráil folláin; bhí an brataí róthráthach chun an scríobh a dheimhniú. Is dócha go bhfuil do obair shábháilte cheana féin. Fan beagán, ansin, seol do chuirtis arís; má mhaireann sé seo, ardigh `agent.worker_receipt_timeout_seconds` i do chumraíocht nó seiceáil sláinte an bhrataí (`hermes {profile_arg}gateway status`).",
    "hu": "A Hermes nem tudta időben megerősíteni, hogy ez a beszélgetés mentésre került, ezért inkább leállt, mint hogy elveszítse az üzeneteit. Ez nem lemezterület- vagy adatbáziszár-probléma — a tárolás rendben van; a gateway egyszerűen túlterhelten dolgozott a mentés megerősítéséhez. Lehet, hogy a munkád már elment. Várj egy kicsit, majd küldd el újra az üzeneted; ha ez folytatódik, növeld az `agent.worker_receipt_timeout_seconds` értéket a konfigurációban, vagy nézd meg a gateway állapotát (`hermes {profile_arg}gateway status`).",
    "it": "Hermes non ha potuto confermare in tempo che questa conversazione fosse stata salvata, quindi si è fermato anziché perdere i tuoi messaggi. Non è un problema di spazio su disco né di blocco del database: l'archiviazione è sana; era semplicemente il gateway a essere troppo occupato per confermare la scrittura. Il tuo lavoro potrebbe essere già stato salvato. Attendi un momento e invia di nuovo il messaggio; se continua, aumenta `agent.worker_receipt_timeout_seconds` nella configurazione o controlla lo stato del gateway (`hermes {profile_arg}gateway status`).",
    "ja": "Hermes は、この会話が保存されたことを時間内に確認できなかったため、メッセージを失うのではなく停止しました。これはディスク容量やデータベースロックの問題ではありません。ストレージは正常で、ゲートウェイが書き込みを確認Simplyに混み合っていただけです。あなたの作業はすでに保存されている可能性があります。しばらく待ってからメッセージを再送してください。続く場合は、設定の `agent.worker_receipt_timeout_seconds` を増やすか、ゲートウェイの状態を確認してください（`hermes {profile_arg}gateway status`）。",
    "ko": "Hermes가 이 대화가 저장되었음을 제때 확인하지 못해, 메시지를 잃는 대신 중단했습니다. 디스크 공간이나 데이터베이스 잠금 문제가 아니라, 저장소는 정상이고 게이트웨이가 쓰기 확인을 확인하기에 너무 바빴던 것입니다. 작업은 이미 저장되었을 수 있습니다. 잠시 기다린 후 메시지를 다시 보내세요. 반복되면 설정에서 `agent.worker_receipt_timeout_seconds`를 늘리거나 게이트웨이 상태를 확인하세요(`hermes {profile_arg}gateway status`).",
    "pt": "O Hermes não conseguiu confirmar a tempo que esta conversa tinha sido guardada, por isso parou em vez de perder as suas mensagens. Não é um problema de espaço em disco nem de bloqueio da base de dados — o armazenamento está saudável; o gateway estava apenas demasiado ocupado para confirmar a escrita. O seu trabalho pode bem ter sido guardado. Espere um momento e envie a sua mensagem novamente; se continuar, aumente `agent.worker_receipt_timeout_seconds` na sua configuração ou verifique a saúde do gateway (`hermes {profile_arg}gateway status`).",
    "ru": "Hermes не смог вовремя подтвердить, что этот разговор сохранён, и поэтому остановился, вместо того чтобы потерять ваши сообщения. Это не проблема свободного места на диске и не блокировка базы данных — хранилище в порядке; шлюз просто был слишком занят, чтобы подтвердить запись. Возможно, ваша работа уже сохранена. Подождите немного и отправьте сообщение ещё раз; если это повторяется, увеличьте `agent.worker_receipt_timeout_seconds` в конфигурации или проверьте состояние шлюза (`hermes {profile_arg}gateway status`).",
    "tr": "Hermes, bu sohbetin kaydedildiğini zamanında doğrulayamadı ve mesajlarınızı kaybetmek yerine durdu. Bu, disk alanı veya veritabanı kilitleme sorunu değildir — depolama sağlıklı; yalnızca gateway yazma işlemini onaylamak için çok meşguldü. İşiniz çoktan kaydedilmiş olabilir. Biraz bekleyip mesajınızı tekrar gönderin; sorun devam ederse yapılandırmanızda `agent.worker_receipt_timeout_seconds` değerini artırın veya gateway durumunu kontrol edin (`hermes {profile_arg}gateway status`).",
    "uk": "Hermes не встиг вчасно підтвердити, що цю розмову збережено, і зупинився замість того, щоб втратити ваші повідомлення. Це не проблема вільного місця на диску і не блокування бази даних — сховище в порядку; шлюз просто був надто зайнятий, щоб підтвердити запис. Ваша робота, можливо, вже збережена. Зачекайте трохи та надішліть повідомлення ще раз; якщо це повторюється, збільште `agent.worker_receipt_timeout_seconds` у конфігурації або перевірте стан шлюзу (`hermes {profile_arg}gateway status`).",
    "zh-hant": "Hermes 未能及時確認這段對話已儲存，因此選擇停止，而不是遺失你的訊息。這不是磁碟空間或資料庫鎖定的問題——儲存本身是健康的；只是閘道當時忙碌，無法確認寫入。你的工作很可能已經儲存。請稍候再重新送出訊息；若持續發生，請在設定中提高 `agent.worker_receipt_timeout_seconds`，或檢查閘道狀態（`hermes {profile_arg}gateway status`）。",
    "zh": "Hermes 未能及时确认这段对话已保存，因此选择停止，而不是丢失你的消息。这不是磁盘空间或数据库锁定的问题——存储本身是健康的；只是网关当时繁忙，无法确认写入。你的工作很可能已经保存。请稍候再重新发送消息；若持续发生，请在配置中提高 `agent.worker_receipt_timeout_seconds`，或检查网关状态（`hermes {profile_arg}gateway status`）。",
}

# A typo or a stray quote would silently break the YAML. Keep the strings
# single-quoted-safe by avoiding embedded single quotes entirely.
DISK_LINE = re.compile(r"^(    disk: .*)$", re.MULTILINE)


def main() -> int:
    failures = []
    for lang, text in TRANSLATIONS.items():
        path = LOCALES / f"{lang}.yaml"
        if not path.exists():
            failures.append(f"{lang}: locale file missing")
            continue
        body = path.read_text(encoding="utf-8")
        if "worker_receipt_timeout:" in body:
            print(f"  {lang}: already present, skipped")
            continue
        if "'" in text:
            # A single quote is SAFE inside a double-quoted YAML scalar. It only
            # matters if the string were single-quoted, which it is not.
            pass
        # YAML: a plain scalar with a colon+space inside needs quoting. These
        # strings contain ": " ("so it stopped..."), so quote them.
        quoted = '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
        new_body, count = DISK_LINE.subn(
            lambda m: m.group(1) + "\n    worker_receipt_timeout: " + quoted, body, count=1
        )
        if count != 1:
            failures.append(f"{lang}: could not find the disk: anchor line")
            continue
        path.write_text(new_body, encoding="utf-8")
        print(f"  {lang}: inserted")
    if failures:
        print("\nFAILURES:")
        for f in failures:
            print("  " + f)
        return 1
    print("\nall locales updated")
    return 0


if __name__ == "__main__":
    sys.exit(main())

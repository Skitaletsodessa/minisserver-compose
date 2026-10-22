package home.minis.portal

/** Every user-visible string of the app (Russian only). Plain constants so that the pure logic (error mapping, form validation) is testable on the JVM. */
object Texts {
    const val SIGN_IN_TITLE = "Вход в «Дом»"
    const val SIGN_IN_INTRO = "Войдите один раз под своим именем и паролем. Дальше приложение открывается по отпечатку пальца, пароль больше не нужен."
    const val FIELD_NAME = "Имя"
    const val FIELD_PASSWORD = "Пароль"
    const val FIELD_TOTP = "Код из аутентификатора (для администратора)"
    const val FIELD_URL = "Адрес сервера"
    const val ADVANCED = "Дополнительно"
    const val SIGN_IN_BUTTON = "Войти"
    const val WORKING = "Подождите…"

    const val ERR_NAME_EMPTY = "Введите имя."
    const val ERR_PASSWORD_EMPTY = "Введите пароль."
    const val ERR_TOTP_FORMAT = "Код состоит из 6 цифр."
    const val ERR_URL_BAD = "Адрес сервера должен начинаться с https:// и не содержать пути."

    const val LOCK_TITLE = "Приложение заблокировано"
    const val LOCK_BUTTON = "Открыть по отпечатку"
    const val PROMPT_UNLOCK_TITLE = "Откройте «Дом»"
    const val PROMPT_UNLOCK_SUBTITLE = "Приложите палец к сканеру."
    const val PROMPT_SEAL_TITLE = "Подтвердите отпечатком"
    const val PROMPT_SEAL_SUBTITLE = "Так ключ доступа этого телефона будет защищён вашим отпечатком."

    const val NO_SECURE_LOCK = "На телефоне нет блокировки экрана с отпечатком пальца. Включите блокировку и добавьте отпечаток в настройках телефона, потом войдите снова. Без этого приложение не сохранит доступ."
    const val SIGN_IN_CANCELLED = "Вход не завершён: отпечаток не подтверждён. Доступ на этом телефоне не сохранён."
    const val RELOGIN_REVOKED = "Нужно войти заново: доступ этого телефона отозван или истёк."
    const val RELOGIN_NEW_FINGERPRINT = "Нужно войти заново: на телефоне появился новый отпечаток, и прежний ключ доступа стёрт (так задумано)."
    const val RELOGIN_KEY_LOST = "Нужно войти заново: ключ доступа на этом телефоне недоступен."

    const val NET_TITLE = "Нет связи с домом"
    const val NET_TEXT = "Проверьте, что Tailscale включён и телефон в сети, потом повторите."
    const val TLS_TITLE = "Соединение небезопасно"
    const val TLS_TEXT = "Сертификат сервера не подошёл или неверно время на телефоне. Приложение не будет работать с таким соединением. Проверьте дату и время на телефоне."
    const val SERVER_TITLE = "Дом ответил ошибкой"
    const val SERVER_TEXT = "Сервер временно не справляется. Повторите через минуту."
    const val BTN_RETRY = "Повторить"
    const val BTN_TAILSCALE = "Открыть Tailscale"
    const val BTN_SIGN_IN_AGAIN = "Войти заново"
    const val BTN_CLOSE = "Закрыть"
    const val BTN_CANCEL = "Отмена"
    const val TAILSCALE_MISSING = "Приложение Tailscale не найдено на телефоне."

    const val LOGIN_BAD = "Неверное имя, пароль или код."
    const val LOGIN_THROTTLED = "Слишком много попыток. Подождите немного и попробуйте снова."
    const val LOGIN_DEVICE_LIMIT = "На этого человека уже зарегистрировано максимум телефонов. Попросите администратора отозвать лишний."
    const val LOGIN_GENERIC = "Не получилось войти. Попробуйте ещё раз."

    const val ABOUT_TITLE = "О приложении"
    const val ABOUT_SIGN_OUT = "Выйти и забыть это устройство"
    const val ABOUT_SIGN_OUT_NOTE = "На сервере доступ этого телефона будет отозван. Если связи нет, телефон забудет доступ сам, а администратору придётся отозвать его на сайте."
    const val ABOUT_SIGNED_OUT_OFFLINE = "Нет связи с домом: телефон забыл доступ, но на сервере его должен отозвать администратор («Люди и устройства» на сайте)."

    const val BLOCKED_LINK = "Эта ссылка не из «Дома» и не открыта."
}

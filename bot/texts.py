CHOOSE_LANGUAGE = "Choose your language / Выберите язык / Escolha o idioma / Elige tu idioma"

DEFAULT_LANGUAGES = {
    "ru": "🇷🇺 Русский",
    "en": "🇬🇧 English",
    "pt": "🇵🇹 Português",
    "es": "🇪🇸 Español",
}

DEFAULTS = {
    "ru": {
        "sub_prompt": (
            "<b>Для участия нужна подписка на Telegram-канал Royal Partners</b>\n\n"
            "Подпишись на канал и нажми «Проверить подписку»."
        ),
        "btn_subscribe": "Подписаться",
        "btn_check": "Проверить подписку",
        "not_subscribed": (
            "<b>Подписка на канал не найдена</b>\n\n"
            "Для продолжения подпишись на Telegram-канал и нажми «Проверить подписку»."
        ),
        "registered": (
            "<b>Подписка подтверждена ✨</b>\n\n"
            "Ты участвуешь в MIRACLE GLOW.\nЖелаем удачи в новогоднем розыгрыше!"
        ),
        "already": (
            "<b>Ты уже участвуешь в MIRACLE GLOW ✨</b>\n\nПовторная регистрация не требуется."
        ),
        "error": "<b>Что-то пошло не так</b>\n\nПопробуй еще раз.",
    },
    "en": {
        "sub_prompt": (
            "<b>To take part, you need to be subscribed to the Royal Partners Telegram channel</b>\n\n"
            "Subscribe to the channel and tap “Check subscription”."
        ),
        "btn_subscribe": "Subscribe",
        "btn_check": "Check subscription",
        "not_subscribed": (
            "<b>Channel subscription not found</b>\n\n"
            "To continue, subscribe to the Telegram channel and tap “Check subscription”."
        ),
        "registered": (
            "<b>Subscription confirmed ✨</b>\n\n"
            "You are in MIRACLE GLOW.\nGood luck in the New Year giveaway!"
        ),
        "already": (
            "<b>You are already taking part in MIRACLE GLOW ✨</b>\n\n"
            "No need to register again."
        ),
        "error": "<b>Something went wrong</b>\n\nPlease try again.",
    },
    "pt": {
        "sub_prompt": (
            "<b>Para participar, é preciso estar inscrito no canal do Telegram da Royal Partners</b>\n\n"
            "Inscreva-se no canal e toque em «Verificar inscrição»."
        ),
        "btn_subscribe": "Inscrever-se",
        "btn_check": "Verificar inscrição",
        "not_subscribed": (
            "<b>Inscrição no canal não encontrada</b>\n\n"
            "Para continuar, inscreva-se no canal do Telegram e toque em «Verificar inscrição»."
        ),
        "registered": (
            "<b>Inscrição confirmada ✨</b>\n\n"
            "Você está participando do MIRACLE GLOW.\nBoa sorte no sorteio de Ano Novo!"
        ),
        "already": (
            "<b>Você já está participando do MIRACLE GLOW ✨</b>\n\n"
            "Não é necessário se registrar novamente."
        ),
        "error": "<b>Algo deu errado</b>\n\nTente novamente.",
    },
    "es": {
        "sub_prompt": (
            "<b>Para participar, necesitas estar suscrito al canal de Telegram de Royal Partners</b>\n\n"
            "Suscríbete al canal y pulsa «Comprobar suscripción»."
        ),
        "btn_subscribe": "Suscribirse",
        "btn_check": "Comprobar suscripción",
        "not_subscribed": (
            "<b>No se encontró la suscripción al canal</b>\n\n"
            "Para continuar, suscríbete al canal de Telegram y pulsa «Comprobar suscripción»."
        ),
        "registered": (
            "<b>Suscripción confirmada ✨</b>\n\n"
            "Ya participas en MIRACLE GLOW.\n¡Te deseamos suerte en el sorteo de Año Nuevo!"
        ),
        "already": (
            "<b>Ya participas en MIRACLE GLOW ✨</b>\n\nNo es necesario registrarte de nuevo."
        ),
        "error": "<b>Algo salió mal</b>\n\nInténtalo de nuevo.",
    },
}


# Подписи ключей для админки
KEY_LABELS = {
    "sub_prompt": "Просьба подписаться",
    "btn_subscribe": "Кнопка «Подписаться»",
    "btn_check": "Кнопка «Проверить подписку»",
    "not_subscribed": "Подписка не найдена",
    "registered": "Успешная регистрация",
    "already": "Уже участвует",
    "error": "Техническая ошибка",
}

GLOBAL = "*"  # «язык» для общих текстов (приглашение выбрать язык)
CHOOSE_KEY = "choose_language"


class Texts:
    """Тексты бота: значения по умолчанию + правки из админки (таблица texts)."""

    def __init__(self, db):
        self.db = db
        self.languages: dict[str, str] = {}
        self.overrides: dict[tuple[str, str], str] = {}

    async def load(self) -> None:
        self.languages = {r["code"]: r["title"] for r in await self.db.list_languages()}
        self.overrides = {(r["language"], r["key"]): r["value"] for r in await self.db.list_texts()}

    @staticmethod
    def default(language: str, key: str) -> str:
        if language == GLOBAL:
            return CHOOSE_LANGUAGE
        return DEFAULTS.get(language, DEFAULTS["en"])[key]

    def get(self, language: str | None, key: str) -> str:
        language = language or "en"
        return self.overrides.get((language, key)) or self.default(language, key)

    def is_overridden(self, language: str, key: str) -> bool:
        return (language, key) in self.overrides

    def choose_prompt(self) -> str:
        return self.get(GLOBAL, CHOOSE_KEY)

    async def set(self, language: str, key: str, value: str) -> None:
        await self.db.set_text(language, key, value)
        self.overrides[(language, key)] = value

    async def reset(self, language: str, key: str) -> None:
        await self.db.delete_text(language, key)
        self.overrides.pop((language, key), None)

    async def add_language(self, code: str, title: str) -> bool:
        if not await self.db.add_language(code, title):
            return False
        self.languages[code] = title
        return True

    async def delete_language(self, code: str) -> None:
        await self.db.delete_language(code)
        await self.load()

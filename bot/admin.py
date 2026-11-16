import asyncio
import csv
import io
import logging
import re
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .config import Config, parse_channel_id
from .db import Database
from .handlers import is_subscribed
from .manager import BotManager
from .texts import KEY_LABELS, Texts

log = logging.getLogger(__name__)

SOURCE_RE = re.compile(r"^[A-Z0-9_]{1,10}$")
LANG_RE = re.compile(r"^[a-z]{2,5}$")
TOKEN_RE = re.compile(r"^\d+:[\w-]{30,}$")

_background: set[asyncio.Task] = set()


class AddBot(StatesGroup):
    source = State()
    token = State()
    language = State()
    channel_id = State()
    channel_url = State()


class EditChannel(StatesGroup):
    channel_id = State()
    channel_url = State()


class FindUser(StatesGroup):
    telegram_id = State()


class EditText(StatesGroup):
    value = State()


class AddLanguage(StatesGroup):
    code = State()
    title = State()


def kb(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=data) for text, data in row]
            for row in rows
        ]
    )


BACK_MENU = [("« Меню", "menu")]
CANCEL = kb([("Отмена", "menu")])


async def show(event: Message | CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    if isinstance(event, CallbackQuery):
        try:
            await event.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:  # «message is not modified» и т.п.
            pass
        await event.answer()
    else:
        await event.answer(text, reply_markup=markup)


def text_label(language: str, key: str) -> str:
    return KEY_LABELS[key]


def valid_text_ref(texts: Texts, language: str, key: str) -> bool:
    return language in texts.languages and key in KEY_LABELS


def build_admin_router(cfg: Config) -> Router:
    router = Router()
    is_admin = F.from_user.id.in_(cfg.admin_ids)
    router.message.filter(F.chat.type == "private")

    # --- доступ -----------------------------------------------------------

    @router.message(~is_admin)
    async def no_access(message: Message) -> None:
        await message.answer(f"Нет доступа. Ваш Telegram ID: <code>{message.from_user.id}</code>")

    @router.callback_query(~is_admin)
    async def no_access_cb(callback: CallbackQuery) -> None:
        await callback.answer("Нет доступа", show_alert=True)

    # --- меню -------------------------------------------------------------

    async def menu(event: Message | CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await show(
            event,
            "<b>MIRACLE GLOW — админка</b>",
            kb(
                [("📊 Статистика", "stats"), ("🔗 Источники", "refs")],
                [("🤖 Боты", "bots"), ("🌐 Тексты и языки", "texts")],
                [("📥 Экспорт участников", "export"), ("👤 Участник", "user")],
                [("🔄 Перепроверить подписки", "recheck")],
            ),
        )

    @router.message(Command("start", "admin", "cancel"))
    async def on_menu_cmd(message: Message, state: FSMContext) -> None:
        await menu(message, state)

    @router.callback_query(F.data == "menu")
    async def on_menu_cb(callback: CallbackQuery, state: FSMContext) -> None:
        await menu(callback, state)

    # --- статистика -------------------------------------------------------

    @router.callback_query(F.data == "stats")
    async def on_stats(callback: CallbackQuery, db: Database) -> None:
        s = await db.stats()
        pools = [b["source"] for b in await db.list_bots()]
        pools += sorted((set(s["participants"]) | set(s["started"])) - set(pools))
        lines = [f"<b>Участников всего: {sum(s['participants'].values())}</b>", ""]
        for pool in pools:
            started = s["started"].get(pool, 0)
            joined = s["participants"].get(pool, 0)
            conv = f"{joined / started:.0%}" if started else "—"
            lines.append(
                f"<b>{escape(pool)}</b>: {joined} участн. "
                f"(за 24 ч: +{s['last_24h'].get(pool, 0)})\n"
                f"   нажали Start: {started}, конверсия: {conv}, "
                f"отписались: {s['unsubscribed'].get(pool, 0)}"
            )
        if not pools:
            lines.append("Пока нет ни ботов, ни участников.")
        lines += ["", "<i>«Отписались» обновляется кнопкой «Перепроверить подписки».</i>"]
        await show(callback, "\n".join(lines), kb([("🔄 Обновить", "stats")], BACK_MENU))

    @router.callback_query(F.data == "refs")
    async def on_refs(callback: CallbackQuery, db: Database) -> None:
        lines = ["<b>Источники (метки deep link)</b>", ""]
        pool = None
        for r in await db.ref_stats():
            if r["pool"] != pool:
                pool = r["pool"]
                lines.append(f"<b>{escape(pool)}</b>")
            name = f"<code>{escape(r['ref'])}</code>" if r["ref"] else "без метки"
            lines.append(f"   {name}: Start — {r['started']}, участников — {r['joined']}")
        if pool is None:
            lines.append("Пока никто не запускал ботов.")
        lines += [
            "",
            "Метка задаётся в ссылке на бота: <code>t.me/бот?start=метка</code> "
            "(латиница, цифры, «_» и «-», до 64 символов). "
            "Запоминается первая метка, с которой пришёл пользователь.",
        ]
        text = "\n".join(lines)
        if len(text) > 4000:
            text = text[:3900] + "\n…\nПолный список — в экспорте (колонка ref)."
        await show(callback, text, kb([("🔄 Обновить", "refs")], BACK_MENU))

    # --- боты -------------------------------------------------------------

    @router.callback_query(F.data == "bots")
    async def on_bots(callback: CallbackQuery, db: Database, manager: BotManager) -> None:
        rows = []
        for b in await db.list_bots():
            mark = "🟢" if manager.is_running(b["source"]) else ("🔴" if b["is_active"] else "⏸")
            rows.append(
                [(f"{mark} {b['source']} · {b['language']} — @{b['username']}", f"bot:{b['source']}")]
            )
        await show(
            callback,
            "<b>Боты розыгрыша</b>\n\n🟢 работает · ⏸ выключен · 🔴 включён, но не запущен (ошибка)",
            kb(*rows, [("➕ Добавить бота", "bot_add")], BACK_MENU),
        )

    async def bot_card(event, db: Database, texts: Texts, manager: BotManager, source: str) -> None:
        b = await db.get_bot(source)
        if not b:
            await show(event, "Бот не найден.", kb([("« Боты", "bots")]))
            return
        if manager.is_running(source):
            status = "🟢 работает"
        elif b["is_active"]:
            status = "🔴 не запущен — проверьте токен"
        else:
            status = "⏸ выключен"
        count = len(await db.list_participants(source))
        await show(
            event,
            f"<b>Бот {escape(source)}</b> — @{escape(b['username'] or '?')}\n\n"
            f"Статус: {status}\n"
            f"Язык: {escape(texts.languages.get(b['language'], b['language']))}\n"
            f"Канал: <code>{escape(b['channel_id'])}</code>\n"
            f"Ссылка: {escape(b['channel_url'])}\n"
            f"Участников в пуле: {count}\n\n"
            f"Ссылка с меткой источника:\n<code>https://t.me/{escape(b['username'] or '')}?start=МЕТКА</code>",
            kb(
                [("⏸ Выключить" if b["is_active"] else "▶️ Включить", f"bot_toggle:{source}")],
                [("🌐 Изменить язык", f"bot_lang:{source}"), ("✏️ Изменить канал", f"bot_chan:{source}")],
                [("🗑 Удалить", f"bot_del:{source}")],
                [("« Боты", "bots")],
            ),
        )

    @router.callback_query(F.data.startswith("bot:"))
    async def on_bot(callback: CallbackQuery, db: Database, texts: Texts, manager: BotManager) -> None:
        await bot_card(callback, db, texts, manager, callback.data.split(":", 1)[1])

    @router.callback_query(F.data.startswith("bot_lang:"))
    async def on_bot_lang(callback: CallbackQuery, texts: Texts) -> None:
        source = callback.data.split(":", 1)[1]
        rows = [[(title, f"bl:{source}:{code}")] for code, title in texts.languages.items()]
        await show(
            callback,
            f"<b>Язык бота {escape(source)}</b>\n\nБот будет отвечать только на выбранном языке.",
            kb(*rows, [("Отмена", f"bot:{source}")]),
        )

    @router.callback_query(F.data.startswith("bl:"))
    async def on_bot_lang_set(
        callback: CallbackQuery, db: Database, texts: Texts, manager: BotManager
    ) -> None:
        _, source, code = callback.data.split(":", 2)
        row = await db.get_bot(source)
        if row and code in texts.languages and code != row["language"]:
            await db.set_bot_language(source, code)
            if manager.is_running(source):
                await manager.start(await db.get_bot(source))  # перезапуск с новым языком
                await asyncio.sleep(1)
        await bot_card(callback, db, texts, manager, source)

    @router.callback_query(F.data.startswith("bot_toggle:"))
    async def on_bot_toggle(
        callback: CallbackQuery, db: Database, texts: Texts, manager: BotManager
    ) -> None:
        source = callback.data.split(":", 1)[1]
        b = await db.get_bot(source)
        if b:
            await db.set_bot_active(source, not b["is_active"])
            if b["is_active"]:
                await manager.stop(source)
            else:
                await manager.start(b)
                await asyncio.sleep(1)  # дать поллингу стартовать, чтобы показать верный статус
        await bot_card(callback, db, texts, manager, source)

    @router.callback_query(F.data.startswith("bot_del:"))
    async def on_bot_del(callback: CallbackQuery) -> None:
        source = callback.data.split(":", 1)[1]
        await show(
            callback,
            f"Удалить бота <b>{escape(source)}</b>?\n\n"
            "Бот перестанет отвечать. Участники пула останутся в базе и в экспорте.",
            kb([("Да, удалить", f"bot_del_ok:{source}")], [("Отмена", f"bot:{source}")]),
        )

    @router.callback_query(F.data.startswith("bot_del_ok:"))
    async def on_bot_del_ok(callback: CallbackQuery, db: Database, manager: BotManager) -> None:
        source = callback.data.split(":", 1)[1]
        await manager.stop(source)
        await db.delete_bot(source)
        await on_bots(callback, db, manager)

    # добавление бота

    @router.callback_query(F.data == "bot_add")
    async def on_bot_add(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(AddBot.source)
        await show(
            callback,
            "<b>Новый бот · шаг 1/5</b>\n\nПришлите код пула (source_channel), "
            "например <code>RU</code>, <code>EN</code>, <code>PT</code>, <code>ES</code>.\n"
            "Латиница и цифры, до 10 символов.",
            CANCEL,
        )

    @router.message(AddBot.source, F.text)
    async def add_bot_source(message: Message, state: FSMContext, db: Database) -> None:
        source = message.text.strip().upper()
        if not SOURCE_RE.match(source):
            await message.answer("Код — латиница/цифры, до 10 символов. Попробуйте ещё раз.", reply_markup=CANCEL)
            return
        if await db.get_bot(source):
            await message.answer("Бот с таким кодом уже есть. Пришлите другой код.", reply_markup=CANCEL)
            return
        await state.update_data(source=source)
        await state.set_state(AddBot.token)
        await message.answer(
            "<b>Шаг 2/5</b>\n\nПришлите токен бота от @BotFather. "
            "Сообщение с токеном я удалю из чата.",
            reply_markup=CANCEL,
        )

    @router.message(AddBot.token, F.text)
    async def add_bot_token(message: Message, state: FSMContext, texts: Texts) -> None:
        token = message.text.strip()
        try:
            await message.delete()
        except TelegramAPIError:
            pass
        if not TOKEN_RE.match(token):
            await message.answer("Это не похоже на токен. Пришлите токен ещё раз.", reply_markup=CANCEL)
            return
        probe = Bot(token)
        try:
            me = await probe.get_me()
        except TelegramAPIError:
            await message.answer("Telegram не принял этот токен. Пришлите корректный.", reply_markup=CANCEL)
            return
        finally:
            await probe.session.close()
        await state.update_data(token=token, username=me.username)
        await state.set_state(AddBot.language)
        rows = [[(title, f"al:{code}")] for code, title in texts.languages.items()]
        await message.answer(
            f"Бот найден: @{me.username}\n\n<b>Шаг 3/5</b>\n\n"
            "Выберите язык бота. Бот будет отвечать только на нём.",
            reply_markup=kb(*rows, [("Отмена", "menu")]),
        )

    @router.callback_query(AddBot.language, F.data.startswith("al:"))
    async def add_bot_language(callback: CallbackQuery, state: FSMContext, texts: Texts) -> None:
        code = callback.data.split(":", 1)[1]
        if code not in texts.languages:
            await callback.answer("Язык не найден")
            return
        username = (await state.get_data())["username"]
        await state.update_data(language=code)
        await state.set_state(AddBot.channel_id)
        await show(
            callback,
            f"Язык: {escape(texts.languages[code])}\n\n<b>Шаг 4/5</b>\n\n"
            "Пришлите канал для проверки подписки: <code>@username</code> "
            "или числовой id (<code>-100…</code>).\n\n"
            f"⚠️ @{username} должен быть администратором этого канала, "
            "иначе проверка подписки не работает.",
            CANCEL,
        )

    def read_channel_id(text: str) -> str | None:
        value = text.strip()
        if re.match(r"^@\w{4,}$", value) or re.match(r"^-100\d+$", value):
            return value
        return None

    @router.message(AddBot.channel_id, F.text)
    @router.message(EditChannel.channel_id, F.text)
    async def set_channel_id(message: Message, state: FSMContext) -> None:
        channel_id = read_channel_id(message.text)
        if not channel_id:
            await message.answer("Нужен <code>@username</code> или id вида <code>-100…</code>.", reply_markup=CANCEL)
            return
        await state.update_data(channel_id=channel_id)
        editing = await state.get_state() == EditChannel.channel_id.state
        await state.set_state(EditChannel.channel_url if editing else AddBot.channel_url)
        await message.answer(
            ("" if editing else "<b>Шаг 5/5</b>\n\n")
            + "Пришлите ссылку для кнопки «Подписаться», например <code>https://t.me/channel</code>.",
            reply_markup=CANCEL,
        )

    async def channel_warning(token: str, channel_id: str) -> str:
        """Пустая строка, если бот — администратор канала."""
        probe = Bot(token)
        try:
            member = await probe.get_chat_member(parse_channel_id(channel_id), probe.id)
            if member.status in ("administrator", "creator"):
                return ""
        except TelegramAPIError:
            pass
        finally:
            await probe.session.close()
        return (
            "\n\n⚠️ Бот не является администратором канала (или канал не найден). "
            "Добавьте его в администраторы — до этого проверка подписки будет выдавать ошибку."
        )

    @router.message(AddBot.channel_url, F.text)
    @router.message(EditChannel.channel_url, F.text)
    async def set_channel_url(
        message: Message, state: FSMContext, db: Database, manager: BotManager
    ) -> None:
        url = message.text.strip()
        if not re.match(r"^https://t\.me/\S+$", url):
            await message.answer("Ссылка должна начинаться с <code>https://t.me/</code>.", reply_markup=CANCEL)
            return
        data = await state.get_data()
        editing = await state.get_state() == EditChannel.channel_url.state
        source = data["source"]
        if editing:
            await db.update_bot_channel(source, data["channel_id"], url)
            head = f"Канал бота <b>{escape(source)}</b> обновлён."
        else:
            if not await db.add_bot(
                source, data["token"], data["username"], data["channel_id"], url, data["language"]
            ):
                await state.clear()
                await message.answer(
                    "Бот с таким кодом или токеном уже добавлен.", reply_markup=kb([("« Боты", "bots")])
                )
                return
            head = f"Бот <b>{escape(source)}</b> (@{data['username']}) создан и запущен ✅"
        await state.clear()
        row = await db.get_bot(source)
        if row["is_active"]:
            await manager.start(row)
        warning = await channel_warning(row["token"], row["channel_id"])
        await message.answer(head + warning, reply_markup=kb([("Открыть бота", f"bot:{source}")], BACK_MENU))

    @router.callback_query(F.data.startswith("bot_chan:"))
    async def on_bot_chan(callback: CallbackQuery, state: FSMContext) -> None:
        source = callback.data.split(":", 1)[1]
        await state.set_state(EditChannel.channel_id)
        await state.update_data(source=source)
        await show(
            callback,
            f"<b>Канал бота {escape(source)}</b>\n\nПришлите новый канал: "
            "<code>@username</code> или id (<code>-100…</code>).",
            CANCEL,
        )

    # --- тексты и языки ---------------------------------------------------

    @router.callback_query(F.data == "texts")
    async def on_texts(callback: CallbackQuery, state: FSMContext, texts: Texts) -> None:
        await state.clear()
        rows = [[(title, f"tl:{code}")] for code, title in texts.languages.items()]
        await show(
            callback,
            "<b>Тексты и языки</b>\n\nВыберите язык, чтобы править его фразы. "
            "Язык каждому боту назначается в разделе «Боты».",
            kb(*rows, [("➕ Добавить язык", "lang_add")], BACK_MENU),
        )

    @router.callback_query(F.data.startswith("tl:"))
    async def on_text_language(callback: CallbackQuery, texts: Texts) -> None:
        language = callback.data.split(":", 1)[1]
        if language not in texts.languages:
            await callback.answer("Язык не найден")
            return
        rows = [
            [(("✏️ " if texts.is_overridden(language, key) else "") + label, f"tk:{language}:{key}")]
            for key, label in KEY_LABELS.items()
        ]
        await show(
            callback,
            f"<b>{escape(texts.languages[language])}</b> (<code>{language}</code>)\n\n"
            "✏️ — фраза изменена относительно стандартной.",
            kb(*rows, [("🗑 Удалить язык", f"lang_del:{language}")], [("« Языки", "texts")]),
        )

    async def text_card(event, texts: Texts, language: str, key: str) -> None:
        back = ("« Назад", f"tl:{language}")
        state_note = "изменён" if texts.is_overridden(language, key) else "стандартный"
        await show(
            event,
            f"<b>{text_label(language, key)}</b> — {state_note}\n"
            f"────────────\n{texts.get(language, key)}",
            kb(
                [("✏️ Изменить", f"te:{language}:{key}"), ("↩️ Сбросить", f"tr:{language}:{key}")],
                [back],
            ),
        )

    def parse_ref(data: str) -> tuple[str, str]:
        _, language, key = data.split(":", 2)
        return language, key

    @router.callback_query(F.data.startswith("tk:"))
    async def on_text(callback: CallbackQuery, state: FSMContext, texts: Texts) -> None:
        await state.clear()
        language, key = parse_ref(callback.data)
        if not valid_text_ref(texts, language, key):
            await callback.answer("Текст не найден")
            return
        await text_card(callback, texts, language, key)

    @router.callback_query(F.data.startswith("tr:"))
    async def on_text_reset(callback: CallbackQuery, texts: Texts) -> None:
        language, key = parse_ref(callback.data)
        if valid_text_ref(texts, language, key):
            await texts.reset(language, key)
            await text_card(callback, texts, language, key)
        else:
            await callback.answer("Текст не найден")

    @router.callback_query(F.data.startswith("te:"))
    async def on_text_edit(callback: CallbackQuery, state: FSMContext, texts: Texts) -> None:
        language, key = parse_ref(callback.data)
        if not valid_text_ref(texts, language, key):
            await callback.answer("Текст не найден")
            return
        await state.set_state(EditText.value)
        await state.update_data(language=language, key=key)
        hint = (
            "Для кнопки — короткий текст без форматирования."
            if key.startswith("btn_")
            else "Форматирование (жирный, курсив, ссылки) сохранится как в вашем сообщении."
        )
        await show(
            callback,
            f"<b>{text_label(language, key)}</b>\n\nПришлите новый текст одним сообщением.\n{hint}",
            kb([("Отмена", f"tk:{language}:{key}")]),
        )

    @router.message(EditText.value, F.text)
    async def on_text_value(message: Message, state: FSMContext, texts: Texts) -> None:
        data = await state.get_data()
        language, key = data["language"], data["key"]
        value = message.text.strip() if key.startswith("btn_") else message.html_text.strip()
        if key.startswith("btn_") and len(value) > 60:
            await message.answer("Текст кнопки — до 60 символов. Пришлите короче.")
            return
        await texts.set(language, key, value)
        await state.clear()
        await message.answer("Сохранено ✅ Изменение уже действует в ботах на этом языке.")
        await text_card(message, texts, language, key)

    # языки

    @router.callback_query(F.data == "lang_add")
    async def on_lang_add(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(AddLanguage.code)
        await show(
            callback,
            "<b>Новый язык · шаг 1/2</b>\n\nПришлите код языка латиницей, "
            "например <code>de</code> или <code>tr</code>.",
            kb([("Отмена", "texts")]),
        )

    @router.message(AddLanguage.code, F.text)
    async def on_lang_code(message: Message, state: FSMContext, texts: Texts) -> None:
        code = message.text.strip().lower()
        if not LANG_RE.match(code) or code in texts.languages:
            await message.answer("Нужен новый код из 2–5 латинских букв. Попробуйте ещё раз.")
            return
        await state.update_data(code=code)
        await state.set_state(AddLanguage.title)
        await message.answer(
            "<b>Шаг 2/2</b>\n\nПришлите название языка для админки, "
            "например <code>🇩🇪 Deutsch</code>."
        )

    @router.message(AddLanguage.title, F.text)
    async def on_lang_title(message: Message, state: FSMContext, texts: Texts) -> None:
        title = message.text.strip()[:40]
        code = (await state.get_data())["code"]
        await state.clear()
        await texts.add_language(code, title)
        await message.answer(
            f"Язык {escape(title)} добавлен ✅\n\n"
            "Пока фразы не переведены, для него показываются английские тексты — "
            "задайте свои в его разделе, затем назначьте язык боту.",
            reply_markup=kb([("Открыть фразы", f"tl:{code}")], [("« Языки", "texts")]),
        )

    @router.callback_query(F.data.startswith("lang_del:"))
    async def on_lang_del(callback: CallbackQuery, db: Database, texts: Texts) -> None:
        code = callback.data.split(":", 1)[1]
        used_by = [b["source"] for b in await db.list_bots() if b["language"] == code]
        if used_by:
            await callback.answer(
                f"Язык используется ботами: {', '.join(used_by)}. Сначала смените им язык.",
                show_alert=True,
            )
            return
        if len(texts.languages) <= 1:
            await callback.answer("Нельзя удалить единственный язык", show_alert=True)
            return
        await show(
            callback,
            f"Удалить язык <b>{escape(texts.languages.get(code, code))}</b> и все правки его фраз?",
            kb([("Да, удалить", f"lang_del_ok:{code}")], [("Отмена", f"tl:{code}")]),
        )

    @router.callback_query(F.data.startswith("lang_del_ok:"))
    async def on_lang_del_ok(
        callback: CallbackQuery, state: FSMContext, db: Database, texts: Texts
    ) -> None:
        code = callback.data.split(":", 1)[1]
        in_use = any(b["language"] == code for b in await db.list_bots())
        if len(texts.languages) > 1 and not in_use:
            await texts.delete_language(code)
        await on_texts(callback, state, texts)

    # --- участник: поиск и удаление ---------------------------------------

    @router.callback_query(F.data == "user")
    async def on_user(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(FindUser.telegram_id)
        me = callback.from_user.id
        await show(
            callback,
            "<b>Участник</b>\n\nПришлите Telegram ID пользователя, чтобы посмотреть его "
            "или удалить (например, для повторного теста).",
            kb([(f"Это я ({me})", f"user:{me}")], [("Отмена", "menu")]),
        )

    async def user_card(event, db: Database, telegram_id: int) -> None:
        p = await db.get_participant(telegram_id)
        visits = await db.list_user_rows(telegram_id)
        lines = [f"<b>Пользователь</b> <code>{telegram_id}</code>", ""]
        if p:
            lines += [
                f"Участвует в пуле <b>{escape(p['source_channel'])}</b>",
                f"Username: {'@' + escape(p['username']) if p['username'] else '—'}",
                f"Код: <code>{escape(p['code'] or '—')}</code>",
                f"Источник: <code>{escape(p['ref'] or '—')}</code>",
                f"Подписка: {p['subscription_status']}",
                f"Зарегистрирован: {p['registered_at']} UTC",
            ]
        else:
            lines.append("Не участвует в розыгрыше.")
        if visits:
            lines += ["", "Заходил в ботов: " + ", ".join(escape(v["source_channel"]) for v in visits)]
        if not p and not visits:
            await show(event, "\n".join(lines), kb([("Найти другого", "user")], BACK_MENU))
            return
        lines += ["", "Удаление стирает участие и историю входов — пользователь сможет пройти всё заново."]
        await show(
            event,
            "\n".join(lines),
            kb([("🗑 Удалить", f"user_del:{telegram_id}")], [("Найти другого", "user")], BACK_MENU),
        )

    @router.message(FindUser.telegram_id, F.text)
    async def on_user_id(message: Message, state: FSMContext, db: Database) -> None:
        raw = message.text.strip()
        if not raw.isdigit():
            await message.answer("Telegram ID — это число. Пришлите ещё раз.", reply_markup=CANCEL)
            return
        await state.clear()
        await user_card(message, db, int(raw))

    @router.callback_query(F.data.startswith("user:"))
    async def on_user_card(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
        await state.clear()
        await user_card(callback, db, int(callback.data.split(":", 1)[1]))

    @router.callback_query(F.data.startswith("user_del:"))
    async def on_user_del(callback: CallbackQuery, db: Database) -> None:
        telegram_id = int(callback.data.split(":", 1)[1])
        deleted = await db.delete_user(telegram_id)
        await show(
            callback,
            f"Пользователь <code>{telegram_id}</code> удалён ✅ Он может пройти регистрацию заново."
            if deleted
            else f"Пользователь <code>{telegram_id}</code> уже удалён.",
            kb([("Найти другого", "user")], BACK_MENU),
        )

    # --- экспорт ----------------------------------------------------------

    @router.callback_query(F.data == "export")
    async def on_export(callback: CallbackQuery, db: Database) -> None:
        s = await db.stats()
        rows = [[(f"{pool} ({count})", f"exp:{pool}")] for pool, count in sorted(s["participants"].items())]
        await show(
            callback,
            "<b>Экспорт участников (CSV)</b>\n\nВыберите пул.",
            kb(*rows, [("Все пулы одним файлом", "exp:*")], BACK_MENU),
        )

    @router.callback_query(F.data.startswith("exp:"))
    async def on_export_pool(callback: CallbackQuery, db: Database) -> None:
        pool = callback.data.split(":", 1)[1]
        participants = await db.list_participants(None if pool == "*" else pool)
        if not participants:
            await callback.answer("Участников пока нет", show_alert=True)
            return
        columns = [
            "telegram_id", "username", "full_name", "bot_language", "source_channel",
            "subscription_status", "participation_status", "registered_at", "code", "ref",
        ]
        buffer = io.StringIO()
        writer = csv.writer(buffer, delimiter=";")
        writer.writerow(columns)
        for p in participants:
            writer.writerow([p[c] for c in columns])
        name = f"miracle_glow_{'all' if pool == '*' else pool}.csv"
        await callback.message.answer_document(
            BufferedInputFile(buffer.getvalue().encode("utf-8-sig"), name),
            caption=f"Участников: {len(participants)}. Время регистрации — UTC.",
        )
        await callback.answer()

    # --- перепроверка подписок --------------------------------------------

    async def recheck(admin_bot: Bot, chat_id: int, db: Database, manager: BotManager) -> None:
        checked = unsubscribed = failed = 0
        skipped: set[str] = set()
        for p in await db.list_participants():
            bot = manager.bot(p["source_channel"]) if manager.is_running(p["source_channel"]) else None
            row = await db.get_bot(p["source_channel"])
            if not bot or not row:
                skipped.add(p["source_channel"])
                continue
            try:
                ok = await is_subscribed(bot, parse_channel_id(row["channel_id"]), p["telegram_id"])
            except TelegramAPIError:
                failed += 1
                continue
            await db.set_subscription_status(p["telegram_id"], "subscribed" if ok else "unsubscribed")
            checked += 1
            unsubscribed += not ok
            await asyncio.sleep(0.05)  # не упираться в лимиты Telegram
        text = (
            f"<b>Перепроверка подписок завершена</b>\n\n"
            f"Проверено: {checked}\nОтписались: {unsubscribed}\nОшибок проверки: {failed}"
        )
        if skipped:
            text += f"\nПропущены пулы без работающего бота: {', '.join(sorted(skipped))}"
        await admin_bot.send_message(chat_id, text, reply_markup=kb([("📊 Статистика", "stats")], BACK_MENU))

    @router.callback_query(F.data == "recheck")
    async def on_recheck(callback: CallbackQuery, bot: Bot, db: Database, manager: BotManager) -> None:
        if any(t.get_name() == "recheck" for t in _background):
            await callback.answer("Перепроверка уже идёт", show_alert=True)
            return
        task = asyncio.create_task(recheck(bot, callback.message.chat.id, db, manager), name="recheck")
        _background.add(task)
        task.add_done_callback(_background.discard)
        await callback.answer("Запущено. Пришлю результат, когда закончу.", show_alert=True)

    return router

import asyncio
import os
import pdfkit
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, BufferedInputFile
import aiohttp
import uvicorn
from fastapi import FastAPI, Request
from decouple import config
from cachetools import TTLCache
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart, Command
from aiogram.fsm.state import State, StatesGroup
from aiogram import F

app = FastAPI()
EVRAZ_API_URL = config("EVRAZ_API_URL")
TOKEN = config("TELEGRAM_TOKEN")
bot = Bot(token=TOKEN)
dp = Dispatcher()
cache = TTLCache(maxsize=10000, ttl=3600)
instructions = dict()

pdfkit_options = {
    'page-size': 'A4',
    'orientation': 'landscape'
}

class Form(StatesGroup):
    set_instructions_state = State()
    default_state = State()

@app.post("/webhook")
async def handle_webhook(request: Request):
    data = await request.json()
    if "request_id" not in data or "report_content" not in data:
        request_id = data["request_id"]
        if request_id in cache:
            chat_data = cache[request_id]
            status = data.get("status", "unknown")
            chat_id = chat_data["chat_id"]
            message_id = chat_data["message_id"]
            await bot.send_message(
                chat_id=chat_id,
                reply_to_message_id=message_id,
                text=f"⚠️ К сожалению, ваш запрос обработан с ошибкой.\nСтатус запроса: {status}."
            )

    request_id = data["request_id"]

    if request_id not in cache:
        return

    chat_data = cache[request_id]
    chat_id = chat_data["chat_id"]
    message_id = chat_data["message_id"]

    input_file = BufferedInputFile(
        file=pdfkit.from_url(f"{EVRAZ_API_URL}/reports/{request_id}", False, options=pdfkit_options),
        filename="report.pdf"
    )

    await bot.send_document(
        chat_id=chat_id,
        document=input_file,
        reply_to_message_id=message_id,
        caption="☑️ Ваш отчет успешно составлен."
    )


@dp.message(Command("set_instr"))
async def set_instructions_handler(message: types.Message, state: FSMContext):
    if message.document is not None:
        await set_instructions(message)
        await state.set_state(Form.default_state)
    else:
        await state.set_state(Form.set_instructions_state)
        await message.reply("📝 Отправьте файл с инструкциями в формате PDF.")


async def set_instructions(message: types.Message):
    if message.document is not None:
        file_url = await get_file_url(message.document)
        has_prev = message.from_user.id in instructions
        instructions[message.from_user.id] = file_url
        await message.reply(
            f"☑️ Инструкции успешно {'обновлены' if has_prev else 'установлены'}."
        )


@dp.message(Form.set_instructions_state)
async def set_instructions_state_handler(message: types.Message, state: FSMContext):
    if message.document is None:
        await message.reply("⚠️ Пожалуйста, отправьте файл с инструкциями в формате PDF.")
        return
    if not await is_pdf_document(message.document):
        await message.reply("⚠️ Неверный формат. Пожалуйста, отправьте файл в формате PDF.")
        return
    await set_instructions(message)
    await state.set_state(Form.default_state)


@dp.message(F.content_type == types.ContentType.DOCUMENT)
async def handle_document_updates(message: types.Message):
    user_id = message.from_user.id
    if user_id not in instructions:
        await message.reply("⚠️ Сначала установите инструкции по ревью проекта, используя /set_instr.")
        return
    if message.document is None:
        await message.reply("⚠️ Не удалось обработать документ.")
        return

    target_file_url = await get_file_url(message.document)
    instructions_file_url = instructions[user_id]
    data = {
        "instructions_file_url": instructions_file_url,
        "target_file_url": target_file_url
    }
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(f"{EVRAZ_API_URL}/upload", json=data, timeout=5) as response:
                if response.status in [200, 202]:
                    response_data = await response.json()
                    cache[response_data["request_id"]] = {
                        "chat_id": message.chat.id,
                        "message_id": message.message_id
                    }
                    await message.reply("⏳ Принято в обработку. Ожидайте.")
                else:
                    await message.reply(f"❌ Ошибка при отправке данных в АПИ. Код ошибки: {response.status}")
        except Exception:
            await message.reply("❌ Произошла неизвестная ошибка.")


@dp.message(CommandStart())
async def start_message(message: Message, state: FSMContext):
    if await state.get_state() == Form.default_state:
        await unknown_command(message, state)
        return
    await message.reply(
        "👋 Привет! Я бот для проверки проектов. "
        "Отправь мне инструкции с требованиями к проекту, а потом сам проект. "
        "Я сформирую отчет с результатами ревью!"
    )
    await state.set_state(Form.set_instructions_state)


@dp.message(F.text)
async def unknown_command(message: Message,  state: FSMContext):
    current_state = await state.get_state()
    if current_state is None:
        await start_message(message, state)
        return
    await message.reply("🫤 Я не знаю, что делать с этим. Пожалуйста, отправьте мне файл или архив для обработки.")


async def get_file_url(document: types.Document):
    file_id = document.file_id
    tg_file = await bot.get_file(file_id)
    file_url = f"https://api.telegram.org/file/bot{TOKEN}/{tg_file.file_path}"
    return file_url


async def is_pdf_document(document: types.Document) -> bool:
    return document.mime_type == "application/pdf"


async def on_start():
    await dp.start_polling(bot)


async def run():
    server = uvicorn.Server(
        uvicorn.Config(app, host="0.0.0.0", port=int(os.getenv("EVRAZ_TG_BOT_PORT", "8010")))
    )

    bot_task = asyncio.create_task(on_start())
    server_task = asyncio.create_task(server.serve())

    await asyncio.gather(server_task, bot_task)

if __name__ == '__main__':
    asyncio.run(run())
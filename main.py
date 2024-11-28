import io
import asyncio
from asyncio import sleep
from aiogram.types import InputFile, Message
import aiohttp
import uvicorn
from fastapi import FastAPI, Request
from decouple import config
from cachetools import TTLCache
from telebot.types import InputFile
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart, Command
from aiogram import F

app = FastAPI()
EVRAZ_API_URL = config("EVRAZ_API_URL")
TOKEN = config("TELEGRAM_TOKEN")
bot = Bot(token=TOKEN)
dp = Dispatcher()
cache = TTLCache(maxsize=1000, ttl=3600)
instructions = dict()


@app.post("/webhook")
async def handle_webhook(request: Request):
    data = await request.json()
    if "request_id" not in data or "report_file_url" not in data:
        if "record_id" in data:
            record_id = data["record_id"]
            if record_id in cache:
                chat_data = cache[record_id]
                status = data.get("status", "unknown")
                chat_id = chat_data["chat_id"]
                message_id = chat_data["message_id"]
                await bot.send_message(chat_id, "Запрос не успешный, отсутствует report_file_url.")
                await bot.send_message(
                    chat_id=chat_id,
                    reply_to_message_id=message_id,
                    text=f"⚠️ К сожалению, ваш запрос обработан с ошибкой.\nСтатус запроса: {status}."
                )
            return
        else:
            return

    request_id = data["request_id"]
    report_file_url = data["report_file_url"]

    if request_id not in cache:
        return

    chat_data = cache[request_id]
    chat_id = chat_data["chat_id"]
    message_id = chat_data["message_id"]

    async with aiohttp.ClientSession() as session:
        async with session.get(report_file_url) as response:
            if response.status != 200:
                return
            file_data = io.BytesIO(await response.read())
    file_data.name = "report.pdf"

    await bot.send_document(
        chat_id=chat_id,
        document=InputFile(file_data),
        reply_to_message_id=message_id,
        caption=f"☑️ Ваш отчет успешно составлен."
    )


@dp.message(Command("set_instr"))
async def set_instructions(message: types.Message):
    if message.document is not None:
        file_url = await get_file_url(message.document)
        instructions[message.from_user.id] = file_url
        await message.reply("☑️ Инструкции успешно установлены.")
    else:
        await message.reply("⚠️ Пожалуйста, отправьте команду вместе с документом с инструкциями.")


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
                if response.status == 200:
                    reply_message = await message.reply("⏳ Принятно в обработку. Ожидайте.")
                    await sleep(3)
                    await reply_message.delete()
                else:
                    await message.reply(f"❌ Ошибка при отправке данных в АПИ. Код ошибки: {response.status}")
        except Exception:
            await message.reply("❌ Произошла неизвестная ошибка.")


@dp.message(CommandStart())
async def start_message(message: Message):
    await message.reply("Привет! Я бот для проверки проектов. Отправьте мне файл или архив для обработки.")


@dp.message(F.text)
async def unknown_command(message: Message):
    await message.reply("Я не знаю, что делать с этим. Пожалуйста, отправьте мне файл или архив для обработки.")


async def get_file_url(document: types.Document):
    file_id = document.file_id
    tg_file = await bot.get_file(file_id)
    file_url = f"https://api.telegram.org/file/bot{TOKEN}/{tg_file.file_path}"
    return file_url


async def on_start():
    await dp.start_polling(bot)


async def run():
    server = uvicorn.Server(
        uvicorn.Config(app, host="0.0.0.0", port=8010)
    )

    bot_task = asyncio.create_task(on_start())
    server_task = asyncio.create_task(server.serve())

    await asyncio.gather(server_task, bot_task)

if __name__ == '__main__':
    asyncio.run(run())
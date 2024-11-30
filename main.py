import asyncio
import logging
import os
import uuid
import zipfile

import pdfkit
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, BufferedInputFile, KeyboardButton, ReplyKeyboardMarkup, BotCommand, \
    BotCommandScopeDefault
import aiohttp
import uvicorn
from fastapi import FastAPI, Request
from decouple import config
from cachetools import TTLCache
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart, Command
from aiogram.fsm.state import State, StatesGroup
from aiogram import F
from starlette.exceptions import HTTPException
from starlette.responses import FileResponse, Response

app = FastAPI()
EVRAZ_API_URL = config("EVRAZ_API_URL")
BOT_API_PORT = config("EVRAZ_TG_BOT_PORT")
BOT_API_HOST = config("EVRAZ_TG_BOT_HOST")
TOKEN = config("TELEGRAM_TOKEN")
bot = Bot(token=TOKEN)
dp = Dispatcher()
cache = TTLCache(maxsize=10000, ttl=3600)
many_files_archives = TTLCache(maxsize=10000, ttl=7200)
instructions = dict()
many_files_dict = dict()

data_dir = os.path.join(os.path.dirname(__file__), 'data')

pdfkit_options = {
    'page-size': 'A4',
    'orientation': 'landscape'
}

many_upload_finish = "✅ Закончить загрузку"
many_upload_cancel = "⛔️ Отмена"

class Form(StatesGroup):
    set_instructions_state = State()
    default_state = State()
    many_files_accepting = State()

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
        caption="✅ Ваш отчет успешно составлен."
    )


@app.api_route(
    "/get/{file_id}",
    methods=["GET", "HEAD"],
    summary="Скачать файл по id",
    status_code=200,
    responses={
        404: {"description": "Файл не найден"},
    }
)
async def get_file(request: Request, file_id: str):
    if file_id not in many_files_archives:
        raise HTTPException(status_code=404, detail="Файл не найден.")

    file_path = many_files_archives[file_id]

    if not os.path.exists(file_path):
        many_files_archives.pop(file_id)
        raise HTTPException(status_code=404, detail="Файл не найден на сервере.")

    if request.method == "HEAD":
        headers = {
            "content-type": "application/zip",
            "content-length": str(os.path.getsize(file_path))
        }
        return Response(status_code=200, headers=headers)

    return FileResponse(path=file_path, media_type='application/zip', filename=os.path.basename(file_path))


@dp.message(Command("upload_many_files"))
async def upload_many_files_handler(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if user_id not in instructions:
        await message.reply("⚠️ Сначала установите инструкции по ревью проекта, используя /set_instr.")
        return
    await state.set_state(Form.many_files_accepting)
    many_files_dict[user_id] = list()
    if message.document is not None:
        many_files_dict[user_id].append(message.document)
    else:
        await message.reply("📔 Отправьте нужные вам файлы.", reply_markup=await main_kb())


@dp.message(Command("set_instr"))
async def set_instructions_handler(message: types.Message, state: FSMContext):
    if message.document is not None:
        await set_instructions(message)
        await state.set_state(Form.default_state)
    else:
        await state.set_state(Form.set_instructions_state)
        await message.reply("📝 Отправьте файл с инструкциями в формате PDF.", reply_markup=types.ReplyKeyboardRemove())


async def set_instructions(message: types.Message):
    if message.document is not None:
        file_url = await get_file_url(message.document)
        has_prev = message.from_user.id in instructions
        instructions[message.from_user.id] = file_url
        await message.reply(
            f"✅ Инструкции успешно {'обновлены' if has_prev else 'установлены'}."
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


async def upload_archive(message: types.Message, target_file_url: str):
    data = {
        "instructions_file_url": instructions[message.from_user.id],
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
                    logging.error(f"Ошибка при отправке данных в АПИ. Код ошибки: {response.status}")
                    response_data = await response.json()
                    if response_data:
                        logging.error(response_data)
                    await message.reply(f"❌ Ошибка при отправке данных в АПИ. Код ошибки: {response.status}")
        except Exception:
            await message.reply("❌ Произошла неизвестная ошибка.")


@dp.message(F.content_type == types.ContentType.DOCUMENT, Form.default_state)
async def handle_document_updates(message: types.Message, state: FSMContext):
    user_id = message.from_user.id

    if (await state.get_state()) == Form.many_files_accepting:
        many_files_dict[user_id].append(message.document)
        return
    if user_id not in instructions:
        await message.reply("⚠️ Сначала установите инструкции по ревью проекта. Отправьте мне PDF-файл.")
        await state.set_state(Form.set_instructions_state)
        return
    if message.document is None:
        await message.reply("⚠️ Не удалось обработать документ.")
        return

    target_file_url = await get_file_url(message.document)
    await upload_archive(message, target_file_url)



@dp.message(CommandStart())
async def start_message(message: Message, state: FSMContext):
    if (await state.get_state()) == Form.default_state:
        await unknown_command(message, state)
        return
    await message.reply(
        "👋 Привет! Я бот для проверки проектов. "
        "Отправь мне инструкции с требованиями к проекту, а потом сам проект. "
        "Я сформирую отчет с результатами ревью!"
    )
    await state.set_state(Form.set_instructions_state)


@dp.message(Form.many_files_accepting, F.text == many_upload_cancel)
async def many_upload_cancel_handler(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    many_files_dict[user_id].clear()
    await state.set_state(Form.default_state)
    await message.reply("Отменено.", reply_markup=types.ReplyKeyboardRemove())


@dp.message(Form.many_files_accepting, F.text == many_upload_finish)
async def many_upload_cancel_handler(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    file_id = await download_and_archive_documents(user_id, many_files_dict[user_id])
    target_file_url = f"http://{BOT_API_HOST}:{BOT_API_PORT}/get/{file_id}"
    await upload_archive(message, target_file_url)
    await state.set_state(Form.default_state)


@dp.message(F.text)
async def unknown_command(message: Message,  state: FSMContext):
    current_state = await state.get_state()
    if current_state is None:
        await start_message(message, state)
        return
    await message.reply("🫤 Я не знаю, что делать с этим. Пожалуйста, отправьте архив для обработки, или воспользуйтесь командой /upload_many_files")


async def get_file_url(document: types.Document):
    file_id = document.file_id
    tg_file = await bot.get_file(file_id)
    file_url = f"https://api.telegram.org/file/bot{TOKEN}/{tg_file.file_path}"
    return file_url


async def is_pdf_document(document: types.Document) -> bool:
    return document.mime_type == "application/pdf"


async def main_kb():
    kb_list = [
        [KeyboardButton(text=many_upload_finish)],
        [KeyboardButton(text=many_upload_cancel)]
    ]
    keyboard = ReplyKeyboardMarkup(keyboard=kb_list, resize_keyboard=True, one_time_keyboard=True, input_field_placeholder="Отправьте еще файлы или воспользуйтесь меню:")
    return keyboard


async def set_commands():
    commands = [BotCommand(command='upload_many_files', description='Загрузить несколько файлов'),
                BotCommand(command='set_instr', description='Установить инструкции')]
    await bot.set_my_commands(commands, BotCommandScopeDefault())



async def download_and_archive_documents(user_id, documents):
    user_dir = os.path.join(data_dir, str(user_id))
    os.makedirs(user_dir, exist_ok=True)

    async with aiohttp.ClientSession() as session:
        for doc in documents:
            file_url = await get_file_url(doc)
            file_name = os.path.join(user_dir, doc.file_name)

            async with session.get(file_url) as resp:
                if resp.status == 200:
                    with open(file_name, 'wb') as f:
                        f.write(await resp.read())
                else:
                    logging.error(f"Ошибка загрузки {file_url}")

    current_file_uuid = str(uuid.uuid4())
    archive_name = f"{current_file_uuid}.zip"
    archive_path = os.path.join(data_dir, str(user_id), archive_name)

    with zipfile.ZipFile(archive_path, 'w') as archive:
        for root, dirs, files in os.walk(user_dir):
            for file in files:
                archive.write(os.path.join(root, file), file)

    many_files_archives[current_file_uuid] = archive_path
    logging.info(f"Created archive with file_id={current_file_uuid}")
    return current_file_uuid


async def on_start():
    await dp.start_polling(bot)


async def run():
    logging.basicConfig(
        level=logging.INFO,
    )
    server = uvicorn.Server(
        uvicorn.Config(app, host="0.0.0.0", port=int(os.getenv("EVRAZ_TG_BOT_PORT", "8010")))
    )

    bot_task = asyncio.create_task(on_start())
    server_task = asyncio.create_task(server.serve())

    await set_commands()
    await asyncio.gather(server_task, bot_task)

if __name__ == '__main__':
    asyncio.run(run())
import base64
import enum
import sys
import requests
import argparse

import telebot.apihelper
from telebot import TeleBot
from telebot.types import *
from telebot import apihelper

from telegram_bot_pagination import InlineKeyboardPaginator


class State(enum.Enum):
    WAIT = 0
    NOTE = 1
    POST = 2


class UserInfo:
    def __init__(self, user_id, chat_id, state):
        self.user = user_id
        self.chat = chat_id
        self.state = state
        self.note = ""
        self.notes_pages = []

    def set_state(self, state):
        if state is State.WAIT:
            self.note = ""
        if state is State.NOTE:
            assert (self.note == "")
        self.state = state

    def add_to_note(self, text):
        self.note += text + "\n"

    def set_notes_pages(self, notes):
        self.notes_pages = notes

    def notes(self):
        return self.notes_pages


class Provider:
    def __init__(self, host: str):
        assert (":" in host)
        self.host = host

    @staticmethod
    def _post_url(url, params):
        import json
        from types import SimpleNamespace
        response = json.loads(requests.get(url, params=params).content, object_hook=lambda x: SimpleNamespace(**x))
        if response.status != 0:
            raise Exception(response.error)
        return response

    def get_all_notes(self, user):
        url = self.host + "/notes/get/all"
        response = self._post_url(url, params={})
        return [x.content for x in response.notes]

    def post_with_geo(self, user, text, x, y):
        url = self.host + "/notes/create/geo"
        params = {"content": text, "x": str(x), "y": str(y)}
        self._post_url(url, params)

    def post_without_geo(self, user, text):
        url = self.host + "/notes/create/nogeo"
        params = {"content": text}
        self._post_url(url, params)


class ImageProvider:
    def __init__(self, host):
        self.host = host

    @staticmethod
    def _post_url(url, body):
        return requests.post(url, json=body)

    def generate(self, text):
        data = {
            "prompt": str(text),
            "width": 512,
            "height": 512,
            "steps": 60,
            "cfg_scale": 5,
            "seed": 0
        }
        print(str(text))
        res = self._post_url(self.host + "/sdapi/v1/txt2img", data)
        return res.json()['images'][0]


class Bot:
    def __init__(self, token, server_host, image_host):
        # required for middleware, must be called before init of TeleBot
        apihelper.ENABLE_MIDDLEWARE = True
        self.bot = TeleBot(token)
        self.user_info = dict()

        self.provider = Provider(server_host)
        self.image_provider = ImageProvider(image_host)
        self.setup()

    def setup(self):
        bot = self.bot

        #######################################################################################
        # Pagination for checking out my_notes

        @bot.middleware_handler(update_types=['message'])
        def init_user_info_if_required(_, msg: Message):
            user = msg.from_user.id
            if user not in self.user_info.keys():
                self.user_info[user] = UserInfo(user, msg.chat.id, State.WAIT)

        @bot.message_handler(commands=['my_notes'])
        def all_notes_pages(msg: Message):
            user = msg.from_user.id
            self.user_info[user].set_notes_pages(self.provider.get_all_notes(user))
            send_note(user, msg.chat.id, 1)

        @bot.callback_query_handler(func=lambda callback: callback.data.split('#')[0] == 'character')
        def characters_page_callback(callback: CallbackQuery):
            bot.delete_message(callback.message.chat.id, callback.message.message_id)
            send_note(callback.from_user.id, callback.message.chat.id, int(callback.data.split('#')[1]))

        def send_note(user, chat, page):
            notes = self.user_info[user].notes_pages
            paginator = InlineKeyboardPaginator(len(notes), current_page=page, data_pattern='character#{page}')
            bot.send_message(chat, notes[page - 1], reply_markup=paginator.markup)

        #######################################################################################
        # Other logic: saving notes, posting

        @bot.message_handler(commands=['start'], func=lambda msg: self.user_info[msg.from_user.id].state == State.WAIT)
        def on_start(msg: Message):
            bot.reply_to(msg, "Welcome! Try /note first. You can use /my_notes to load all your notes")

        @bot.message_handler(commands=['note'], func=lambda msg: self.user_info[msg.from_user.id].state == State.WAIT)
        def on_note(msg: Message):
            self.user_info[msg.from_user.id].set_state(State.NOTE)
            bot.reply_to(msg, "Type anything you want and after use /post or /cancel")

        @bot.message_handler(commands=['cancel'], func=lambda msg: self.user_info[msg.from_user.id].state == State.NOTE \
                                                                   or self.user_info[
                                                                       msg.from_user.id].state == State.POST)
        def on_cancel(msg: Message):
            self.user_info[msg.from_user.id].set_state(State.WAIT)
            bot.reply_to(msg, "Canceled, dropping messages")  # TODO: may be not drop, but let user write another msg

        @bot.message_handler(commands=['post'], func=lambda msg: self.user_info[msg.from_user.id].state == State.NOTE)
        def on_post(msg: Message):
            user = msg.from_user.id
            self.user_info[user].set_state(State.POST)

            markup = ReplyKeyboardMarkup(row_width=2)
            markup.add(KeyboardButton(text="/post_with_geo", request_location=True))
            markup.add(KeyboardButton(text="/post_without_geo"))
            bot.reply_to(msg, text="Choose whatever send with geomark or without", reply_markup=markup)

        @bot.message_handler(func=lambda msg: self.user_info[msg.from_user.id].state == State.NOTE)
        def on_echo_note(msg: Message):
            self.user_info[msg.from_user.id].add_to_note(msg.text)
            bot.reply_to(msg, "Recorded")

        @bot.message_handler(commands=['post_with_geo'],
                             func=lambda msg: self.user_info[msg.from_user.id].state == State.POST)
        def on_post_with_geo(msg):
            (x, y) = msg.location
            user = msg.from_user.id
            text = self.user_info[user].note

            self.provider.post_with_geo(user, text, x, y)
            self.user_info[user].set_state(State.WAIT)
            bot.reply_to(msg, "Done", reply_markup=ReplyKeyboardRemove())
            self.send_picture(text, msg.chat.id)

        @bot.message_handler(commands=['post_without_geo'],
                             func=lambda msg: self.user_info[msg.from_user.id].state == State.POST)
        def on_post_without_geo(msg):
            user = msg.from_user.id
            text = self.user_info[user].note

            self.provider.post_without_geo(user, text)
            self.user_info[user].set_state(State.WAIT)
            bot.reply_to(msg, "Done", reply_markup=ReplyKeyboardRemove())
            self.send_picture(text, msg.chat.id)

    def start(self):
        self.bot.infinity_polling()

    def send_picture(self, prompt, chat_id):
        img = self.image_provider.generate(prompt)
        telebot.apihelper.send_photo(self.bot.token, chat_id, base64.decodebytes(bytes(img, 'utf-8')))


def main(argv):
    parser = argparse.ArgumentParser(description="TeleBot for interaction with GeoNote. Work in progress.")
    parser.add_argument("--host-server", help="server address in format like http://example.com or http://ip:port",
                        default="http://localhost:7861")
    parser.add_argument("--host-image",
                        help="picture server address in format like http://example.com or http://ip:port",
                        default="http://localhost:7860")
    parser.add_argument("token", help="token for telegram bot api to authorise")

    args = parser.parse_args(argv[1::])
    bot = Bot(args.token, args.host_server, args.host_image)
    bot.start()


if __name__ == "__main__":
    main(sys.argv)

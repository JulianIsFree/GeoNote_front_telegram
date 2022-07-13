import enum
import sys
import requests
import argparse

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
        assert(":" in host)
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


class Bot:
    def __init__(self, token, server_api_provider):
        # required for middleware, must be called before init of TeleBot
        apihelper.ENABLE_MIDDLEWARE = True
        self.bot = TeleBot(token)
        self.user_info = dict()

        self.provider = server_api_provider
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
        def all_notes(msg: Message):
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
            bot.reply_to(msg, "Welcome! Try /note first. You can use /my_notes at any moment")

        @bot.message_handler(commands=['note'], func=lambda msg: self.user_info[msg.from_user.id].state == State.WAIT)
        def on_note(msg: Message):
            self.user_info[msg.from_user.id].set_state(State.NOTE)
            bot.reply_to(msg, "Type anything you want and after use /post or /cancel")

        @bot.message_handler(commands=['cancel'], func=lambda msg: self.user_info[msg.from_user.id].state == State.NOTE \
                                                                   or self.user_info[msg.from_user.id].state == State.POST)
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
            bot.reply_to(msg, text="Done", reply_markup=markup)

        @bot.message_handler(func=lambda msg: self.user_info[msg.from_user.id].state == State.NOTE)
        def on_echo_note(msg: Message):
            self.user_info[msg.from_user.id].add_to_note(msg.text)
            bot.reply_to(msg, "Recorded")

        @bot.message_handler(commands=['post_with_geo'], func=lambda msg: self.user_info[msg.from_user.id].state == State.POST)
        def on_post_with_geo(msg):
            (x, y) = msg.location
            user = msg.from_user.id
            text = self.user_info[user].note

            self.provider.post_with_geo(user, text, x, y)
            self.user_info[user].set_state(State.WAIT)
            bot.reply_to(msg, "Done", reply_markup=ReplyKeyboardRemove())

        @bot.message_handler(commands=['post_without_geo'], func=lambda msg: self.user_info[msg.from_user.id].state == State.POST)
        def on_post_without_geo(msg):
            user = msg.from_user.id
            text = self.user_info[user].note

            self.provider.post_without_geo(user, text)
            self.user_info[user].set_state(State.WAIT)
            bot.reply_to(msg, "Done", reply_markup=ReplyKeyboardRemove())

    def start(self):
        self.bot.infinity_polling()


def main(argv):
    parser = argparse.ArgumentParser(description="TeleBot for interaction with GeoNote. Work in progress.")
    parser.add_argument("--host", help="server address in format like http://example.com or http://ip:port",
                        default="http://localhost:8080")
    parser.add_argument("token", help="token for telegram bot api to authorise")

    args = parser.parse_args(argv[1::])
    bot = Bot(args.token, Provider(args.host))
    bot.start()


if __name__ == "__main__":
    main(sys.argv)

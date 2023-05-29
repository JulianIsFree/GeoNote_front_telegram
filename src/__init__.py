import argparse
import base64
import enum
import sys
import threading

import requests
import telebot.apihelper
from telebot import TeleBot
from telebot import apihelper
from telebot.types import *


class State(enum.Enum):
    WAIT = 0
    IN_GROUP = 1


class Provider:
    def __init__(self, host: str):
        assert (":" in host)
        self.host = host

    @staticmethod
    def _post_url(url, body):
        import json
        from types import SimpleNamespace
        response = json.loads(requests.post(url, json=body).content, object_hook=lambda x: SimpleNamespace(**x))
        if response.status != 0:
            raise Exception(response.error)
        return response

    def send_image(self, prompt, image_id):
        url = self.host + "/image/create"
        body = {
            "prompt": prompt,
            "url": image_id
        }
        response = self._post_url(url, body)
        return response.image.url

    def send_score(self, user_id, image_id, score):
        url = self.host + "/image/score"
        body = {
            "user": user_id,
            "url": image_id,
            "score": score
        }
        self._post_url(url, body)

    @staticmethod
    def _get_url(url, params):
        import json
        from types import SimpleNamespace
        response = json.loads(requests.get(url, params=params).content, object_hook=lambda x: SimpleNamespace(**x))
        if response.status != 0:
            raise Exception(response.error)
        return response

    def get_top(self):
        url = self.host + "/image/top"
        response = self._get_url(url, {})
        return response.top


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
            "steps": 1,
            "cfg_scale": 5,
            "seed": 0
        }
        print(str(text))
        res = self._post_url(self.host + "/sdapi/v1/txt2img", data)
        return res.json()['images'][0]


class ActiveGroup:
    lock = threading.Lock()

    def __init__(self, idx: int):
        self.id = idx

        # user_id -> chat_id
        self.users = {}
        self.prompt = ""

    def add_user(self, user_id, chat_id):
        self.users[user_id] = chat_id

    def remove_user(self, user_id):
        self.users.pop(user_id)

    def add_prompt(self, msg: str):
        with self.lock:
            self.prompt += msg + "\n"
            return self.prompt

    def every_chat(self):
        return self.users.values()

    def every_chat_except(self, user_id):
        return map(lambda x: self.users[x], filter(lambda x: x != user_id, self.users.keys()))

    def is_empty(self) -> bool:
        return len(self.users) == 0


class GroupManager:
    def __init__(self):
        self._next_id_counter = 0

        # user_id -> group
        self._user_group: [int, ActiveGroup] = dict()

        # published_group_id -> group
        self._public_groups: [int, ActiveGroup] = dict()

    def _next_id(self):
        res = self._next_id_counter
        self._next_id_counter += 1
        return res

    def publish_group(self, user: User):
        user_id = user.id
        if user_id not in self._user_group.keys():
            return False

        group = self._user_group[user_id]
        if group.id in self._public_groups.keys():
            return False

        self._public_groups[group.id] = group
        return True

    def close_group(self, user: User):
        user_id = user.id

        if user_id not in self._user_group.keys():
            return False

        group = self._user_group[user_id]
        if group.id not in self._public_groups.keys():
            return False

        self._public_groups.pop(group.id)
        return True

    def remove_group(self, group_id: int):
        for group in self._user_group.values():
            assert group.id != group_id, f"There are users in room {group.id}, but we are trying to remove it"

        self._public_groups.pop(group_id)

    # False if not in group, True else
    def user_leaves(self, user: User):
        user_id = user.id
        if user_id not in self._user_group.keys():
            return None

        group = self._user_group[user_id]
        group.remove_user(user_id)
        self._user_group.pop(user_id)

        if group.is_empty() and group.id in self._public_groups.keys():
            self.remove_group(group.id)
            return None

        return group

    # False if user already in some group or no public group found, True else
    def user_joins(self, user: User, chat: Chat, idx: int) -> bool:
        if idx not in self._public_groups.keys():
            return False

        user_id = user.id
        group = self._public_groups[idx]
        group.add_user(user_id, chat.id)
        self._user_group[user_id] = group

        return True

    def public_group_exists(self, group_id):
        return group_id in self._public_groups.keys()

    def get_group(self, user_id: int):
        return self._user_group[user_id]

    def new_group(self, user_id: int, chat_id: int):
        assert user_id not in self._user_group.keys()

        idx = self._next_id()
        group = ActiveGroup(idx)
        group.add_user(user_id, chat_id)
        self._user_group[user_id] = group

        return self._user_group[user_id]

    def public_groups_list(self):
        return self._public_groups.keys()

    def has_group(self, user_id):
        return user_id in self._user_group.keys()


class Bot:
    def __init__(self, token, server_host, image_host):
        # required for middleware, must be called before init of TeleBot
        apihelper.ENABLE_MIDDLEWARE = True
        self.bot = TeleBot(token)
        self.user_info = dict()

        self.service_provider = Provider(server_host)
        self.image_provider = ImageProvider(image_host)
        self.image_cache = dict()

        self.group_manager = GroupManager()

        self.setup()

    def setup(self):
        bot = self.bot

        def is_public_str(group_id):
            if group_id in self.group_manager.public_groups_list():
                return "public"
            return "close"

        def is_public(group_id):
            return is_public_str(group_id) == "public"

        def someone_says(who: str, what: str):
            return f"{who} says:" \
                   f"\n\n{what}"

        def notify_chats(chats, about: str):
            for chat in chats:
                bot.send_message(chat_id=chat, text=about)

        #######################################################################################
        # Other logic: saving notes, posting
        @bot.message_handler(commands=['start'])
        def on_start(msg: Message):
            if not self.group_manager.has_group(msg.from_user.id):
                self.group_manager.new_group(msg.from_user.id, msg.chat.id)

            on_help(msg)

        def get_markup():
            markup = ReplyKeyboardMarkup(row_width=1)
            markup.add(KeyboardButton(text="/leave"))  #
            markup.add(KeyboardButton(text="/join"))  #
            markup.add(KeyboardButton(text="/new_group"))  #
            markup.add(KeyboardButton(text="/help"))  #
            markup.add(KeyboardButton(text="/public_group_list"))  #
            markup.add(KeyboardButton(text="/publish"))  #
            markup.add(KeyboardButton(text="/close"))  #
            markup.add(KeyboardButton(text="/top"))  #
            markup.add(KeyboardButton(text="/current_prompt"))  #
            markup.add(KeyboardButton(text="/get"))
            markup.add(KeyboardButton(text="/add"))
            markup.add(KeyboardButton(text="/new"))
            return markup

        @bot.message_handler(commands=['help'])
        def on_help(msg: Message):
            user_id = msg.from_user.id
            manager = self.group_manager
            group = manager.get_group(user_id) if manager.has_group(user_id) else None
            if group is None:
                group_info = "You have no group! Try /new_group\n"
            else:
                group_info = f"Your group ID is {group.id} and its {is_public_str(group.id)}.\n"
            text = f"Welcome!\n" \
                   f"{group_info}" \
                   f"If you want to join any group just try /public_group_list or use /join instead\n" \
                   f"Use /new to generate new image independently\n" \
                   f"Or use /add to stack up your prompt with previous /add calls"
            bot.reply_to(msg, text=text, reply_markup=get_markup())

        @bot.message_handler(commands=['leave'])
        def on_leave(msg: Message):
            user_id = msg.from_user.id
            username = msg.from_user.username
            if not self.group_manager.has_group(user_id):
                bot.reply_to(msg, text="You are not in any group yet. Try /public_group_list")
                return
            group = self.group_manager.get_group(user_id)
            notify_chats(group.every_chat(), someone_says(f"Group {group.id}", f"{username} left"))
            self.group_manager.user_leaves(msg.from_user)

        @bot.message_handler(commands=['join'])
        def on_join(msg: Message):
            data = msg.text.split()
            if len(data) < 2:
                bot.reply_to(msg, "Specify id of group to join: /join id")
                return

            user_id = msg.from_user.id
            username = msg.from_user.username

            try:
                idx = int(data[1])
                if not self.group_manager.public_group_exists(idx):
                    bot.reply_to(msg, f"There is no public group with ID {idx}")
                    return

                if self.group_manager.has_group(user_id):
                    on_leave(msg)

                self.group_manager.user_joins(msg.from_user, msg.chat, idx)

            except ValueError:
                bot.reply_to(msg, "ID mustn't contain anything but numbers")
                return

            current_group = self.group_manager.get_group(user_id)
            notify_chats(current_group.every_chat(), someone_says(f"Group {current_group.id}", f"{username} joined"))

        @bot.message_handler(commands=['publish'])
        def on_publish(msg: Message):
            user_id = msg.from_user.id
            if not self.group_manager.publish_group(msg.from_user):
                if self.group_manager.has_group(user_id):
                    group = self.group_manager.get_group(user_id)
                    assert is_public_str(group.id)
                    bot.reply_to(msg, f"Your group {group.id} is already published")
                    return
                else:
                    group = self.group_manager.new_group(user_id, msg.chat.id)
                    assert self.group_manager.publish_group(msg.from_user)
                    bot.reply_to(msg, f"You didn't have a group, so we created public group for you! ID: {group.id}")
                    return
            bot.reply_to(msg, f"You've made your group {self.group_manager.get_group(user_id).id} public")

        @bot.message_handler(commands=['close'])
        def on_close(msg: Message):
            user_id = msg.from_user.id
            if not self.group_manager.close_group(msg.from_user):
                if self.group_manager.has_group(user_id):
                    group = self.group_manager.get_group(user_id)
                    assert not is_public(group.id)
                    bot.reply_to(msg, f"Your group {group.id} is already closed")
                    return
                else:
                    group = self.group_manager.new_group(user_id, msg.chat.id)
                    assert not is_public(group.id)
                    bot.reply_to(msg, f"You didn't have a group, so we created closed group for you! ID: {group.id}")
                    return
            bot.reply_to(msg, f"You've closed your group {self.group_manager.get_group(user_id).id}")

        @bot.message_handler(commands=['new_group'])
        def on_new_group(msg: Message):
            user_id = msg.from_user.id
            if self.group_manager.has_group(user_id):
                bot.reply_to(msg, f"You are already in a group {self.group_manager.get_group(user_id).id}.\n"
                                  f"Leave first: /leave")
                return

            group = self.group_manager.new_group(user_id, msg.chat.id)
            bot.reply_to(msg, f"Your new group's ID is: {group.id} and it's closed by default.\n"
                              f"To publish use /publish")

        @bot.message_handler(commands=['public_group_list'])
        def on_get_public_group_list(msg: Message):
            groups = self.group_manager.public_groups_list()
            if len(groups) == 0:
                text = "No public groups. Be the first! /publish"
            else:
                text = ""
                for group in groups:
                    text += str(group) + "\n"

            bot.reply_to(msg, text)

        @bot.message_handler(commands=['current_prompt'])
        def on_get_current_prompt(msg: Message):
            user_id = msg.from_user.id
            if not self.group_manager.has_group(user_id):
                bot.reply_to(msg, "You aren't in any group yet. Open your room with /new_group")
                return

            group = self.group_manager.get_group(user_id)
            prompt = group.prompt
            if len(prompt) == 0:
                text = "Current prompt is empty"
            else:
                text = f"Current prompt is: \n{prompt}"
            bot.reply_to(msg, text)

        @bot.message_handler(commands=['new'])
        def on_new(msg: Message):
            data = msg.text.split()

            if len(data) < 2:
                bot.reply_to(msg, "Specify what you want to see: /new Winter forest with a frozen lake")
                return

            prompt = msg.text[msg.text.find('/new') + 5:]
            self.send_picture(prompt, msg.chat.id)

        @bot.message_handler(commands=['add'])
        def on_add(msg: Message):
            user_id = msg.from_user.id
            if not self.group_manager.has_group(user_id):
                bot.reply_to(msg, "You are not in any group yet. Create one with /new_group")
                return

            data = msg.text.split()

            if len(data) < 2:
                bot.reply_to(msg, "Specify what you want to add to current prompt: /add And also fog everywhere")
                return

            prompt_to_add = msg.text[msg.text.find('/add') + 5:]
            group = self.group_manager.get_group(user_id)
            prompt = group.add_prompt(prompt_to_add)

            chats = group.every_chat()

            keyboard = InlineKeyboardMarkup()
            row1 = [InlineKeyboardButton("Like", callback_data='like'),
                    InlineKeyboardButton("dislike", callback_data='dislike')]
            keyboard.add(*row1)

            for chat_id in chats:
                image_message, image_id = self.send_picture(prompt, chat_id)
                bot.reply_to(image_message, text=f"ID: {image_id}", reply_markup=keyboard)

        # Handle callback queries
        @bot.callback_query_handler(func=lambda call: True)
        def handle_callback_query(call):
            # Handle button press here
            user_id = call.from_user.id
            if call.message.text is not None:
                photo_file_id = call.message.text[call.message.text.find("ID: ") + 4:]
            else:
                photo_file_id = call.message.caption
            if call.data == 'like':
                self.service_provider.send_score(user_id, photo_file_id, 1)
            elif call.data == 'dislike':
                self.service_provider.send_score(user_id, photo_file_id, -1)

        @bot.message_handler(commands=['top'])
        def on_top(msg: Message):
            top = self.service_provider.get_top()
            if len(top) == 0:
                text = "No one in top, be first!"
            else:
                text = ""
                for row in top:
                    text += f"{row.score} : {row.url}\n"

            bot.reply_to(msg, text=text)

        @bot.message_handler(commands=['get'])
        def on_get(msg: Message):
            data = msg.text.split()
            if len(data) < 2:
                bot.reply_to(msg, "Identifier required: /get ID")
                return

            url = msg.text[msg.text.find('/get') + 5:]

            if url not in self.image_cache.values():
                bot.reply_to(msg, "Sorry, no ID found")
                return

            keyboard = InlineKeyboardMarkup()
            row1 = [InlineKeyboardButton("Like", callback_data='like'),
                    InlineKeyboardButton("dislike", callback_data='dislike')]
            keyboard.add(*row1)

            bot.send_photo(chat_id=msg.chat.id, photo=url, caption=url, reply_markup=keyboard)

        @bot.message_handler(func=lambda x: True)
        def on_echo(msg: Message):
            user_id = msg.from_user.id
            if not self.group_manager.has_group(user_id):
                bot.reply_to(msg, "This isn't a known command to me and you aren't in any group to chat, try /publish")
                return

            group = self.group_manager.get_group(user_id)
            notify_chats(group.every_chat_except(user_id), someone_says(msg.from_user.username, msg.text))

    def start(self):
        self.bot.infinity_polling()

    def send_picture(self, prompt, chat_id, reply_markup=None):
        if prompt in self.image_cache.keys():
            img = self.image_cache[prompt]
        else:
            img = base64.decodebytes(bytes(self.image_provider.generate(prompt), 'utf-8'))
        sent_message = self.bot.send_photo(chat_id=chat_id, photo=img, reply_markup=reply_markup)

        if prompt not in self.image_cache.keys():
            photos = sent_message.photo
            photo = photos[len(photos) - 1]
            self.image_cache[prompt] = photo.file_id

        image_id = self.service_provider.send_image(prompt, self.image_cache[prompt])
        return sent_message, image_id


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

# coding=utf-8

import html
import logging
import mimetypes
import os
from typing import Optional

import telegram
import telegram.constants
import telegram.error
import telegram.ext
import yaml

from ehforwarderbot import EFBChannel, EFBMsg, EFBStatus, EFBChat, coordinator
from ehforwarderbot import utils as efb_utils
from ehforwarderbot.constants import MsgType, ChannelType
from . import __version__ as version
from . import utils as etm_utils
from .db import DatabaseManager
from .bot_manager import TelegramBotManager
from .chat_binding import ChatBindingManager, ETMChat
from .commands import CommandsManager
from .master_message import MasterMessageProcessor
from .slave_message import SlaveMessageProcessor
from .utils import ExperimentalFlagsManager
from .voice_recognition import VoiceRecognitionManager


class TelegramChannel(EFBChannel):
    """
    EFB Channel - Telegram (Master)
    Based on python-telegram-bot, Telegram Bot API

    Author: Eana Hufwe <https://github.com/blueset>

    External Services:
        You may need API keys from following service providers to use speech recognition.
        Bing Speech API: https://www.microsoft.com/cognitive-services/en-us/speech-api
        Baidu Speech Recognition API: http://yuyin.baidu.com/

    Configuration file example:
        .. code-block:: yaml
            
            token: "12345678:1a2b3c4d5e6g7h8i9j"
            admins:
            - 102938475
            - 91827364
            speech_api:
                bing: ["token1", "token2"]
                baidu:
                    app_id: 123456
                    api_key: "API_KEY_GOES_HERE"
                    secret: "SECRET_KEY_GOES_HERE"
            flags:
                join_msg_threshold_secs: 10
                multiple_slave_chats: false
    """

    # Meta Info
    channel_name = "Telegram Master"
    channel_emoji = "✈"
    channel_type = ChannelType.Master
    supported_message_types = {MsgType.Text, MsgType.File, MsgType.Audio,
                               MsgType.Image, MsgType.Link, MsgType.Location,
                               MsgType.Sticker, MsgType.Video}
    __version__ = version.__version__

    # Data
    msg_status = {}
    msg_storage = {}
    _stop_polling = False
    timeout_count = 0

    # Constants
    config = None

    # Slave-only channels
    get_chat = None
    get_chats = None
    get_chat_picture = None

    def __init__(self):
        """
        Initialization.
        """
        super().__init__()

        # Suppress debug logs from dependencies
        logging.getLogger('requests').setLevel(logging.CRITICAL)
        logging.getLogger('urllib3').setLevel(logging.CRITICAL)
        logging.getLogger('telegram.bot').setLevel(logging.CRITICAL)
        logging.getLogger('telegram.vendor.ptb_urllib3.urllib3.connectionpool').setLevel(logging.CRITICAL)

        # Set up logger
        self.logger: logging.Logger = logging.getLogger(__name__)

        # Load configs
        self.load_config()

        # Load predefined MIME types
        mimetypes.init(files=["mimetypes"])

        # Initialize managers
        self.db: DatabaseManager = DatabaseManager(self)
        self.bot_manager: TelegramBotManager = TelegramBotManager(self)
        self.voice_recognition: VoiceRecognitionManager = VoiceRecognitionManager(self)
        self.chat_binding: ChatBindingManager = ChatBindingManager(self)
        self.flag: ExperimentalFlagsManager = ExperimentalFlagsManager(self)
        self.commands: CommandsManager = CommandsManager(self)
        self.master_messages: MasterMessageProcessor = MasterMessageProcessor(self)
        self.slave_messages: SlaveMessageProcessor = SlaveMessageProcessor(self)

        # Basic message handlers
        self.bot_manager.dispatcher.add_handler(
            telegram.ext.CommandHandler("start", self.start, pass_args=True))
        self.bot_manager.dispatcher.add_handler(
            telegram.ext.CommandHandler("help", self.help))
        self.bot_manager.dispatcher.add_handler(
            telegram.ext.CommandHandler("info", self.info))

        self.bot_manager.dispatcher.add_error_handler(self.error)

    def load_config(self):
        """
        Load configuration from path specified by the framework.
        
        Configuration file is in YAML format.
        """
        config_path = efb_utils.get_config_path(self.channel_id)
        if not os.path.exists(config_path):
            raise FileNotFoundError("Config File does not exist. (%s)" % config_path)
        with open(config_path) as f:
            data = yaml.load(f)

            # Verify configuration
            if not isinstance(data.get('token', None), str):
                raise ValueError('Telegram bot token must be a string')
            if isinstance(data.get('admins', None), int):
                data['admins'] = [data['admins']]
            if isinstance(data.get('admins', None), str) and data['admins'].isdigit():
                data['admins'] = [int(data['admins'])]
            if not isinstance(data.get('admins', None), list) or len(data['admins']) < 1:
                raise ValueError('Admins\' user IDs must be a list of one number or more.')
            for i in range(len(data['admins'])):
                if isinstance(data['admins'][i], str) and data['admins'][i].isdigit():
                    data['admins'][i] = int(data['admins'][i])
                if not isinstance(data['admins'][i], int):
                    raise ValueError('Admin ID is expected to be a string, but %r is found.' % data['admins'][i])

            self.config = data.copy()

    def info(self, bot, update):
        """
        Show info of the current telegram conversation.
        Triggered by `/info`.

        Args:
            bot: Telegram Bot instance
            update: Message update
        """
        if update.message.chat_id == update.message.from_user.id:  # Talking to the bot.
            msg = "This is EFB Telegram Master Channel %s, " \
                  "you currently have %s slave channels activated:" % (self.__version__, len(coordinator.slaves))
            for i in coordinator.slaves:
                msg += "\n- %s %s (%s, %s)" % (coordinator.slaves[i].channel_emoji,
                                               coordinator.slaves[i].channel_name,
                                               i, coordinator.slaves[i].__version__)
        else:
            links = self.db.get_chat_assoc(master_uid=etm_utils.chat_id_to_str(self.channel_id, update.message.chat_id))
            if links:  # Linked chat
                msg = "The group {group_name} ({group_id}) is " \
                      "linked to the following chat(s):".format(group_name=update.message.chat.title,
                                                                group_id=update.message.chat_id)
                for i in links:
                    channel_id, chat_id = etm_utils.chat_id_str_to_id(i)
                    d = self.chat_binding.get_chat_from_db(channel_id, chat_id)
                    if d:
                        msg += "\n- %s" % ETMChat(chat=d, db=self.db).full_name()
                    else:
                        msg += "\n- {channel_emoji} {channel_name}: Unknown chat ({chat_id})".format(
                            channel_emoji=coordinator.slaves[channel_id].channel_emoji,
                            channel_name=coordinator.slaves[channel_id].channel_name,
                            chat_id=chat_id
                        )
            else:
                msg = "The group {group_name} ({group_id}) is not linked to any remote chat. " \
                      "To link one, use /link.".format(group_name=update.message.chat.title,
                                                       group_id=update.message.chat_id)

        update.message.reply_text(msg)

    def start(self, bot, update, args=None):
        """
        Process bot command `/start`.

        Args:
            bot: Telegram Bot instance
            update (telegram.Update): Message update
            args: Arguments from message
        """
        if update.message.chat.type != telegram.Chat.PRIVATE and args:  # from group
            self.chat_binding.link_chat(update, args)
        else:
            txt = "Welcome to EH Forwarder Bot: EFB Telegram Master Channel.\n\n" \
                  "To learn more, please visit https://github.com/blueset/ehForwarderBot ."
            bot.send_message(update.message.from_user.id, txt)

    def help(self, bot, update):
        txt = "EFB Telegram Master Channel\n" \
              "/link\n" \
              "    Link a remote chat to an empty Telegram group.\n" \
              "    Followed by a regular expression to filter results.\n" \
              "/chat\n" \
              "    Generate a chat head to start a conversation.\n" \
              "    Followed by a regular expression to filter results.\n" \
              "/extra\n" \
              "    List all extra function from slave channels.\n" \
              "/unlink_all\n" \
              "    Unlink all remote chats in this chat.\n" \
              "/recog\n" \
              "    Reply to a voice message to convert it to text.\n" \
              "    Followed by a language code to choose a specific language.\n" \
              "    You have to enable speech to text in the config file first.\n" \
              "/help\n" \
              "    Print this command list."
        bot.send_message(update.message.from_user.id, txt)

    def poll(self):
        """
        Message polling process.
        """

        self.bot_manager.polling()

    def error(self, bot, update, error):
        """
        Print error to console, and send error message to first admin.
        Triggered by python-telegram-bot error callback.
        """
        if "Conflict: terminated by other long poll or webhook (409)" in str(error):
            msg = 'Conflicted polling detected. If this error persists, ' \
                  'please ensure you are running only one instance of this Telegram bot.'
            self.logger.critical(msg)
            self.bot_manager.send_message(self.config['admins'][0], msg)
            return
        try:
            raise error
        except telegram.error.Unauthorized:
            self.logger.error("The bot is not authorised to send update:\n%s\n%s", str(update), str(error))
        except telegram.error.BadRequest:
            self.logger.error("Message request is invalid.\n%s\n%s", str(update), str(error))
            self.bot_manager.send_message(self.config['admins'][0],
                                          "Message request is invalid.\n%s\n<code>%s</code>)" %
                                          (html.escape(str(error)), html.escape(str(update))), parse_mode="HTML")
        except (telegram.error.TimedOut, telegram.error.NetworkError):
            self.timeout_count += 1
            self.logger.error("Poor internet connection detected.\n"
                              "Number of network error occurred since last startup: %s\n\%s\nUpdate: %s",
                              self.timeout_count, str(error), str(update))
            if update is not None and isinstance(getattr(update, "message", None), telegram.Message):
                update.message.reply_text("This message is not processed due to poor internet environment "
                                          "of the server.\n"
                                          "<code>%s</code>" % html.escape(str(error)), quote=True, parse_mode="HTML")

            timeout_interval = self.flag('network_error_prompt_interval')
            if timeout_interval > 0 and self.timeout_count % timeout_interval == 0:
                self.bot_manager.send_message(self.config['admins'][0],
                                              "<b>EFB Telegram Master channel</b>\n"
                                              "You may have a poor internet connection on your server. "
                                              "Currently %s network errors are detected.\n"
                                              "For more details, please refer to the log." % (self.timeout_count),
                                              parse_mode="HTML")
        except telegram.error.ChatMigrated as e:
            new_id = e.new_chat_id
            old_id = update.message.chat_id
            count = 0
            for i in self.db.get_chat_assoc(master_uid=etm_utils.chat_id_to_str(self.channel_id, old_id)):
                self.logger.debug('Migrating slave chat %s from Telegram chat %s to %s.', i, old_id, new_id)
                self.db.remove_chat_assoc(slave_uid=i)
                self.db.add_chat_assoc(master_uid=etm_utils.chat_id_to_str(self.channel_id, new_id), slave_uid=i)
                count += 1
            bot.send_message(new_id, "Chat migration detected."
                                     "All remote chats (%s) are now linked to this new group." % count)
        except:
            try:
                bot.send_message(self.config['admins'][0],
                                 "EFB Telegram Master channel encountered error <code>%s</code> "
                                 "caused by update <code>%s</code>." %
                                 (html.escape(str(error)), html.escape(str(update))), parse_mode="HTML")
            except:
                self.logger.error("Failed to send error message through Telegram.")
            finally:
                self.logger.error('Unhandled telegram bot error!\n'
                                  'Update %s caused error %s' % (update, error))

    def send_message(self, msg: EFBMsg) -> EFBMsg:
        return self.slave_messages.send_message(msg)

    def send_status(self, status: EFBStatus):
        return self.slave_messages.send_status(status)

    def stop_polling(self):
        self.logger.debug("Gracefully stopping %s (%s).", self.channel_name, self.channel_id)
        self.bot_manager.graceful_stop()
        self.logger.debug("%s (%s) gracefully stopped.", self.channel_name, self.channel_id)
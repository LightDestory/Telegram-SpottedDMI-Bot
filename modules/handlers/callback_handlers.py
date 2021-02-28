"""Handles the execution of callbacks by the bot"""
from typing import Tuple
from telegram import Update, InlineKeyboardMarkup
from telegram.ext import CallbackContext
from telegram.error import BadRequest, Unauthorized
from modules.handlers import STATE
from modules.debug.log_manager import logger
from modules.data.data_reader import config_map
from modules.data import PendingPost, PublishedPost, PostData, Report, User
from modules.utils.info_util import get_callback_info
from modules.utils.keyboard_util import REACTION, update_approve_kb, update_vote_kb, get_stats_kb
from modules.utils.post_util import send_post_to, show_admins_votes


def old_reactions(data: str) -> str:
    """Used to mantain compatibility with the old reactions.
    Can be removed later

    Args:
        data (str): callback data

    Returns:
        str: new reaction data corrisponding with the old reaction
    """
    if data == "meme_vote_yes":
        return "meme_vote,1"
    if data == "meme_vote_no":
        return "meme_vote,0"
    return data


def meme_callback(update: Update, context: CallbackContext) -> int:
    """Passes the meme callback to the correct handler

    Args:
        update (Update): update event
        context (CallbackContext): context passed by the handler

    Returns:
        int: value to return to the handler, if requested
    """
    info = get_callback_info(update, context)
    info['data'] = old_reactions(info['data'])
    # the callback data indicates the correct callback and the arg to pass to it separated by ,
    data = info['data'].split(",")
    try:
        # call the correct function
        message_text, reply_markup, output = globals()[f'{data[0][5:]}_callback'](info, data[1])

    except KeyError as e:
        message_text = reply_markup = output = None
        logger.error("meme_callback: %s", e)

    if message_text:  # if there is a valid text, edit the menu with the new text
        info['bot'].edit_message_text(chat_id=info['chat_id'],
                                      message_id=info['message_id'],
                                      text=message_text,
                                      reply_markup=reply_markup)
    elif reply_markup:  # if there is a valid reply_markup, edit the menu with the new reply_markup
        info['bot'].edit_message_reply_markup(chat_id=info['chat_id'],
                                              message_id=info['message_id'],
                                              reply_markup=reply_markup)
    return output


# region handle meme_callback
def confirm_callback(info: dict, arg: str) -> Tuple[str, InlineKeyboardMarkup, int]:
    """Handles the confirm,[ yes | no ] callback.

    - yes: Saves the post as pending and sends it to the admins for them to check.
    - no: cancel the current spot conversation

    Args:
        info (dict): information about the callback
        arg (str): [ yes | no ]

    Returns:
        Tuple[str, InlineKeyboardMarkup, int]: text and replyMarkup that make up the reply, new conversation state
    """
    if arg == "yes":  # if the the user wants to publish the post
        user_message = info['message'].reply_to_message
        admin_message = send_post_to(message=user_message, bot=info['bot'], destination="admin")
        if admin_message:
            text = "Il tuo post è in fase di valutazione\n"\
                "Una volta pubblicato, lo potrai trovare su @Spotted_DMI"
        else:
            text = "Si è verificato un problema\nAssicurati che il tipo di post sia fra quelli consentiti"

    elif arg == "no":  # if the the user changed his mind
        text = "Va bene, alla prossima 🙃"

    else:
        text = None
        logger.error("confirm_callback: invalid arg '%s'", arg)

    return text, None, STATE['end']


def settings_callback(info: dict, arg: str) -> Tuple[str, InlineKeyboardMarkup, int]:
    """Handles the settings,[ anonimo | credit ] callback.

    - anonimo: Removes the user_id from the table of credited users, if present.
    - credit: Adds the user_id to the table of credited users, if it wasn't already there.

    Args:
        info (dict): information about the callback
        arg (str): [ anonimo | credit ]

    Returns:
        Tuple[str, InlineKeyboardMarkup, int]: text and replyMarkup that make up the reply, new conversation state
    """
    user = User(info['sender_id'])
    if arg == "anonimo":  # if the user wants to be anonym
        # if the user was already anonym
        if user.become_anonym():
            text = "Sei già anonimo"
        else:
            text = "La tua preferenza è stata aggiornata\n"\
                "Ora i tuoi post saranno anonimi"

    elif arg == "credit":  # if the user wants to be credited
        if user.become_credited():
            text = "Sei già creditato nei post\n"
        else:
            text = "La tua preferenza è stata aggiornata\n"

        if info['sender_username']:  # the user has a valid username
            text += f"I tuoi post avranno come credit @{info['sender_username']}"
        else:
            text += "ATTENZIONE:\nNon hai nessun username associato al tuo account telegram\n"\
                "Se non lo aggiungi, non sarai creditato"
    else:
        text = None
        logger.error("settings_callback: invalid arg '%s'", arg)

    return text, None, None


def approve_yes_callback(info: dict, arg: None) -> Tuple[str, InlineKeyboardMarkup, int]:  # pylint: disable=unused-argument
    """Handles the approve_yes callback.
    Approves the post, deleting it from the pending_post table, publishing it in the channel \
    and putting it in the published post table

    Args:
        info (dict): information about the callback

    Returns:
        Tuple[str, InlineKeyboardMarkup, int]: text and replyMarkup that make up the reply, new conversation state
    """
    pending_post = PendingPost.from_group(group_id=info['chat_id'], g_message_id=info['message_id'])
    if pending_post is None:  # this pending post is not present in the database
        return None, None, None
    info['bot'].answerCallbackQuery(callback_query_id=info['query_id'])  # end the spinning progress bar
    n_approve = pending_post.set_admin_vote(info['sender_id'], True)

    # The post passed the approval phase and is to be published
    if n_approve >= config_map['meme']['n_votes']:
        message = info['message']
        user_id = pending_post.user_id
        published_post = send_post_to(message=message, bot=info['bot'], destination="channel")

        # if comments are enabled, save the user_id, so the user can be credited
        if config_map['meme']['comments']:
            info['bot_data'][f"{published_post.chat_id},{published_post.message_id}"] = user_id

        try:
            info['bot'].send_message(chat_id=user_id,
                                     text="Il tuo ultimo post è stato pubblicato su @Spotted_DMI")  # notify the user
        except (BadRequest, Unauthorized) as e:
            logger.warning("Notifying the user on approve_yes: %s", e)

        # Shows the list of admins who approved the pending post and removes it form the db
        show_admins_votes(pending_post=pending_post, bot=info['bot'], approve=True)
        pending_post.delete_post()
        return None, None, None

    if n_approve != -1:  # the vote changed
        keyboard = info['reply_markup'].inline_keyboard
        return None, update_approve_kb(keyboard=keyboard, pending_post=pending_post, approve=n_approve), None

    return None, None, None


def approve_no_callback(info: dict, arg: None) -> Tuple[str, InlineKeyboardMarkup, int]:  # pylint: disable=unused-argument
    """Handles the approve_no callback.
    Rejects the post, deleting it from the pending_post table

    Args:
        info (dict): information about the callback

    Returns:
        Tuple[str, InlineKeyboardMarkup, int]: text and replyMarkup that make up the reply, new conversation state
    """
    pending_post = PendingPost.from_group(group_id=info['chat_id'], g_message_id=info['message_id'])
    if pending_post is None:  # this pending post is not present in the database
        return None, None, None
    info['bot'].answerCallbackQuery(callback_query_id=info['query_id'])  # end the spinning progress bar
    n_reject = pending_post.set_admin_vote(info['sender_id'], False)

    # The post has been refused
    if n_reject >= config_map['meme']['n_votes']:
        user_id = pending_post.user_id

        try:
            info['bot'].send_message(
                chat_id=user_id,
                text="Il tuo ultimo post è stato rifiutato\nPuoi controllare le regole con /rules")  # notify the user
        except (BadRequest, Unauthorized) as e:
            logger.warning("Notifying the user on approve_no: %s", e)

        # Shows the list of admins who refused the pending post and removes it form the db
        show_admins_votes(pending_post=pending_post, bot=info['bot'], approve=False)
        pending_post.delete_post()
        return None, None, None

    if n_reject != -1:  # the vote changed
        keyboard = info['reply_markup'].inline_keyboard
        return None, update_approve_kb(keyboard=keyboard, pending_post=pending_post, reject=n_reject), None

    return None, None, None


def vote_callback(info: dict, arg: str) -> Tuple[str, InlineKeyboardMarkup, int]:
    """Handles the vote,[ 0 | 1 | 2 | 3 | 4 ] callback.

    Args:
        info (dict): information about the callback
        arg (str): [ 0 | 1 | 2 | 3 | 4 ]


    Returns:
        Tuple[str, InlineKeyboardMarkup, int]: text and replyMarkup that make up the reply, new conversation state
    """
    publishedPost = PublishedPost.from_channel(channel_id=info['chat_id'], c_message_id=info['message_id'])
    was_added = publishedPost.set_user_vote(user_id=info['sender_id'], vote=arg)

    if was_added:
        info['bot'].answerCallbackQuery(callback_query_id=info['query_id'], text=f"Hai messo un {REACTION[arg]}")
    else:
        info['bot'].answerCallbackQuery(callback_query_id=info['query_id'], text=f"Hai tolto il {REACTION[arg]}")

    keyboard = info['reply_markup'].inline_keyboard
    return None, update_vote_kb(keyboard=keyboard, published_post=publishedPost), None


# endregion


def report_spot_callback(info: dict, args: str) -> Tuple[str, InlineKeyboardMarkup, int]:  # pylint: disable=unused-argument
    """Handles the report callback.

    Args:
        info (dict): information about the callback
        arg (str): unused

    Returns:
        Tuple[str, InlineKeyboardMarkup, int]: text and replyMarkup that make up the reply, new conversation state
    """

    abusive_message_id = info['message']['reply_to_message']['message_id']

    report = Report.get_post_report(user_id=info['sender_id'],
                                    channel_id=config_map['meme']['channel_id'],
                                    c_message_id=abusive_message_id)
    if report is not None:  # this user has already reported this post
        info['bot'].answerCallbackQuery(callback_query_id=info['query_id'], text="Hai già segnalato questo spot.")
        return None, None, STATE['end']

    info['bot'].answerCallbackQuery(callback_query_id=info['query_id'], text="Segnala in privato tramite il bot.")

    info['bot'].forward_message(chat_id=info['sender_id'], from_chat_id=info['chat_id'], message_id=abusive_message_id)

    info['bot'].send_message(chat_id=info['sender_id'],
                             text="Scrivi il motivo della segnalazione del post, altrimenti digita /cancel")

    info['user_data']['current_post_reported'] = abusive_message_id

    return None, None, STATE['reporting_spot']


def stats_callback(update: Update, context: CallbackContext):
    """Passes the stats callback to the correct handler

    Args:
        update (Update): update event
        context (CallbackContext): context passed by the handler
    """
    info = get_callback_info(update, context)
    info['bot'].answerCallbackQuery(callback_query_id=info['query_id'])  # end the spinning progress bar
    # the callback data indicates the correct callback and the arg to pass to it separated by ,
    data = info['data'].split(",")
    try:
        message_text = globals()[f'{data[0][6:]}_callback'](data[1])  # call the function based on its name
    except KeyError as e:
        logger.error("stats_callback: %s", e)
        return

    if message_text:  # if there is a valid text, edit the menu with the new text
        info['bot'].edit_message_text(chat_id=info['chat_id'],
                                      message_id=info['message_id'],
                                      text=message_text,
                                      reply_markup=get_stats_kb())
    else:  # remove the reply markup
        info['bot'].edit_message_reply_markup(chat_id=info['chat_id'], message_id=info['message_id'], reply_markup=None)


# region handle stats_callback
def avg_callback(arg: str) -> str:
    """Handles the avg_[ votes | 0 | 1 ] callback.
    Shows the average of the %arg per post

    Args:
        arg (str): [ votes | 0 | 1 ]

    Returns:
        str: text for the reply
    """
    if arg == "votes":
        avg_votes = PostData.get_avg()
        text = f"Gli spot ricevono in media {avg_votes} voti"
    else:
        avg_votes = PostData.get_avg(arg)
        text = f"Gli spot ricevono in media {avg_votes} {REACTION[arg]}"

    return text


def max_callback(arg: str) -> str:
    """Handles the max_[ votes | 0 | 1 ] callback
    Shows the post with the most %arg

    Args:
        arg (str): [ votes | 0 | 1 ]

    Returns:
        str: text for the reply
    """
    if arg == "votes":
        max_votes, message_id, channel_id = PostData.get_max_id()
        text = f"Lo spot con più voti ne ha {max_votes}\n"\
            f"Lo trovi a questo link: https://t.me/c/{channel_id[4:]}/{message_id}"
    else:
        max_votes, message_id, channel_id = PostData.get_max_id(arg)
        text = f"Lo spot con più {REACTION[arg]} ne ha {max_votes}\n" \
            f"Lo trovi a questo link: https://t.me/c/{channel_id[4:]}/{message_id}"

    return text


def tot_callback(arg: str) -> str:
    """Handles the tot_[ posts | votes | 0 | 1 ] callback
    Shows the total number of %arg

    Args:
        arg (str): [ posts | votes | 0 | 1 ]

    Returns:
        str: text for the reply
    """
    if arg == "posts":
        n_posts = PostData.get_n_posts()
        text = f"Sono stati pubblicati {n_posts} spot nel canale fin'ora.\nPotresti ampliare questo numero..."
    elif arg == "votes":
        n_votes = PostData.get_n_votes()
        text = f"Il totale dei voti ammonta a {n_votes}"
    else:
        n_votes = PostData.get_n_votes(arg)
        text = f"Il totale dei {REACTION[arg]} ammonta a {n_votes}"

    return text


def close_callback(arg: None) -> str:  # pylint: disable=unused-argument
    """Handles the close callback
    Closes the stats menu

    Returns:
        str: text and replyMarkup that make up the reply
    """
    return None


# endregion

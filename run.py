import time
import threading
from bot.argparser import args
from bot.logger import logger, set_logger_verbosity, quiesce_logger

set_logger_verbosity(args.verbosity)
quiesce_logger(args.quiet)



@logger.catch(reraise=True)
def init_mastodon():
    logger.init("Mastodon AI Horde Bot", status="Starting")
    from mastodon.Mastodon import MastodonNetworkError, MastodonGatewayTimeoutError, MastodonBadGatewayError, MastodonAPIError
    from bot.mastodon_notifications import db_r, MentionHandler
    from bot.mastodon_listener import StreamListenerExtended
    from bot.mastodon_ctrl import mastodon
    from bot import db_r
    notifications = mastodon.notifications(
        exclude_types=["follow", "favourite", "reblog", "poll", "follow_request"]
    )
    notifications.reverse()
    logger.info(f"Retrieved {len(notifications)} notifications.")
    waiting_threads = []
    for notification in notifications:
        if db_r.get(str(notification["id"])):
            continue
        notification_handler = MentionHandler(notification)
        thread = threading.Thread(target=notification_handler.handle_notification, args=())
        thread.start()
        waiting_threads.append(thread)    
    while True:
        try:
            logger.debug(f"Starting Listener")
            listener = StreamListenerExtended()
            logger.debug(f"Streaming User")
            mastodon.stream_user(listener=listener)
            time.sleep(1)
        except (MastodonGatewayTimeoutError, MastodonNetworkError, MastodonBadGatewayError, MastodonAPIError) as err:
            logger.warning(f"{err} reopening connection")
            listener.shutdown()
            time.sleep(10)

@logger.catch(reraise=True)
def init_lemmy():
    logger.init("Lemmy AI Horde Bot", status="Starting")
    from bot.lemmy_listener import StreamListenerExtended
    while True:
        try:
            logger.debug(f"Starting Listener")
            listener = StreamListenerExtended()
            time.sleep(1)
        except Exception as err:
            logger.warning(f"{err} reopening connection")
            listener.shutdown()
            time.sleep(10)

if __name__ == "__main__":
    try:
        if args.type == "mastodon":
            init_mastodon()
        if args.type == "lemmy":
            init_lemmy()
    except KeyboardInterrupt:
        logger.init_ok("AI Horde Bot", status="Exited")

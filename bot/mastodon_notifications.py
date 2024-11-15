import os, time, json
from mastodon.Mastodon import MastodonNetworkError, MastodonNotFoundError, MastodonGatewayTimeoutError, MastodonBadGatewayError, MastodonAPIError
from bs4 import BeautifulSoup
from datetime import timedelta
from bot.logger import logger 
from bot import db_r
from bot.horde import HordeMultiGen, JobStatus
from bot.mastodon_ctrl import mastodon
from bot.lemmy_ctrl import lemmy, lemmy_image_community_id
from bot.ratings import polled_ratings
from bot.style import Styling
from bot.exceptions import HordeBotReplyException, HordeBotException

class MentionHandler:

    def __init__(self, notification):
        self.status = JobStatus.INIT
        self.notification = notification
        self.incoming_status = self.notification["status"]
        self.notification_id = self.notification["id"]
        self.acct = self.notification["account"]['acct']
        self.request_id = self.incoming_status["id"]
        self.tags = [tag.name for tag in self.incoming_status["tags"]]

        self.mention_content = BeautifulSoup(self.incoming_status["content"],features="html.parser").get_text()

    def is_finished(self):
        return self.status in [JobStatus.DONE, JobStatus.FAULTED]

    @logger.catch(reraise=True)
    def handle_notification(self):
        if self.notification["status"]["visibility"] == "direct" and not Styling.is_generation_request(self.mention_content):
            self.handle_dm()
        else:
            self.handle_mention()

    def handle_mention(self):
        submit_ratings = False # TODO: Add logic to submit ratings to ratings DB when relevant
        # pp.pprint(notification)
        self.status = JobStatus.WORKING
        logger.debug(f"Handling notification {self.notification_id} as a mention")
        # logger.debug([self.notification_id, last_parsed_notification, self.notification_id < last_parsed_notification])
        try:
            styling = Styling(self.mention_content, self.acct)
            if db_r.get(str(self.notification["account"]["acct"])) and str(self.notification["account"]["acct"]) != 'stablehorde':
                logger.warning(f"Too frequent requests from {self.notification['account']['acct']}")
                self.reply_faulted("Unfortunately this bot has been rate limited. Please only send one request every 3 minutes.")
                return
            db_r.setex(str(self.notification["account"]["acct"]), timedelta(minutes=3), 1)
            gen: HordeMultiGen = styling.request_images(self.notification_id)
        except HordeBotReplyException as err:
            self.reply_faulted(err.reply)
            return
        except HordeBotException:
            logger.info(f"{self.request_id} is not a generation request, skipping")
            db_r.setex(str(self.notification_id), timedelta(days=30), 1)
            self.status = JobStatus.DONE
            return
        media_dicts = []
        done_jobs = gen.get_all_done_jobs()
        for job in done_jobs:
            for iter_fn in range(len(job.filenames)):
                logger.debug(f"Uploading {job.filenames[iter_fn]}...")
                for iter in range(3):
                    try:
                        media_dict = mastodon.media_post(
                            media_file=job.filenames[iter_fn],
                            description=f"Image with seed {job.seeds[iter_fn]} generated via Stable Diffusion through @stablehorde@sigmoid.social. Prompt: {job.prompt}"
                        )
                        break
                    except (MastodonGatewayTimeoutError, MastodonNetworkError, MastodonBadGatewayError, MastodonAPIError) as e:
                        # If a file fails, we skip it
                        if iter >= 2:
                            continue
                        logger.warning(f"Error '{e}' when uploading files. Retry {iter+1}/3")
                media_dicts.append(media_dict)
                logger.debug(f"Uploaded {job.filenames[iter_fn]}")
        if len(media_dicts) == 0:
            self.reply_faulted("Something went wrong when trying to fulfil your request. Please try again later")
            self.cleanup_files(gen)
            return
        logger.info(f"replying to {self.request_id}: {self.mention_content}")
        tags_string = ''
        for t in self.tags:
            tags_string += f" #{t}"
        for iter in range(4):
            try:
                visibility = self.incoming_status['visibility']
                # if visibility != 'direct' and os.environ['MASTODON_INSTANCE'] == "hachyderm.io":
                #     public_spot_found = False
                #     for iter in range(5):
                #         daily_post = db_r.get(f"hachyderm_daily_post_{iter}")
                #         if not daily_post:
                #             visibility = 'public'
                #             public_spot_found = True
                #             db_r.setex(f"hachyderm_daily_post_{iter}", timedelta(hours=24), 1)
                #             break
                #     if not public_spot_found:
                #         visibility = 'direct'
                public_minutes = 10
                if os.environ['MASTODON_INSTANCE'] == "hachyderm.io":
                    public_minutes = 120
                if visibility == 'public':
                    if db_r.get(f"{os.environ['MASTODON_INSTANCE']}_unlisted_post"):
                        visibility = 'unlisted'
                    else:
                        visibility = 'public'
                        db_r.setex(f"{os.environ['MASTODON_INSTANCE']}_unlisted_post", timedelta(minutes=public_minutes), 1)
                extra_tags = ''
                if os.environ['MASTODON_INSTANCE'] == "hachyderm.io":
                    extra_tags = " #hachybots"
                reply_text = f"Here are some images matching your request\nPrompt: {styling.prompt}\nStyle: {styling.style}\n\n#aiart #stablediffusion #aihorde{extra_tags}{tags_string}"
                if len(reply_text) > 500:
                    reply_text = f"Here are some images matching your request\nPrompt: {styling.prompt[0:300]}...\nStyle: {styling.style}\n\n#aiart #stablediffusion #aihorde{tags_string}"
                media_status_dict = mastodon.status_reply(
                    to_status=self.incoming_status,
                    status=reply_text,
                    media_ids=media_dicts,
                    spoiler_text="AI Generated Images",
                    visibility=visibility,
                )
                if len(media_dicts) > 1:
                    # Allow the other post to appear
                    time.sleep(5)
                    poll_options = []
                    for iter in range(len(media_dicts)):
                        poll_options.append(f"Generation {iter+1}")
                    poll = mastodon.make_poll(
                        options=poll_options,
                        expires_in=900,
                        # expires_in=300, # Testing
                    )
                    poll_status_dict = mastodon.status_reply(
                        to_status=media_status_dict,
                        status="Please let us know which of the generated images is the best.",
                        sensitive = False,
                        spoiler_text = None,
                        visibility=visibility,
                        poll=poll,
                    )
                    ret_poll = poll_status_dict['poll']
                    if submit_ratings:
                        polled_ratings.queue_poll(
                            poll_dict = ret_poll,
                            horde_job = done_jobs[0],
                        )
                if visibility in ["public", "unlisted"] and self.acct not in json.loads(os.getenv("CROSSPOST_IGNORE_LIST")):
                    logger.info("Initiating crosspost to Bot Art")
                    image_body = ''
                    for media_dict in media_dicts:
                        image_body += f"![{media_dict['description']}]({media_dict['url']})"
                    if len(media_dicts) > 1:
                        image_body += f"\n\n You can vote for the best image here: {poll_status_dict['url']}"
                    post_result = lemmy.post(
                        community_id=lemmy_image_community_id,
                        name=f"{styling.style}: {styling.prompt}"[0:200],
                        url=media_dicts[0]["url"],
                        body=f"Prompt: {styling.prompt}\n\nStyle: {styling.style}\n\n{image_body}"
                    )
                    if not post_result:
                        logger.warning("Failed to crosspost to Bot Art")
                break
            except (MastodonGatewayTimeoutError, MastodonNetworkError, MastodonBadGatewayError) as err:
                if iter >= 3:
                    self.reply_faulted("Something went wrong when trying to fulfil your request. Please try again later")
                    self.cleanup_files(gen)
                    return
                logger.warning(f"Network error when replying. Retry {iter+1}/3")
            except (MastodonNotFoundError) as err:
                self.set_faulted()
                logger.error(f"Missing reply. Aborting!")
                self.cleanup_files(gen)
                return
        self.cleanup_files(gen)
        # mastodon.status_reply(to_status=self.incoming_status, status="Here is your generation", media_ids=media_dict)
        db_r.setex(str(self.notification_id), timedelta(days=30), 1)
        self.status = JobStatus.DONE

    def handle_dm(self):
        # pp.pprint(notification)
        logger.debug(f"Handling notification {self.notification_id} as a DM")
        db_r.setex(str(self.notification_id), timedelta(days=30), 1)
        self.status = JobStatus.DONE

    def set_faulted(self):
        self.status = JobStatus.FAULTED
        db_r.setex(str(self.notification_id), timedelta(days=30), 1)

    def reply_faulted(self,message):
        self.set_faulted()
        visibility = "unlisted"
        if os.environ['MASTODON_INSTANCE'] == "hachyderm.io":
            visibility = "direct"
        mastodon.status_reply(
            to_status=self.incoming_status,
            status=message,
            visibility=visibility,
        )

    def cleanup_files(self,gen):
        for fn in gen.get_all_filenames():
            os.remove(fn)

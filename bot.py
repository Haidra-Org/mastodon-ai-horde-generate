import requests, json, os, time, argparse, base64, random
from mastodon import Mastodon
from mastodon.Mastodon import MastodonNetworkError, MastodonNotFoundError, MastodonGatewayTimeoutError, MastodonBadGatewayError, MastodonAPIError
from bot import args, logger, get_bot_db, is_redis_up, set_logger_verbosity, quiesce_logger
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from PIL import Image, ImageFont, ImageDraw, ImageFilter, ImageOps
from io import BytesIO
from datetime import timedelta


load_dotenv()
set_logger_verbosity(args.verbosity)
quiesce_logger(args.quiet)
import pprint, re

db_r = None
logger.init("Database", status="Connecting")
if is_redis_up():
	db_r = get_bot_db()
	logger.init_ok("Database", status="Connected")
else:
	logger.init_err("Database", status="Failed")

pp = pprint.PrettyPrinter(depth=3)
term_regex = re.compile(r'draw for me (.+)', re.IGNORECASE)
modifier_seek_regex = re.compile(r'style:', re.IGNORECASE)
prompt_only_regex = re.compile(r'draw for me (.+)style:', re.IGNORECASE)
style_regex = re.compile(r'style: ?(\w+)', re.IGNORECASE)

mastodon = Mastodon(
    access_token = 'pytooter_usercred.secret',
    api_base_url = 'https://sigmoid.social'
)

HORDE_URL = "https://stablehorde.net"
imgen_params = {
    "n": 4,
    "width": 512,
    "height":512,
    "steps": 30,
    "sampler_name": "k_euler_a",
    "cfg_scale": 7.5,
    "post_processing": ['GFPGAN'],
}
generic_submit_dict = {
    "prompt": "a horde of cute stable robots in a sprawling server room repairing a massive mainframe",
    "nsfw": False,
    "censor_nsfw": True,
    "trusted_workers": True,
    "models": ["stable_diffusion"]
}

@logger.catch(reraise=True)
def check_for_requests():
    last_parsed_notification = db_r.get("last_parsed_id")
    if last_parsed_notification != None:
        last_parsed_notification = int(last_parsed_notification)
    logger.debug(f"Last notification ID: {last_parsed_notification}")
    notifications = mastodon.notifications(
        since_id={"id": last_parsed_notification},  # doesn't work atm https://github.com/halcy/Mastodon.py/issues/270
        exclude_types=["follow", "favourite", "reblog", "poll", "follow_request"]
    )
    notifications.reverse()
    # pp.pprint(notifications[0])
    logger.info(f"Retrieved {len(notifications)} notifications.")
    for notification in notifications:
        incoming_status = notification["status"]
        notification_id = notification["id"]
        request_id = incoming_status["id"]
        tags = [tag.name for tag in incoming_status["tags"]]
        reply_content = BeautifulSoup(incoming_status["content"],features="html.parser").get_text()
        # logger.debug([notification_id, last_parsed_notification, notification_id < last_parsed_notification])
        reg_res = term_regex.search(reply_content)
        if notification_id <= last_parsed_notification:
            logger.debug(f"skipping {notification_id} < {last_parsed_notification}")
            continue
        if not reg_res:
            logger.info(f"{request_id} is not a generation request, skipping")
            if notification_id > last_parsed_notification:
                db_r.set("last_parsed_id",notification_id)
            continue
        styles_array = parse_style(reply_content)
        # For now we're only have the same styles on each element. Later we might be able to have multiple ones.
        unformated_prompt = reg_res.group(1)
        if modifier_seek_regex.search(unformated_prompt):
            por = prompt_only_regex.search(reply_content)
            unformated_prompt = por.group(1)
        prompt = styles_array[0]["prompt"].format(p=unformated_prompt)
        model = styles_array[0]["model"]
        headers = {"apikey": os.environ['HORDE_API']}
        submit_dict = generic_submit_dict.copy()
        submit_dict["prompt"] = prompt
        submit_dict["params"] = imgen_params
        submit_dict["models"] = [model]
        logger.debug(f"Submitting: {submit_dict}")
        continue
        submit_req = requests.post(f'{HORDE_URL}/api/v2/generate/async', json = submit_dict, headers = headers)
        if submit_req.ok:
            submit_results = submit_req.json()
            logger.debug(submit_results)
            req_id = submit_results['id']
            is_done = False
            while not is_done:
                chk_req = requests.get(f'{HORDE_URL}/api/v2/generate/check/{req_id}')
                if not chk_req.ok:
                    logger.error(chk_req.text)
                    return
                chk_results = chk_req.json()
                logger.info(chk_results)
                is_done = chk_results['done']
                time.sleep(0.8)
            retrieve_req = requests.get(f'{HORDE_URL}/api/v2/generate/status/{req_id}')
            if not retrieve_req.ok:
                logger.error(retrieve_req.text)
                return
            results_json = retrieve_req.json()
            # logger.debug(results_json)
            if results_json['faulted']:
                final_submit_dict = request_data.get_submit_dict()
                if "source_image" in final_submit_dict:
                    final_submit_dict["source_image"] = f"img2img request with size: {len(final_submit_dict['source_image'])}"
                logger.error(f"Something went wrong when generating the request. Please contact the horde administrator with your request details: {final_submit_dict}")
                return
            results = results_json['generations']
            seeds = []
            filenames = []
            media_dicts = []
            for iter in range(len(results)):
                b64img = results[iter]["img"]
                base64_bytes = b64img.encode('utf-8')
                img_bytes = base64.b64decode(base64_bytes)
                img = Image.open(BytesIO(img_bytes))
                final_filename = f"{iter}_horde_generation.jpg"
                filenames.append(final_filename)
                seed = results[iter]["seed"]
                seeds.append(seed)
                img.save(final_filename)
                for iter in range(4):
                    try:
                        media_dict = mastodon.media_post(media_file=final_filename, description=f"Image with seed {seed} generated via Stable Diffusion through @stablehorde@sigmoid.social. Prompt: {prompt}")
                        break
                    except (MastodonGatewayTimeoutError, MastodonNetworkError, MastodonBadGatewayError) as e:
                        if iter >= 3:
                            raise e
                        logger.warning(f"Network error when uploading files. Retry {iter+1}/3")
                media_dict = mastodon.media_post(media_file=final_filename, description=f"Image with seed {seed} generated via Stable Diffusion through @stablehorde@sigmoid.social. Prompt: {prompt}")
                media_dicts.append(media_dict)
                logger.info(f"Uploaded {final_filename}")
        else:
            logger.error(submit_req.text)
        logger.info(f"replying to {request_id}: {reply_content}")
        tags_string = ''
        for t in tags:
            tags_string += f" #{t}"
        for iter in range(4):
            try:
                mastodon.status_reply(
                    to_status=incoming_status,
                    status=f"Here are some images matching your prompt\n\n#aiart #stablediffusion #stablehorde{tags_string}", 
                    media_ids=media_dicts,
                    spoiler_text="AI Generated Images",
                )
                break
            except (MastodonGatewayTimeoutError, MastodonNetworkError, MastodonBadGatewayError) as e:
                if iter >= 3:
                    raise e
                logger.warning(f"Network error when replying. Retry {iter+1}/3")
        # mastodon.status_reply(to_status=incoming_status, status="Here is your generation", media_ids=media_dict)
        if notification_id > last_parsed_notification:
            db_r.set("last_parsed_id",notification_id)

def get_styles():
    styles = db_r.get("styles")
    logger.info([styles, type(styles)])
    logger.debug("Downloading styles")
    for iter in range(5):
        try:
            r = requests.get("https://raw.githubusercontent.com/db0/Stable-Horde-Styles/main/styles.json")
            styles = r.json()
            # db_r.setex("styles", timedelta(minutes=30), styles)
            break
        except Exception as e:
            raise e
            if iter >= 3: 
                styles = {"raw": "{p}"}
                break
            logger.warning(f"Error during style download. Retrying ({iter+1}/3)")
            time.sleep(1)
    return(styles)

def parse_style(reply_content):
    '''retrieves the styles requested and returns a list of unformated style prompts and the models to use'''
    global style_regex
    styles = get_styles()
    style_array = []
    default_style = {
            "prompt": "{p}",
            "model": "stable_diffusion"
        }
    for iter in range(4):
        style_array.append(default_style)
    sr = style_regex.search(reply_content)
    if sr:
        requested_style = sr.group(1)
        if requested_style == "raw":
            for iter in range(4):
                style_array = [styles[requested_style]]
        else:
            for category in styles:
                if requested_style == category:
                    # TODO: For now I do all of them in a random style. Later I will switch it to a random style per image
                    random_key = random.choice(list(styles[category].keys()))
                    for iter in range(4):
                        style_array = [styles[category][key]]
                        # style_array = [styles[category].pop(key)] # for the TODO
                if requested_style in styles[category]:
                    for iter in range(4):
                        style_array = [styles[category][requested_style]]
    logger.debug(style_array)
    return(style_array)

logger.init("Mastodon Stable Horde Bot", status="Starting")
try:
    while True:
        try:
            check_for_requests()
            time.sleep(5)
        except (MastodonGatewayTimeoutError, MastodonNetworkError, MastodonBadGatewayError, MastodonAPIError):
            logger.warning("MastodonNetworkError skipping iteration")
        except MastodonNotFoundError:
            logger.warning("MastodonNotFoundError post was deleted")
except KeyboardInterrupt:
    logger.init_ok("Mastodon Stable Horde Bot", status="Exited")
""" Misc helper function and classes. """
from collections import OrderedDict
from urllib.parse import urlparse, parse_qs, urljoin
import json
import math
import base64
import uuid
import random
import time
import magic
import os
import hashlib
import re
import pyexiv2
import bcrypt
import tinycss2
from captcha.image import ImageCaptcha
from datetime import datetime, timedelta
from io import BytesIO
from PIL import Image
from bs4 import BeautifulSoup
from functools import update_wrapper
import misaka as m
from redis import Redis
import sendgrid
import config
from flask import url_for, request, g, jsonify, session
from flask_login import AnonymousUserMixin, current_user
from .caching import cache
from .socketio import socketio
from .badges import badges

from .models import Sub, SubPost, User, SiteMetadata, SubSubscriber, Message, UserMetadata
from .models import SubPostVote, SubPostComment, SubPostCommentVote, SiteLog, SubLog
from .models import SubMetadata, rconn, SubStylesheet, UserIgnores, SubUploads, SubFlair
from peewee import JOIN, fn, SQL, NodeList
import requests
import logging


from wheezy.template.engine import Engine
from wheezy.template.ext.core import CoreExtension
from wheezy.template.loader import FileLoader

engine = Engine(
    loader=FileLoader([os.path.split(__file__)[0] + '/html']),
    extensions=[CoreExtension()]
)


redis = Redis(host=config.CACHE_REDIS_HOST,
              port=config.CACHE_REDIS_PORT,
              db=config.CACHE_REDIS_DB)

# Regex that matches VALID user and sub names
allowedNames = re.compile("^[a-zA-Z0-9_-]+$")
WHITESPACE = "\u0009\u000A\u000B\u000C\u000D\u0020\u0085\u00A0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u200b\u2029\u202f\u205f\u3000\u180e\u200b\u200c\u200d\u2060\ufeff\u00AD"


class SiteUser(object):
    """ Representation of a site user. Used on the login manager. """

    def __init__(self, userclass=None, subs=[], prefs={}):
        self.user = userclass
        self.notifications = self.user.get('notifications', 0)
        self.name = self.user['name']
        self.uid = self.user['uid']
        self.prefs = [x['key'] for x in prefs]

        self.subtheme = [x['value'] for x in prefs if x['key'] == 'subtheme']
        self.subtheme = self.subtheme[0] if self.subtheme else ''

        self.subsid = []
        self.subscriptions = []
        self.blocksid = []

        self.top_bar = []
        for i in subs:
            if i['status'] == 1:
                self.subscriptions.append(i['name'])
                self.subsid.append(i['sid'])
            else:
                self.blocksid.append(i['sid'])

            if i['status'] in (1, 5):
                if i not in self.top_bar:
                    self.top_bar.append(i)

        self.score = self.user['score']
        self.given = self.user['given']
        # If status is not 0, user is banned
        if self.user['status'] != 0:
            self.is_active = False
        else:
            self.is_active = True
        self.is_active = True if self.user['status'] == 0 else False
        self.is_authenticated = True if self.user['status'] == 0 else False
        self.is_anonymous = True if self.user['status'] != 0 else False
        self.can_admin = 'admin' in self.prefs

        if (time.time() - session.get('apriv', 0) < 7200) or not (getattr(config, 'ENABLE_TOTP', False)):
            self.admin = 'admin' in self.prefs
        else:
            self.admin = False

        self.canupload = True if ('canupload' in self.prefs) or (self.admin) else False
        if getattr(config, 'ALLOW_UPLOADS', False):
            self.canupload = True

    def __repr__(self):
        return "<SiteUser {0}>".format(self.uid)

    def get_id(self):
        """ Returns the unique user id. Used on load_user """
        return self.uid

    def get_username(self):
        """ Returns the user name. Used on load_user """
        return self.name

    def get_given(self):
        return getUserGivenScore(self.uid)

    def is_mod(self, sid):
        """ Returns True if the current user is a mod of 'sub' """
        try:
            SubMetadata.get((SubMetadata.sid == sid) & (SubMetadata.key << ('mod1', 'mod2')) & (SubMetadata.value == self.uid))
            return True
        except SubMetadata.DoesNotExist:
            pass

        if self.admin:
            try:
                SiteMetadata.get((SiteMetadata.key == 'default') & (SiteMetadata.value == sid))
                return True
            except SiteMetadata.DoesNotExist:
                pass

        return False

    def is_subban(self, sub):
        """ Returns True if the current user is banned from 'sub' """
        return isSubBan(sub, self.user)

    def is_modinv(self, sub):
        """ Returns True if the current user is invited to mod of 'sub' """
        return isModInv(sub, self.uid)

    def is_admin(self):
        """ Returns true if the current user is a site admin. """
        return self.admin

    def is_topmod(self, sid):
        """ Returns True if the current user is a mod of 'sub' """
        try:
            SubMetadata.get((SubMetadata.sid == sid) & (SubMetadata.key == 'mod1') & (SubMetadata.value == self.uid))
            return True
        except SubMetadata.DoesNotExist:
            return False

    def has_subscribed(self, name):
        """ Returns True if the current user has subscribed to sub """
        if len(name) == 36:  # TODO: BAD NASTY HACK REMOVE THIS.
            return name in self.subsid
        else:
            return name in self.subscriptions

    def has_blocked(self, sid):
        """ Returns True if the current user has blocked sub """
        return sid in self.blocksid

    def new_count(self):
        """ Returns new message count """
        return self.notifications

    def likes_scroll(self):
        """ Returns true if user likes scroll """
        return 'noscroll' not in self.prefs

    def block_styles(self):
        """ Returns true if user selects to block sub styles """
        return 'nostyles' in self.prefs

    def show_nsfw(self):
        """ Returns true if user selects show nsfw posts """
        return 'nsfw' in self.prefs

    @cache.memoize(300)
    def get_user_level(self):
        """ Returns the level and xp of a user. """
        return get_user_level(self.uid, self.score)

    def get_top_bar(self):
        return self.top_bar

    def update_prefs(self, key, value, boolean=True):
        if boolean:
            value = '1' if value else '0'
        try:
            md = UserMetadata.get((UserMetadata.uid == self.uid) & (UserMetadata.key == key))
            md.value = value
            md.save()
        except UserMetadata.DoesNotExist:
            md = UserMetadata.create(uid=self.uid, key=key, value=value)
    
    @cache.memoize(30)
    def get_global_stylesheet(self):
        if self.subtheme:
            try:
                css = SubStylesheet.select().join(Sub).where(Sub.name == self.subtheme).get()
            except SubStylesheet.DoesNotExist:
                return ''
            return css.content
        return ''


class SiteAnon(AnonymousUserMixin):
    """ A subclass of AnonymousUserMixin. Used for logged out users. """
    uid = False
    subsid = []
    subscriptions = []
    blocksid = []
    prefs = []
    admin = False
    canupload = False

    def get_id(self):
        return False

    @classmethod
    def is_mod(cls, sub):
        """ Anons are not mods. """
        return False

    @classmethod
    def is_admin(cls):
        """ Anons are not admins. """
        return False

    @classmethod
    def is_topmod(cls, sub):
        """ Anons are not owners. """
        return False

    @classmethod
    def likes_scroll(cls):
        """ Anons like scroll. """
        return True

    @classmethod
    def get_top_bar(cls):
        return getDefaultSubs_list(True)

    @classmethod
    def has_subscribed(cls, sub):
        """ Anons dont get subscribe options. """
        return False

    @classmethod
    def has_blocked(cls, sub):
        """ Anons dont get blocked options. """
        return False

    @classmethod
    def is_labrat(cls):
        return False

    @classmethod
    def block_styles(cls):
        """ Anons dont get usermetadata options. """
        return False

    @classmethod
    def show_nsfw(cls):
        """ Anons dont get usermetadata options. """
        return False

    @classmethod
    def is_modinv(cls):
        """ Anons dont get see submod page. """
        return False

    @classmethod
    def is_subban(cls, sub):
        """ Anons dont get banned by default. """
        return False
    
    @classmethod
    def get_user_level(self):
        return (0, 0)
    
    @classmethod
    def get_global_stylesheet(self):
        return ''


class RateLimit(object):
    """ This class does the rate-limit magic """
    expiration_window = 10

    def __init__(self, key_prefix, limit, per, send_x_headers):
        self.reset = (int(time.time()) // per) * per + per
        self.key = key_prefix + str(self.reset)
        self.limit = limit
        self.per = per
        self.send_x_headers = send_x_headers
        p = redis.pipeline()
        p.incr(self.key)
        p.expireat(self.key, self.reset + self.expiration_window)
        self.current = min(p.execute()[0], limit)

    remaining = property(lambda x: x.limit - x.current)
    over_limit = property(lambda x: x.current >= x.limit)


def get_view_rate_limit():
    """ Returns the rate limit for the current view """
    return getattr(g, '_view_rate_limit', None)


def on_over_limit(limit):
    """ This is called when the rate limit is reached """
    return jsonify(status='error', error=['Whoa, calm down and wait a '
                                          'bit before posting again.'])


def get_ip():
    """ Tries to return the user's actual IP address. """
    if request.access_route:
        return request.access_route[-1]
    else:
        return request.remote_addr


def ratelimit(limit, per=300, send_x_headers=True,
              over_limit=on_over_limit,
              scope_func=lambda: get_ip(),
              key_func=lambda: request.endpoint):
    """ This is a decorator. It does the rate-limit magic. """
    def decorator(f):
        """ Function inside function! """
        def rate_limited(*args, **kwargs):
            """ FUNCTIONCEPTION """
            key = 'rate-limit/%s/%s/' % (key_func(), scope_func())
            rlimit = RateLimit(key, limit + 1, per, send_x_headers)
            g._view_rate_limit = rlimit
            if over_limit is not None and rlimit.over_limit:
                if not g.appconfig.get('TESTING'):
                    return over_limit(rlimit)
            return f(*args, **kwargs)
        return update_wrapper(rate_limited, f)
    return decorator


def safeRequest(url, recieve_timeout=10):
    """ Gets stuff for the internet, with timeouts and size restrictions """
    # Returns (Response, File)
    max_size = 25000000  # won't download more than 25MB
    try:
        r = requests.get(url, stream=True, timeout=recieve_timeout, headers={'User-Agent': 'Throat/1 (Phuks)'})
    except:
        raise ValueError('error fetching')
    r.raise_for_status()

    if int(r.headers.get('Content-Length', 1)) > max_size:
        raise ValueError('response too large')

    size = 0
    start = time.time()
    f = b''
    for chunk in r.iter_content(1024):
        if time.time() - start > recieve_timeout:
            raise ValueError('timeout reached')

        size += len(chunk)
        f += chunk
        if size > max_size:
            raise ValueError('response too large')
    return (r, f)

RE_AMENTION_BARE = r'(?<=^|(?<=[^a-zA-Z0-9-_\.]))((@|\/u\/|' + getattr(config, 'SUB_PREFIX', '/s') + r'\/)([A-Za-z0-9\-\_]+))'

RE_AMENTION_PRE0 = r'(?:(?:\[.+?\]\(.+?\))|(?<=^|(?<=[^a-zA-Z0-9-_\.]))(?:(?:@|\/u\/|' + getattr(config, 'SUB_PREFIX', '/s') + r'\/)(?:[A-Za-z0-9\-\_]+)))'
RE_AMENTION_PRE1 = r'(?:(\[.+?\]\(.+?\))|' + RE_AMENTION_BARE + r')'
RE_AMENTION_ESCAPED = re.compile("```.*{0}.*```|`.*?{0}.*?`|({1})".format(RE_AMENTION_PRE0, RE_AMENTION_PRE1), flags=re.MULTILINE + re.DOTALL)
RE_AMENTION_LINKS = re.compile("\[.*?({1}).*?\]\(.*?\)|({0})".format(RE_AMENTION_PRE1, RE_AMENTION_BARE), flags=re.MULTILINE + re.DOTALL)


class PhuksDown(m.SaferHtmlRenderer):
    _allowed_url_re = re.compile(r'^(https?:|\/|\#)', re.I)

    def image(self, raw_url, title='', alt=''):
        return False

    def check_url(self, url, is_image_src=False):
        return bool(self._allowed_url_re.match(url))


md = m.Markdown(PhuksDown(sanitization_mode='escape'),
                extensions=['tables', 'fenced-code', 'autolink', 'strikethrough',
                            'superscript'])


def our_markdown(text):
    """ Here we create a custom markdown function where we load all the
    extensions we need. """
    def repl(match):
        if match.group(3) is None:
            return match.group(0)

        if match.group(4) == '@':
            ln = '/u/' + match.group(5)
        else:
            ln = match.group(3)
        text = match.group(3)
        text = text.replace('_', '\_')
        text = text.replace('*', '\*')
        text = text.replace('~', '\~')
        return '[{0}]({1})'.format(text, ln)
    text = RE_AMENTION_ESCAPED.sub(repl, text)
    try:
        return md(text)
    except RecursionError:
        return '> tfw tried to break the site'


@cache.memoize(20)
def get_post_upcount(pid):
    """ Returns the upvote count """
    score = SubPostVote.select().where(SubPostVote.pid == pid).where(SubPostVote.positive == 1).count()
    return score


@cache.memoize(20)
def get_post_downcount(pid):
    """ Returns the downvote count """
    score = SubPostVote.select().where(SubPostVote.pid == pid).where(SubPostVote.positive == 0).count()
    return score


@cache.memoize(20)
def get_comment_voting(cid):
    """ Returns a tuple with the up/downvote counts """
    c = SubPostCommentVote.select().where(SubPostCommentVote.cid == cid)
    upvote = 0
    downvote = 0
    for i in c:
        if i.positive:
            upvote += 1
        else:
            downvote += 1
    return (upvote, downvote)


def isMod(sid, uid):
    """ Returns True if 'user' is a mod of 'sub' """
    try:
        SubMetadata.get((SubMetadata.sid == sid) & (SubMetadata.key << ('mod1', 'mod2')) & (SubMetadata.value == uid))
        return True
    except SubMetadata.DoesNotExist:
        return False


@cache.memoize(30)
def isSubBan(sub, user):
    """ Returns True if 'user' is banned 'sub' """
    if isinstance(sub, dict):
        sid = sub['sid']
    else:
        sid = sub.sid
    try:
        SubMetadata.get((SubMetadata.sid == sid) & (SubMetadata.key == "ban") & (SubMetadata.value == user['uid']))
        return True
    except SubMetadata.DoesNotExist:
        return False


def isModInv(sid, uid):
    """ Returns True if 'user' is a invited to mod of 'sub' """
    try:
        SubMetadata.get((SubMetadata.sid == sid) & (SubMetadata.key == 'mod2i') & (SubMetadata.value == uid))
        return True
    except SubMetadata.DoesNotExist:
        return False


def getPostFlair(post):
    """ Returns true if the post has available flair """
    # The mere existence of this function pains me.
    return post['flair']

def getSubFlairs(sid):
    return SubFlair.select().where(SubFlair.sid == sid)


@cache.memoize(600)
def getDefaultSubs():
    """ Returns a list of all the default subs """
    defaults = [x.value for x in SiteMetadata.select().where(SiteMetadata.key == 'default')]
    defaults = Sub.select(Sub.sid, Sub.name).where(Sub.sid << defaults)
    return list(defaults.dicts())


@cache.memoize(600)
def getDefaultSubs_list(ext=False):
    """ Returns a list of all the default subs """
    defaults = getDefaultSubs()
    if not ext:
        defaults = sorted(defaults, key=str.lower)
    else:
        defaults = sorted(defaults, key=lambda x: x['name'].lower())
    return defaults


def enableInviteCode():
    """ Returns true if invite code is required to register """
    try:
        xm = SiteMetadata.get(SiteMetadata.key == 'useinvitecode')
        return False if xm.value == '0' else True
    except SiteMetadata.DoesNotExist:
        return False


def sendMail(to, subject, content):
    """ Sends a mail through sendgrid """
    sg = sendgrid.SendGridAPIClient(api_key=config.SENDGRID_API_KEY)

    from_email = sendgrid.Email(config.SENDGRID_DEFAULT_FROM)
    to_email = sendgrid.Email(to)
    content = sendgrid.helpers.mail.Content('text/html', content)

    mail = sendgrid.helpers.mail.Mail(from_email, subject, to_email,
                                      content)

    sg.client.mail.send.post(request_body=mail.get())


# TODO: Make all these functions one.
def getYoutubeID(url):
    """ Returns youtube ID for a video. """
    url = urlparse(url)
    if url.hostname == 'youtu.be':
        return url.path[1:]
    if url.hostname in ['www.youtube.com', 'youtube.com']:
        if url.path == '/watch':
            p = parse_qs(url.query)
            return p['v'][0]
        if url.path[:3] == '/v/':
            return url.path.split('/')[2]
    # fail?
    return None


def moddedSubCount(uid):
    """ Returns the number of subs a user is modding """
    sub = SubMetadata.select().where(SubMetadata.value == uid).where(SubMetadata.key << ('mod1', 'mod2'))
    return sub.count()


def workWithMentions(data, receivedby, post, sub, cid=None, c_user=current_user):
    """ Does all the job for mentions """
    mts = re.findall(RE_AMENTION_LINKS, data)
    if isinstance(sub, Sub):
        subname = sub.name
    else:
        subname = sub['name']
    if mts:
        mts = list(set(mts))  # Removes dupes
        clean_mts = []

        for m in mts:
            t = [x for x in m if x != '']
            if len(t) >= 3:
                clean_mts.append(t)  
        
        mts = [x[-1] for x in clean_mts if x[-2] == "/u/" or x[-2] == "@"]
        
        usr_level = current_user.get_user_level()[0]
        if usr_level > 30:
            mts = mts[:15]
        elif usr_level > 20:
            mts = mts[:10]
        else:
            mts = mts[:5]

        for mtn in mts:
            # Send notifications.
            try:
                user = User.get(User.name == mtn)
            except User.DoesNotExist:
                continue
            if user.uid != c_user.uid and user.uid != receivedby:
                # Checks done. Send our shit
                if cid:
                    link = url_for('sub.view_perm', pid=post.pid, sub=subname, cid=cid)
                else:
                    link = url_for('sub.view_post', pid=post.pid, sub=subname)
                create_message(c_user.uid, user.uid,
                               subject="You've been tagged in a post",
                               content="@{0} tagged you in [{1}]({2})"
                               .format(c_user.name, "Here: " + post.title, link),
                               link=link, mtype=8)
                socketio.emit('notification',
                              {'count': get_notification_count(user.uid)},
                              namespace='/snt',
                              room='user' + user.uid)


def getUser(uid):
    """ Returns user from uid, db proxy now """
    return User.select().where(User.uid == uid).dicts().get()


@cache.memoize(5)
def getDomain(link):
    """ Gets Domain from url """
    x = urlparse(link)
    return x.netloc


@cache.memoize(300)
def isImage(link):
    """ Returns True if link ends with img suffix """
    suffix = ('.png', '.jpg', '.gif', '.tiff', '.bmp', '.jpeg')
    return link.lower().endswith(suffix)


@cache.memoize(300)
def isGifv(link):
    """ Returns True if link ends with video suffix """
    return link.lower().endswith('.gifv')


@cache.memoize(300)
def isVideo(link):
    """ Returns True if link ends with video suffix """
    suffix = ('.mp4', '.webm')
    return link.lower().endswith(suffix)


@cache.memoize(10)
def get_user_level(uid, score=None):
    """ Returns the user's level and XP as a tuple (level, xp) """
    if not score:
        user = User.get(User.uid == uid)
        xp = user.score
    else:
        xp = score
    badges = getUserBadges(uid)
    for badge in badges:
        xp += badge['score']
    if xp <= 0:  # We don't want to do the sqrt of a negative number
        return (0, xp)
    level = math.sqrt(xp / 10)
    return (int(level), xp)


def _image_entropy(img):
    """calculate the entropy of an image"""
    hist = img.histogram()
    hist_size = sum(hist)
    hist = [float(h) / hist_size for h in hist]

    return -sum(p * math.log(p, 2) for p in hist if p != 0)


THUMB_NAMESPACE = uuid.UUID('f674f09a-4dcf-4e4e-a0b2-79153e27e387')
FILE_NAMESPACE = uuid.UUID('acd2da84-91a2-4169-9fdb-054583b364c4')


def get_thumbnail(link):
    """ Tries to fetch a thumbnail """
    # 1 - Check if it's an image
    try:
        req = safeRequest(link)
    except (requests.exceptions.RequestException, ValueError):
        return ''
    ctype = req[0].headers.get('content-type', '').split(";")[0].lower()
    good_types = ['image/gif', 'image/jpeg', 'image/png']
    if ctype in good_types:
        # yay, it's an image!!1
        # Resize
        im = Image.open(BytesIO(req[1])).convert('RGB')
    elif ctype == 'text/html':
        # Not an image!! Let's try with OpenGraph
        try:
            og = BeautifulSoup(req[1], 'lxml')
        except:
            # If it errors here it's probably because lxml is not installed.
            logging.warn('Thumbnail fetch failed. Is lxml installed?')
            return ''
        try:
            img = urljoin(link, og('meta', {'property': 'og:image'})[0].get('content'))
            req = safeRequest(img)
            im = Image.open(BytesIO(req[1])).convert('RGB')
        except (OSError, ValueError, IndexError):
            # no image, try fetching just the favicon then
            try:
                img = urljoin(link, og('link', {'rel': 'icon'})[0].get('href'))
                req = safeRequest(img)
                im = Image.open(BytesIO(req[1]))
                n_im = Image.new("RGBA", im.size, "WHITE")
                n_im.paste(im, (0, 0), im)
                im = n_im.convert("RGB")
            except (OSError, ValueError, IndexError):
                return ''
    else:
        return ''

    x, y = im.size
    while y > x:
        slice_height = min(y - x, 10)
        bottom = im.crop((0, y - slice_height, x, y))
        top = im.crop((0, 0, x, slice_height))

        if _image_entropy(bottom) < _image_entropy(top):
            im = im.crop((0, 0, x, y - slice_height))
        else:
            im = im.crop((0, slice_height, x, y))

        x, y = im.size

    im.thumbnail((70, 70), Image.ANTIALIAS)
    im.seek(0)
    md5 = hashlib.md5(im.tobytes())
    filename = str(uuid.uuid5(THUMB_NAMESPACE, md5.hexdigest())) + '.jpg'
    im.seek(0)
    if not os.path.isfile(os.path.join(config.THUMBNAILS, filename)):
        im.save(os.path.join(config.THUMBNAILS, filename), "JPEG", optimize=True, quality=85)
    im.close()

    return filename

# -------------
# admin stuff
# -------------

def getAdminUserBadges():
    um = UserMetadata.select().where(UserMetadata.key == 'badge').dicts()
    ret = []
    for bg in um:
        if badges.get(bg['value']):
            l = badges[bg['value']]
            ret.append(l)
    return ret


# -----------------------------------
# Stuff after this line was checked™
# -----------------------------------


@cache.memoize(300)
def getTodaysTopPosts():
    """ Returns top posts in the last 24 hours """
    td = datetime.utcnow() - timedelta(days=1)
    posts = (SubPost.select(SubPost.pid, Sub.name.alias('sub'), SubPost.title, SubPost.posted, SubPost.score)
                    .where(SubPost.posted > td).order_by(SubPost.score.desc()).limit(5)
                    .join(Sub, JOIN.LEFT_OUTER).dicts())
    top_posts = []
    for p in posts:
        top_posts.append(p)
    return top_posts


@cache.memoize(10)
def getSubOfTheDay():
    daysub = rconn.get('daysub')
    if not daysub:
        try:
            daysub = Sub.select(Sub.sid, Sub.name, Sub.title).order_by(fn.Rand()).get()
        except Sub.DoesNotExist:  # No subs
            return False
        today = datetime.utcnow()
        tomorrow = datetime(year=today.year, month=today.month, day=today.day) + timedelta(seconds=86400)
        timeuntiltomorrow = tomorrow - today
        rconn.setex('daysub', value=daysub.sid, time=timeuntiltomorrow)
    else:
        try:
            daysub = Sub.select(Sub.name, Sub.title).where(Sub.sid == daysub).get()
        except Sub.DoesNotExist:  # ???
            return False
    return daysub


def getChangelog():
    """ Returns most recent changelog post """
    if not config.CHANGELOG_SUB:
        return None
    td = datetime.utcnow() - timedelta(days=15)
    changepost = (SubPost.select(Sub.name.alias('sub'), SubPost.pid, SubPost.title, SubPost.posted)
                         .where(SubPost.posted > td).where(SubPost.sid == config.CHANGELOG_SUB)
                         .join(Sub, JOIN.LEFT_OUTER).order_by(SubPost.pid.desc()).dicts())

    try:
        return changepost.get()
    except SubPost.DoesNotExist:
        return None


def getSinglePost(pid):
    if current_user.is_authenticated:
        posts = SubPost.select(SubPost.nsfw, SubPost.sid, SubPost.content, SubPost.pid, SubPost.title, SubPost.posted, SubPost.score, SubPost.upvotes, SubPost.downvotes,
                               SubPost.thumbnail, SubPost.link, User.name.alias('user'), Sub.name.alias('sub'), SubPost.flair, SubPost.edited,
                               SubPost.comments, SubPostVote.positive, User.uid, User.status.alias('userstatus'), SubPost.deleted, SubPost.ptype)
        posts = posts.join(SubPostVote, JOIN.LEFT_OUTER, on=((SubPostVote.pid == SubPost.pid) & (SubPostVote.uid == current_user.uid))).switch(SubPost)
    else:
        posts = SubPost.select(SubPost.nsfw, SubPost.sid, SubPost.content, SubPost.pid, SubPost.title, SubPost.posted, SubPost.score, SubPost.upvotes, SubPost.downvotes,
                               SubPost.thumbnail, SubPost.link, User.name.alias('user'), Sub.name.alias('sub'), SubPost.flair, SubPost.edited,
                               SubPost.comments, User.uid, User.status.alias('userstatus'), SubPost.deleted, SubPost.ptype)
    posts = posts.join(User, JOIN.LEFT_OUTER).switch(SubPost).join(Sub, JOIN.LEFT_OUTER).where(SubPost.pid == pid).dicts().get()
    return posts


def postListQueryBase(*extra, nofilter=False, noAllFilter=False, noDetail=False, adminDetail=False):
    if current_user.is_authenticated and not noDetail:
        posts = SubPost.select(SubPost.nsfw, SubPost.content, SubPost.pid, SubPost.title, SubPost.posted, SubPost.deleted, SubPost.score, SubPost.ptype,
                               SubPost.thumbnail, SubPost.link, User.name.alias('user'), Sub.name.alias('sub'), SubPost.flair, SubPost.edited, Sub.sid,
                               SubPost.comments, SubPostVote.positive, User.uid, User.status.alias('userstatus'), *extra)
        posts = posts.join(SubPostVote, JOIN.LEFT_OUTER, on=((SubPostVote.pid == SubPost.pid) & (SubPostVote.uid == current_user.uid))).switch(SubPost)
    else:
        posts = SubPost.select(SubPost.nsfw, SubPost.content, SubPost.pid, SubPost.title, SubPost.posted, SubPost.deleted, SubPost.score, SubPost.ptype,
                               SubPost.thumbnail, SubPost.link, User.name.alias('user'), Sub.name.alias('sub'), SubPost.flair, SubPost.edited, Sub.sid,
                               SubPost.comments, User.uid, User.status.alias('userstatus'), *extra)
    posts = posts.join(User, JOIN.LEFT_OUTER).switch(SubPost).join(Sub, JOIN.LEFT_OUTER)
    if not adminDetail:
        posts = posts.where(SubPost.deleted == 0)
    if not noAllFilter and not nofilter:
        if current_user.is_authenticated and current_user.blocksid:
            posts = posts.where(SubPost.sid.not_in(current_user.blocksid))
    if (not nofilter) and ((not current_user.is_authenticated) or ('nsfw' not in current_user.prefs)):
        posts = posts.where(SubPost.nsfw == 0)
    return posts


def postListQueryHome(noDetail=False, nofilter=False):
    if current_user.is_authenticated:
        return (postListQueryBase(noDetail=noDetail, nofilter=nofilter).where(SubPost.sid << current_user.subsid))
    else:
        return postListQueryBase(noDetail=noDetail, nofilter=nofilter).join(SiteMetadata, JOIN.LEFT_OUTER, on=(SiteMetadata.key == 'default')).where(SubPost.sid == SiteMetadata.value)


def getPostList(baseQuery, sort, page):
    if sort == "hot":
        if "Postgres" in getattr(config, 'DATABASE', {}).get('engine', ''):
            hot = SubPost.score * 20 + (fn.EXTRACT(NodeList((SQL('EPOCH FROM'), SubPost.posted))) - 1134028003 ) / 1500
            posts = baseQuery.order_by(hot.desc()).limit(100).paginate(page, 25)
        else:
            posts = baseQuery.order_by((SubPost.score * 20 + (fn.Unix_Timestamp(SubPost.posted) - 1134028003) / 1500).desc()).limit(100).paginate(page, 25)
    elif sort == "top":
        posts = baseQuery.order_by(SubPost.score.desc()).paginate(page, 25)
    elif sort == "new":
        posts = baseQuery.order_by(SubPost.pid.desc()).paginate(page, 25)
    return posts


@cache.memoize(600)
def getAnnouncementPid():
    return SiteMetadata.select().where(SiteMetadata.key == 'announcement').get()


def getAnnouncement():
    """ Returns sitewide announcement post or False """
    try:
        ann = getAnnouncementPid()
        if not ann.value:
            return False
        return postListQueryBase(nofilter=True).where(SubPost.pid == ann.value).dicts().get()
    except SiteMetadata.DoesNotExist:
        return False


@cache.memoize(5)
def getWikiPid(sid):
    """ Returns a list of wickied SubPosts """
    x = SubMetadata.select(SubMetadata.value).where(SubMetadata.sid == sid).where(SubMetadata.key == 'wiki').dicts()
    return [int(y['value']) for y in x]


@cache.memoize(5)
def getStickyPid(sid):
    """ Returns a list of stickied SubPosts """
    x = SubMetadata.select(SubMetadata.value).where(SubMetadata.sid == sid).where(SubMetadata.key == 'sticky').dicts()
    return [int(y['value']) for y in x]


def getStickies(sid):
    sp = getStickyPid(sid)
    posts = postListQueryBase().where(SubPost.pid << sp).dicts()
    return posts


def load_user(user_id):
    user = User.select(fn.Count(Message.mid).alias('notifications'),
                    User.given, User.score, User.name, User.uid, User.status, User.email)
    user = user.join(Message, JOIN.LEFT_OUTER, on=((Message.receivedby == User.uid) & (Message.mtype != 6) & (Message.mtype != 9) & (Message.mtype != 41) & Message.read.is_null(True))).switch(User)
    user = user.group_by(User.uid).where(User.uid == user_id).dicts().get()
    
    if request.path == '/socket.io/':
        return SiteUser(user, [], [])
    else:
        prefs = UserMetadata.select(UserMetadata.key, UserMetadata.value).where(UserMetadata.uid == user_id)
        prefs = prefs.where((UserMetadata.value == '1') | (UserMetadata.key == 'subtheme')).dicts()

        try:
            subs = SubSubscriber.select(SubSubscriber.sid, Sub.name, SubSubscriber.status).join(Sub, on=(Sub.sid == SubSubscriber.sid)).switch(SubSubscriber).where(SubSubscriber.uid == user_id)
            subs = subs.order_by(SubSubscriber.order.asc()).dicts()
            return SiteUser(user, subs, prefs)
        except User.DoesNotExist:
            return None


def get_notification_count(uid):
    return Message.select().where((Message.receivedby == uid) & (Message.mtype != 6) & (Message.mtype != 9) & (Message.mtype != 41) & Message.read.is_null(True)).count()


def get_errors(form):
    """ A simple function that returns a list with all the form errors. """
    if request.method == 'GET':
        return []
    ret = []
    for field, errors in form.errors.items():
        for error in errors:
            ret.append(u"Error in the '%s' field - %s" % (
                getattr(form, field).label.text,
                error))
    return ret


def build_tree(tuff, root=None):
    """ Builds a comment tree """
    str = []
    for i in tuff[::]:
        if i['parentcid'] == root:
            tuff.remove(i)
            i['children'] = build_tree(tuff, root=i['cid'])
            str.append(i)

    return str


def count_childs(tuff, depth=0):
    """ Counts number of children in tree """
    if depth > 7:
        return 0
    cnt = 0
    for i in tuff:
        if i.get('morechildren') is not None:
            cnt += i['morechildren']
        else:
            cnt += 1
            cnt += count_childs(i['children'], depth=depth + 1)
    return cnt


def trim_tree(tuff, depth=0, pageno=1):
    """ Trims down the tree. """
    k = 0
    res = []
    for i in tuff:
        perpage = 8 if i['parentcid'] is None else 5
        k += 1
        if k < ((pageno - 1) * perpage) + 1:
            continue
        if k <= pageno * perpage:
            if depth > 1:
                i['morechildren'] = count_childs(i['children'])
                i['children'] = []
            else:
                i['children'] = trim_tree(i['children'], depth=depth + 1)
            res.append(i)
        else:
            res.append({'moresbling': count_childs(tuff[pageno * perpage:]), 'cid': 0, 'parent': i['parentcid']})
            break
    return res


def expand_comment_tree(comsx):
    coms = comsx[0]
    expcomms = SubPostComment.select(SubPostComment.cid, SubPostComment.content, SubPostComment.lastedit,
                                     SubPostComment.score, SubPostComment.status, SubPostComment.time, SubPostComment.pid,
                                     User.name.alias('username'), SubPostCommentVote.positive, SubPostComment.uid,
                                     User.status.alias('userstatus'), SubPostComment.upvotes, SubPostComment.downvotes)
    expcomms = expcomms.join(User, on=(User.uid == SubPostComment.uid)).switch(SubPostComment)
    expcomms = expcomms.join(SubPostCommentVote, JOIN.LEFT_OUTER, on=((SubPostCommentVote.uid == current_user.get_id()) & (SubPostCommentVote.cid == SubPostComment.cid)))
    expcomms = expcomms.where(SubPostComment.cid << comsx[1]).dicts()
    lcomms = {}

    for k in expcomms:
        lcomms[k['cid']] = k

    def i_like_recursion(xm, depth=0):
        if depth > 30:
            return []
        ret = []
        for dom in xm:
            if dom['cid'] == 0:
                ret.append(dom)
                continue
            fmt = {**dom, **lcomms[dom['cid']]}
            if depth == 2 and len(fmt['children']) != 0:
                fmt['imorechildren'] = True
            else:
                fmt['children'] = i_like_recursion(fmt['children'], depth=depth + 1)
            ret.append(fmt)
        return ret

    dcom = []
    for com in coms:
        if not com.get('moresbling'):
            fmt = {**com, **lcomms[com['cid']]}
            fmt['children'] = i_like_recursion(fmt['children'])
            dcom.append(fmt)
        else:
            dcom.append(com)
    return dcom


def build_comment_tree(stuff, root=None, perpage=5, pageno=1):
    cmxk = trim_tree(build_tree(list(stuff), root=root), pageno=pageno)

    def get_cids(tree, c=[]):
        for i in tree:
            if i['cid'] != 0:
                c.append(i['cid'])
                get_cids(i['children'], c)
        return c
    cids = get_cids(cmxk)
    return (cmxk, cids)


def get_post_comments(pid):
    """ Returns the comments for a post `pid`"""
    cmskel = SubPostComment.select(SubPostComment.cid, SubPostComment.parentcid)
    cmskel = cmskel.where(SubPostComment.pid == pid).order_by(SubPostComment.score.desc()).dicts()

    if cmskel.count() == 0:
        return []

    return expand_comment_tree(build_comment_tree(cmskel, perpage=8))


# messages

def getMessagesIndex(page):
    """ Returns messages inbox """
    try:
        msg = Message.select(Message.mid, User.name.alias('username'), Message.sentby, Message.receivedby, Message.subject, Message.content, Message.posted, Message.read, Message.mtype, Message.mlink)
        msg = msg.join(User, JOIN.LEFT_OUTER, on=(User.uid == Message.sentby)).where(Message.mtype == 1).where(Message.receivedby == current_user.get_id()).order_by(Message.mid.desc()).paginate(page, 20).dicts()
    except Message.DoesNotExist:
        return False
    return msg


def getMentionsIndex(page):
    """ Returns user mentions inbox """
    try:
        msg = Message.select(Message.mid, User.name.alias('username'), Message.sentby, Message.receivedby, Message.subject, Message.content, Message.posted, Message.read, Message.mtype, Message.mlink)
        msg = msg.join(User, JOIN.LEFT_OUTER, on=(User.uid == Message.sentby)).where(Message.mtype == 8).where(Message.receivedby == current_user.get_id()).order_by(Message.mid.desc()).paginate(page, 20).dicts()
    except Message.DoesNotExist:
        return False
    return msg


def getMessagesSent(page):
    """ Returns messages sent """
    try:
        msg = Message.select(Message.mid, Message.sentby, User.name.alias('username'), Message.subject, Message.content, Message.posted, Message.read, Message.mtype, Message.mlink)
        msg = msg.join(User, JOIN.LEFT_OUTER, on=(User.uid == Message.receivedby)).where(Message.mtype == 1).where(Message.sentby == current_user.get_id()).order_by(Message.mid.desc()).paginate(page, 20).dicts()
    except Message.DoesNotExist:
        return False
    return msg


def getMessagesModmail(page):
    """ Returns modmail """
    try:
        msg = Message.select(Message.mid, User.name.alias('username'), Message.receivedby, Message.subject, Message.content, Message.posted, Message.read, Message.mtype, Message.mlink)
        msg = msg.join(User, on=(User.uid == Message.sentby)).where(Message.mtype << [2, 7, 11]).where(Message.receivedby == current_user.get_id()).order_by(Message.mid.desc()).paginate(page, 20).dicts()
    except Message.DoesNotExist:
        return False
    return msg


def getMessagesSaved(page):
    """ Returns saved messages """
    try:
        msg = Message.select(Message.mid, User.name.alias('username'), Message.receivedby, Message.subject, Message.content, Message.posted, Message.read, Message.mtype, Message.mlink)
        msg = msg.join(User, on=(User.uid == Message.sentby)).where(Message.mtype == 9).where(Message.receivedby == current_user.get_id()).order_by(Message.mid.desc()).paginate(page, 20).dicts()
    except Message.DoesNotExist:
        return False
    return msg


def getMsgCommReplies(page):
    """ Returns comment replies messages """
    try:
        msg = Message.select(Message.mid, User.name.alias('username'), Message.sentby, Message.receivedby, Message.subject,
                             Message.posted, Message.read, Message.mtype, Message.mlink, SubPostComment.pid, SubPostComment.content,
                             SubPostComment.score, SubPostCommentVote.positive, Sub.name.alias('sub'))
        msg = msg.join(SubPostComment, on=SubPostComment.cid == Message.mlink).join(SubPost).join(Sub).switch(SubPostComment).join(SubPostCommentVote, JOIN.LEFT_OUTER, on=((SubPostCommentVote.uid == current_user.get_id()) & (SubPostCommentVote.cid == Message.mlink)))
        msg = msg.join(User, on=(User.uid == Message.sentby)).where(Message.mtype == 5).where(Message.receivedby == current_user.get_id()).order_by(Message.mid.desc()).paginate(page, 20).dicts()
    except Message.DoesNotExist:
        return False
    return msg


def getMsgPostReplies(page):
    """ Returns post replies messages """
    try:
        msg = Message.select(Message.mid, User.name.alias('username'), Message.sentby, Message.receivedby, Message.subject,
                             Message.posted, Message.read, Message.mtype, Message.mlink, SubPostCommentVote.positive, SubPostComment.pid,
                             SubPostComment.score, SubPostComment.content, Sub.name.alias('sub'))
        msg = msg.join(SubPostComment, on=SubPostComment.cid == Message.mlink).join(SubPost).join(Sub).switch(SubPostComment).join(SubPostCommentVote, JOIN.LEFT_OUTER, on=((SubPostCommentVote.uid == current_user.get_id()) & (SubPostCommentVote.cid == Message.mlink)))
        msg = msg.join(User, on=(User.uid == Message.sentby)).where(Message.mtype == 4).where(Message.receivedby == current_user.get_id()).order_by(Message.mid.desc()).paginate(page, 20).dicts()
    except Message.DoesNotExist:
        return False
    return msg


# user comments


def getUserComments(uid, page):
    """ Returns comments for a user """
    try:
        com = SubPostComment.select(Sub.name.alias('sub'), SubPost.title, SubPostComment.cid, SubPostComment.pid, SubPostComment.uid, SubPostComment.time, SubPostComment.lastedit, SubPostComment.content, SubPostComment.status, SubPostComment.score, SubPostComment.parentcid)
        com = com.join(SubPost).switch(SubPostComment).join(Sub, on=(Sub.sid == SubPost.sid))
        com = com.where(SubPostComment.uid == uid).where(SubPostComment.status.is_null()).order_by(SubPostComment.time.desc()).paginate(page, 20).dicts()
    except com.DoesNotExist:
        return False
    return com


def getUserBadges(uid):
    um = UserMetadata.select().where((UserMetadata.uid == uid) & (UserMetadata.key == 'badge')).dicts()
    ret = []
    for bg in um:
        if badges.get(bg['value']):
            ret.append(badges[bg['value']])
    return ret


def ktime0():
    print('bvo')
    g.boolkk = time.time()


def ktime():
    print('partial load: ', int((time.time() - g.boolkk) * 1000))


def upload_file(max_size=16580608):
    if not current_user.canupload:
        return False

    if 'files' not in request.files:
        return False

    ufile = request.files.getlist('files')[0]
    if ufile.filename == '':
        return False

    mtype = magic.from_buffer(ufile.read(1024), mime=True)

    if mtype == 'image/jpeg':
        extension = '.jpg'
    elif mtype == 'image/png':
        extension = '.png'
    elif mtype == 'image/gif':
        extension = '.gif'
    elif mtype == 'video/mp4':
        extension = '.mp4'
    elif mtype == 'video/webm':
        extension = '.webm'
    else:
        return False
    ufile.seek(0)
    md5 = hashlib.md5()
    while True:
        data = ufile.read(65536)
        if not data:
            break
        md5.update(data)

    f_name = str(uuid.uuid5(FILE_NAMESPACE, md5.hexdigest())) + extension
    ufile.seek(0)

    if not os.path.isfile(os.path.join(config.STORAGE, f_name)):
        ufile.save(os.path.join(config.STORAGE, f_name))
        fsize = os.stat(os.path.join(config.STORAGE, f_name)).st_size
        if fsize > max_size:  # Max file size exceeded
            os.remove(os.path.join(config.STORAGE, f_name))
            return False
        # remove metadata
        if mtype not in ('image/gif', 'video/mp4', 'video/webm'):  # Apparently we cannot write to gif images
            md = pyexiv2.ImageMetadata(os.path.join(config.STORAGE, f_name))
            md.read()
            for k in (md.exif_keys + md.iptc_keys + md.xmp_keys):
                del md[k]
            md.write()
    return f_name


def getSubData(sid, simple=False, extra=False):
    sdata = SubMetadata.select().where(SubMetadata.sid == sid)
    data = {'mods': [], 'mod2': [], 'mod2i': [], 'xmod2': [], 'sticky': []}
    for p in sdata:
        if p.key in ['mod2', 'tag', 'ban', 'mod2i', 'xmod2', 'sticky']:
            if data.get(p.key):
                data[p.key].append(p.value)
            else:
                data[p.key] = [p.value]
        else:
            data[p.key] = p.value

    if not simple:
        try:
            data['videomode']
        except KeyError:
            data['videomode'] = 0

        try:
            data['wiki']
        except KeyError:
            data['wiki'] = ''
        
        if extra:
            if data.get('xmod2', []) != []:
                try:
                    data['xmods'] = User.select(User.uid, User.name).where((User.uid << data['xmod2']) & (User.status == 0)).dicts()
                except User.DoesNotExist:
                    data['xmods'] = []

            if data.get('mod2i', []) != []:
                try:
                    data['modi'] = User.select(User.uid, User.name).where((User.uid << data['mod2i']) & (User.status == 0)).dicts()
                except User.DoesNotExist:
                    data['modi'] = []

        bmodquery = data['mod2'] + [data.get('mod1'), data.get('mod')]
        bmodquery = set([x for x in bmodquery if x])
        modquery = User.select(User.uid, User.name, User.status).where(User.uid << bmodquery).dicts()

        data['mods'] = [x for x in modquery if x['uid'] in data.get('mod2', [])]
        data['owner'] = [x for x in modquery if x['uid'] in data.get('mod1', [])]
        data['creator'] = [x for x in modquery if x['uid'] == data.get('mod', '')]
        if data['owner']:
            data['owner'] = data['owner'][0] if data['owner'][0]['status'] == 0 else None

        if not data['owner']:
            data['owner'] = {'uid': 1, 'name': 'Phuks'}  # TODO: move name to config

        if data['creator']:
            data['creator'] = data['creator'][0] if data['creator'][0]['status'] == 0 else {'uid': '0', 'name': '[Deleted]'}

        if not data['creator']:
            data['creator'] = {'uid': '0', 'name': '[Nobody]'}  # TODO: move name to config

        try:
            data['stylesheet'] = SubStylesheet.get(SubStylesheet.sid == sid).content
        except SubStylesheet.DoesNotExist:
            data['stylesheet'] = ''
    return data


@cache.memoize(5)
def getUserGivenScore(uid):
    pos = SubPostVote.select().where(SubPostVote.uid == uid).where(SubPostVote.positive == 1).count()
    neg = SubPostVote.select().where(SubPostVote.uid == uid).where(SubPostVote.positive == 0).count()
    cpos = SubPostCommentVote.select().where(SubPostCommentVote.uid == uid).where(SubPostCommentVote.positive == 1).count()
    cneg = SubPostCommentVote.select().where(SubPostCommentVote.uid == uid).where(SubPostCommentVote.positive == 0).count()

    return (pos + cpos, neg + cneg, (pos + cpos) - (neg + cneg))


# Note for future self:
#  We keep constantly switching from camelCase to snake_case for function names.
#  For fucks sake make your mind.
def get_ignores(uid):
    return [x.target for x in UserIgnores.select().where(UserIgnores.uid == uid)]


def validate_password(usr, passwd):
    """ Returns True if `passwd` is valid for `usr`. `usr` is a db object. """
    if usr.crypto == 1:  # bcrypt
        thash = bcrypt.hashpw(passwd.encode('utf-8'),
                              usr.password.encode('utf-8'))
        if thash == usr.password.encode('utf-8'):
            return True
    return False


def iter_validate_css(obj, uris):
    for x in obj:
        if x.__class__.__name__ == "URLToken":
            if x.value.startswith('%%') and x.value.endswith('%%'):
                token = x.value.replace('%%', '').strip()
                if uris.get(token):
                    x.value = uris.get(token)
            else:
                return ("URLs not allowed, uploaded files only", x.source_column, x.source_line)
        elif x.__class__.__name__ == "CurlyBracketsBlock":
            return iter_validate_css(x.content)
    return True


def validate_css(css, sid):
    """ Validates CSS. Returns parsed stylesheet or (errcode, col, line)"""
    st = tinycss2.parse_stylesheet(css, skip_comments=True, skip_whitespace=True)
    # create a map for uris.
    uris = {}
    for su in SubUploads.select().where(SubUploads.sid == sid):
        uris[su.name] = config.STORAGE_HOST + su.fileid
    for x in st:
        if x.__class__.__name__ == "AtRule":
            if x.at_keyword.lower() == "import":
                return ("@import token not allowed", x.source_column, x.source_line)  # we do not allow @import
        elif x.__class__.__name__ == "QualifiedRule":  # down the hole we go.
            m = iter_validate_css(x.content, uris)
            if m is not True:
                return m

    try:
        return (0, tinycss2.serialize(st))
    except TypeError:
        return ("Invalid CSS", 0, 0)


@cache.memoize(3)
def get_security_questions():
    """ Returns a list of tuples containing security questions and answers """
    qs = SiteMetadata.select().where(SiteMetadata.key == 'secquestion').dicts()

    return [(str(x['xid']) + '|' + x['value']).split('|') for x in qs]  # hacky separator.


def pick_random_security_question():
    """ Picks a random security question and saves the answer on the session """
    sc = random.choice(get_security_questions())
    session['sa'] = sc[2]
    return sc[1]


def create_message(mfrom, to, subject, content, link, mtype):
    """ Creates a message. """
    posted = datetime.utcnow()
    m = Message.create(sentby=mfrom, receivedby=to, subject=subject, mlink=link, content=content, posted=posted, mtype=mtype)
    return m


try:
    MOTTOS = json.loads(open('phuks.txt').read())
except FileNotFoundError:
    MOTTOS = []


def get_motto():
    return random.choice(MOTTOS)


def populate_feed(feed, posts):
    """ Populates an AtomFeed `feed` with posts """
    for post in posts:
        content = "<table><tr>"
        url = url_for('sub.view_post', sub=post['sub'], pid=post['pid'], _external=True)

        if post['thumbnail']:
            content += '<td><a href=' + url + '"><img src="' + config.THUMBNAIL_HOST + post['thumbnail'] + '" alt="' + post['title'] + '"/></a></td>'
        content +=  '<td>Submitted by <a href=/u/' + post['user'] + '>' + post['user'] + '</a><br/>' + our_markdown(post['content'])
        if post['link']:
            content += '<a href="' + post['link'] + '">[link]</a> '
        content += '<a href="' + url + '">[comments]</a></td></tr></table>'
        feed.add(post['title'], content,
                 content_type='html',
                 author=post['user'],
                 url=url,
                 updated=post['posted'] if not post['edited'] else post['edited'],
                 published=post['posted'])

    return feed


def metadata_to_dict(metadata):
    """ Transforms metadata query objects into dicts """
    res = {}
    for m in metadata:
        if m.value == '0':
            val = False
        elif m.value == '1':
            val = True
        else:
            val = m.value 
        if m.key not in res:
            res[m.key] = val
        else:
            if not isinstance(res[m.key], list):
                res[m.key] = [res[m.key]]
            res[m.key].append(val)
    
    return res


# Log types
LOG_TYPE_USER = 10
LOG_TYPE_USER_BAN = 19

LOG_TYPE_SUB_CREATE = 20
LOG_TYPE_SUB_SETTINGS = 21
LOG_TYPE_SUB_BAN = 22
LOG_TYPE_SUB_UNBAN = 23
LOG_TYPE_SUB_MOD_INVITE = 24
LOG_TYPE_SUB_MOD_ACCEPT = 25
LOG_TYPE_SUB_MOD_REMOVE = 26
LOG_TYPE_SUB_MOD_INV_CANCEL = 27
LOG_TYPE_SUB_MOD_INV_REJECT = 28
LOG_TYPE_SUB_STICKY_ADD = 50
LOG_TYPE_SUB_STICKY_DEL = 51
LOG_TYPE_SUB_DELETE_POST = 52
LOG_TYPE_SUB_DELETE_COMMENT = 53

LOG_TYPE_SUB_TRANSFER = 30

LOG_TYPE_SUB_CREATION = 40
LOG_TYPE_ANNOUNCEMENT = 41
LOG_TYPE_DOMAIN_BAN = 42
LOG_TYPE_DOMAIN_UNBAN = 43
LOG_TYPE_UNANNOUNCE = 44
LOG_TYPE_DISABLE_POSTING = 45
LOG_TYPE_ENABLE_POSTING = 46
LOG_TYPE_ENABLE_INVITE = 47
LOG_TYPE_DISABLE_INVITE = 48


def create_sitelog(action, uid, comment='', link=''):
    SiteLog.create(action=action, uid=uid, desc=comment, link=link).save()


# Note: `admin` makes the entry appear on the sitelog. I should rename it
def create_sublog(action, uid, sid, comment='', link='', admin=False, target=None):
    SubLog.create(action=action, uid=uid, sid=sid, desc=comment, link=link, admin=admin, target=target).save()


def is_domain_banned(link):
    bans = SiteMetadata.select().where(SiteMetadata.key == 'banned_domain')
    banned_domains, banned_domains_b = ([], [])
    for ban in bans:
        banned_domains.append(ban.value)
        banned_domains_b.append('.' + ban.value)

    url = urlparse(link)
    if (url.netloc in banned_domains) or (url.netloc.endswith(tuple(banned_domains_b))):
        return True
    return False


def create_captcha():
    """ Generates a captcha image.
    Returns a tuple with a token and the base64 encoded image """
    token = str(uuid.uuid4())
    captchagen = ImageCaptcha(width=250, height=70)
    if random.randint(1, 50) == 1:
        captcha = random.choice(['help me', 'sorry', 'hello', 'see me', 'observe', 'stop', 'nooooo', 'i can see', 'free me', 'behind you',
                                 'murder', 'shhhh', 'reeeee', 'come here', 'people die', 'it hurts', 'go away', 'touch me', 'last words',
                                 'closer', 'rethink', 'it is dark', 'it is cold', 'i am dying', 'quit staring', 'lock door'])
    else:
        captcha = ''.join(random.choice('abcdefghijklmnopqrstuvwxyz0123456789') for _ in range(random.randint(4, 6)))
    
    data = captchagen.generate(captcha.upper())
    b64captcha = base64.b64encode(data.getvalue()).decode()
    captcha = captcha.replace(' ', '').replace('0', 'o')

    rconn.setex('cap-' + token, value=captcha, time=300)  # captcha valid for 5 minutes.

    return (token, b64captcha)


def validate_captcha(token, response):
    cap = rconn.get('cap-' + token)
    if cap:
        response = response.replace(' ', '').replace('0', 'o')
        rconn.delete('cap-' + token)
        if cap.decode().lower() == response.lower():
            return True
    return False


def get_all_subs():
    """ Temporary function until we work out a better autocomplete
    for createpost """
    # TODO
    return [x.name for x in Sub.select(Sub.name)]


def get_comment_tree(comments, root=None, only_after=None, uid=None, provide_context=True):
    """ Returns a fully paginated and expanded comment tree.

    Parameters:
        comments: bare list of comments (only cid and parentcid)
        root: if present, the root comment to start building the tree on
        only_after: removes all siblings of `root` after the cid on its value

    TODO: Move to misc and implement globally
    """
    def build_tree(tuff, root=None):
        """ Builds a comment tree """
        res = []
        for i in tuff[::]:
            if i['parentcid'] == root:
                tuff.remove(i)
                i['children'] = build_tree(tuff, root=i['cid'])
                res.append(i)
        return res

    # 2 - Build bare comment tree
    comment_tree = build_tree(list(comments))

    # 2.1 - get only a branch of the tree if necessary
    if root:
        def select_branch(comments, root):
            """ Finds a branch with a certain root and returns a new tree """
            for i in comments:
                if i['cid'] == root:
                    return i
                k = select_branch(i['children'], root)
                if k:
                    return k
        comment_tree = select_branch(comment_tree, root)
        if comment_tree:
            # include the parent of the root for context.
            if comment_tree['parentcid'] is None or not provide_context:
                comment_tree = [comment_tree]
            else:
                orig_root = [x for x in list(comments) if x['cid'] == comment_tree['parentcid']]
                orig_root[0]['children'] = [comment_tree]
                comment_tree = orig_root
        else:
            return []
    # 3 - Trim tree (remove all children of depth=3 comments, all siblings after #5
    cid_list = []
    trimmed = False
    def recursive_check(tree, depth=0, trimmed=None, pcid=''):
        """ Recursively checks tree to apply pagination limits """
        or_len = len(tree)
        if only_after and not trimmed:
            imf = list(filter(lambda i: i['cid'] == only_after, tree))
            if imf:
                try:
                    tree = tree[tree.index(imf[0]) + 1:]
                except IndexError:
                    return []
                or_len = len(tree)
                trimmed = True
        if depth > 3:
            return [{'cid': None, 'more': len(tree), 'pcid': pcid}] if tree else []
        if (len(tree) > 5 and depth > 0) or (len(tree) > 10):
            tree = tree[:6] if depth > 0 else tree[:11]
            if or_len - len(tree) > 0:
                tree.append({'cid': None, 'key': tree[-1]['cid'], 'more': or_len - len(tree), 'pcid': pcid})

        for i in tree:
            if not i['cid']:
                continue
            cid_list.append(i['cid'])
            i['children'] = recursive_check(i['children'], depth+1, pcid=i['cid'])

        return tree

    comment_tree = recursive_check(comment_tree, trimmed=trimmed)

    # 4 - Populate the tree (get all the data and cram it into the tree)
    expcomms = SubPostComment.select(SubPostComment.cid, SubPostComment.content, SubPostComment.lastedit,
                                     SubPostComment.score, SubPostComment.status, SubPostComment.time, SubPostComment.pid,
                                     User.name.alias('user'), *([SubPostCommentVote.positive, SubPostComment.uid] if uid else [SubPostComment.uid]), # silly hack
                                     User.status.alias('userstatus'), SubPostComment.upvotes, SubPostComment.downvotes)
    expcomms = expcomms.join(User, on=(User.uid == SubPostComment.uid)).switch(SubPostComment)
    if uid:
        expcomms = expcomms.join(SubPostCommentVote, JOIN.LEFT_OUTER, on=((SubPostCommentVote.uid == uid) & (SubPostCommentVote.cid == SubPostComment.cid)))
    expcomms = expcomms.where(SubPostComment.cid << cid_list).dicts()

    commdata = {}
    for comm in expcomms:
        if comm['userstatus'] == 10 or comm['status']:
            comm['user'] = '[Deleted]'
            comm['uid'] = None

        if comm['status']:
            comm['content'] = ''
            comm['lastedit'] = None
        #del comm['userstatus']
        commdata[comm['cid']] = comm


    def recursive_populate(tree):
        """ Expands the tree with the data from `commdata` """
        populated_tree = []
        for i in tree:
            if not i['cid']:
                populated_tree.append(i)
                continue
            comment = commdata[i['cid']]
            comment['source'] = comment['content']
            comment['content'] = our_markdown(comment['content'])
            comment['children'] = recursive_populate(i['children'])
            populated_tree.append(comment)
        return populated_tree

    comment_tree = recursive_populate(comment_tree)
    return comment_tree


# Message type
MESSAGE_TYPE_PM = [1]
MESSAGE_TYPE_MENTION = [8]
MESSAGE_TYPE_MODMAIL = [2, 7, 11]
MESSAGE_TYPE_POSTREPLY = [4]
MESSAGE_TYPE_COMMREPLY = [5]

def get_messages(mtype, read=False, uid=None):
    """ Returns query for messages. If `read` is True it only queries for unread messages """
    query = Message.select().where(Message.mtype << mtype)
    query = query.where(Message.receivedby == current_user.uid if not uid else uid)
    if read:
        query = query.where(Message.read.is_null(True))
    return query


@cache.memoize(1)
def get_unread_count(mtype):
    return get_messages(mtype, True).count()


def cast_vote(uid, target_type, pcid, value):
    """ Casts a vote in a post.
      `uid` is the id of the user casting the vote
      `target_type` is either `post` or `comment`
      `pcid` is either the pid or cid of the post/comment
      `value` is either `up` or `down`
      """
    # XXX: This function returns api3 objects
    try:
        user = User.get(User.uid == uid)
    except User.DoesNotExist:
        return jsonify(msg="Unknown error. User disappeared"), 403

    if value == "up":
        voteValue = 1
    elif value == "down":
        voteValue = -1
        if user.given < 0:
            return jsonify(msg='Score balance is negative'), 403
    else:
        return jsonify(msg="Invalid vote value"), 400

    if target_type == "post":
        target_model = SubPost
        try:
            target = SubPost.select(SubPost.uid, SubPost.score, SubPost.upvotes, SubPost.downvotes, SubPost.pid.alias('id'), SubPost.posted)
            target = target.where(SubPost.pid == pcid).get()
        except SubPost.DoesNotExist:
            return jsonify(msg='Post does not exist'), 404

        if target.deleted:
            return jsonify(msg="You can't vote on deleted posts"), 400

        try:
            qvote = SubPostVote.select().where(SubPostVote.pid == pcid).where(SubPostVote.uid == uid).get()
        except SubPostVote.DoesNotExist:
            qvote = False
    elif target_type == "comment":
        target_model = SubPostComment
        try:
            target = SubPostComment.select(SubPostComment.uid, SubPost.sid, SubPostComment.pid, SubPostComment.status, SubPostComment.score,
                                           SubPostComment.upvotes, SubPostComment.downvotes, SubPostComment.cid.alias('id'), SubPostComment.time.alias('posted'))
            target = target.join(SubPost).where(SubPostComment.cid == pcid).objects().get()
        except SubPostComment.DoesNotExist:
            return jsonify(msg='Comment does not exist'), 404
        
        if target.uid_id == user.uid:
            return jsonify(msg="You can't vote on your own comments"), 400
        if target.status:
            return jsonify(msg="You can't vote on deleted comments"), 400

        try:
            qvote = SubPostCommentVote.select().where(SubPostCommentVote.cid == pcid).where(SubPostCommentVote.uid == uid).get()
        except SubPostCommentVote.DoesNotExist:
            qvote = False
    else:
        return jsonify(msg="Invalid target"), 400

    try:
        SubMetadata.get((SubMetadata.sid == target.sid) & (SubMetadata.key == "ban") & (SubMetadata.value == user.uid))
        return jsonify(msg='You are banned on this sub.'), 403
    except SubMetadata.DoesNotExist:
        pass

    if (datetime.utcnow() - target.posted) > timedelta(days=60):
        return jsonify(msg="Post is archived"), 400

    user = User.get(User.uid == target.uid)

    positive = True if voteValue == 1 else False
    undone = False

    if qvote:
        if bool(qvote.positive) == (True if voteValue == 1 else False):
            qvote.delete_instance()

            if positive:
                upd_q = target_model.update(score=target_model.score - voteValue, upvotes=target_model.upvotes - 1)
            else:
                upd_q = target_model.update(score=target_model.score - voteValue, downvotes=target_model.downvotes - 1)
            new_score = -voteValue
            undone = True
            User.update(score=User.score - voteValue).where(User.uid == target.uid).execute()
            User.update(given=User.given - voteValue).where(User.uid == uid).execute()
        else:
            qvote.positive = positive
            qvote.save()

            if positive:
                upd_q = target_model.update(score=target_model.score + (voteValue * 2), upvotes=target_model.upvotes + 1, downvotes=target_model.downvotes - 1)
            else:
                upd_q = target_model.update(score=target_model.score + (voteValue * 2), upvotes=target_model.upvotes - 1, downvotes=target_model.downvotes + 1)
            new_score = (voteValue * 2)
            User.update(score=User.score + (voteValue * 2)).where(User.uid == target.uid).execute()
            User.update(given=User.given + voteValue).where(User.uid == uid).execute()
    else: # First vote cast on post
        now = datetime.utcnow()
        if target_type == "post":
            sp_vote = SubPostVote.create(pid=pcid, uid=uid, positive=positive, datetime=now)
        else:
            sp_vote = SubPostCommentVote.create(cid=pcid, uid=uid, positive=positive, datetime=now)

        sp_vote.save()

        if positive:
            upd_q = target_model.update(score=target_model.score + voteValue, upvotes=target_model.upvotes + 1)
        else:
            upd_q = target_model.update(score=target_model.score + voteValue, downvotes=target_model.downvotes + 1)
        new_score = voteValue
        User.update(score=User.score + voteValue).where(User.uid == target.uid).execute()
        User.update(given=User.given + voteValue).where(User.uid == uid).execute()

    if target_type == "post":
        upd_q.where(SubPost.pid == target.id).execute()
        socketio.emit('threadscore', {'pid': target.id, 'score': target.score + new_score},
                      namespace='/snt', room=target.id)

        socketio.emit('yourvote', {'pid': target.id, 'status': voteValue if not undone else 0, 'score': target.score + new_score}, namespace='/snt',
                      room='user' + uid)
    else:
        upd_q.where(SubPostComment.cid == target.id).execute()

    socketio.emit('uscore', {'score': target.uid.score + new_score},
                  namespace='/snt', room="user" + target.uid_id)

    return jsonify(score=target.score + new_score, rm=undone)

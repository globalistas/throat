""" Misc helper function and classes. """
from urllib.parse import urlparse, parse_qs, urljoin
import json
import math
import base64
import uuid
import random
import time
import os
import re
import gevent
import ipaddress
from collections import defaultdict

from bs4 import BeautifulSoup
import tinycss2
from captcha.image import ImageCaptcha
from datetime import datetime, timedelta, timezone
import misaka as m
import sendgrid
from sendgrid.helpers.mail import Mail
from flask import current_app, _request_ctx_stack, has_request_context, has_app_context, g
from flask import url_for, request, jsonify, session
from flask_limiter import Limiter
from flask_mail import Mail
from flask_mail import Message as EmailMessage
from slugify import slugify as s_slugify

from .config import config
from flask_login import AnonymousUserMixin, current_user
from flask_babel import Babel, _
from flask_talisman import Talisman
from .caching import cache
from .socketio import socketio
from .badges import badges

from .models import Sub, SubPost, User, SiteMetadata, SubSubscriber, Message, UserMetadata, SubRule
from .models import SubPostVote, SubPostComment, SubPostCommentVote, SiteLog, SubLog, db
from .models import SubPostReport, SubPostCommentReport, PostReportLog, CommentReportLog, Notification
from .models import SubMetadata, rconn, SubStylesheet, UserIgnores, SubUploads, SubFlair, InviteCode
from .models import SubMod, SubBan, SubPostCommentHistory, SubPostMetadata

from .storage import file_url, thumbnail_url
from peewee import JOIN, fn, SQL, NodeList, Value
import logging
import logging.config
from werkzeug.local import LocalProxy

from wheezy.template.engine import Engine
from wheezy.template.ext.core import CoreExtension
from wheezy.template.loader import FileLoader, autoreload

# Regex that matches VALID user and sub names
allowedNames = re.compile("^[a-zA-ZÀ-ž0-9_-]+$")
WHITESPACE = "\u0009\u000A\u000B\u000C\u000D\u0020\u0085\u00A0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007" \
             "\u2008\u2009\u200a\u200b\u2029\u202f\u205f\u3000\u180e\u200b\u200c\u200d\u2060\ufeff\u00AD\ufffc "


def build_var(builder, lineno, token, value):
    assert token == 'var'
    var, _ = value
    if not value[-1] or "html" not in value[-1]:
        builder.add(lineno, 'w(e(str(' + var + ')))')
    else:
        builder.add(lineno, 'w(str(' + var + '))')
    return True


class EscapeExtension(object):
    builder_rules = [
        ('var', build_var)
    ]


engine = Engine(
    loader=FileLoader([os.path.split(__file__)[0] + '/html']),
    extensions=[EscapeExtension(), CoreExtension()]
)

mail = Mail()

babel = Babel()

talisman = Talisman()


class SiteUser(object):
    """ Representation of a site user. Used on the login manager. """

    def __init__(self, userclass=None, subs=(), prefs=()):
        self.user = userclass
        self.notifications = self.user.get('notifications', 0)
        self.open_reports = self.user.get('open_reports', 0)
        self.name = self.user['name']
        self.uid = self.user['uid']
        self.prefs = [x['key'] for x in prefs]

        self.subtheme = [x['value'] for x in prefs if x['key'] == 'subtheme']
        self.subtheme = self.subtheme[0] if self.subtheme else ''

        self.language = self.user['language']
        self.resets = self.user['resets']

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
        # True if the user is an admin, even without authing with TOTP
        self.can_admin = 'admin' in self.prefs

        try:
            SubMod.select().where(SubMod.user == self.uid).get()
            self.is_a_mod = True
        except SubMod.DoesNotExist:
            self.is_a_mod = False

        if (time.time() - session.get('apriv', 0) < 7200) or not config.site.enable_totp:
            self.admin = 'admin' in self.prefs
        else:
            self.admin = False

        self.canupload = True if ('canupload' in self.prefs) or self.admin else False
        if config.site.allow_uploads and config.site.upload_min_level == 0:
            self.canupload = True
        elif config.site.allow_uploads and (config.site.upload_min_level <= get_user_level(self.uid, self.score)[0]):
            self.canupload = True

    def can_pm_users(self):
        return config.site.send_pm_to_user_min_level <= get_user_level(self.uid, self.score)[0] or self.admin

    def __repr__(self):
        return "<SiteUser {0}>".format(self.uid)

    def get_id(self):
        """ Returns the unique user id. Used on load_user """
        return self.uid if self.resets == 0 else f'{self.uid}${self.resets}'

    @cache.memoize(1)
    def is_mod(self, sid, power_level=2):
        """ Returns True if the current user is a mod of 'sub' """
        return is_sub_mod(self.uid, sid, power_level, self.can_admin)

    @cache.memoize(1)
    def mod_notifications(self):
        if self.is_mod:
            post_report_counts = (
                SubPostReport.select(Sub.sid, fn.COUNT(SubPostReport.id).alias('count'))
                             .join(SubPost).join(Sub).join(SubMod)
                             .where((SubMod.user == self.uid) & SubPostReport.open)
                             .group_by(Sub.sid).dicts())
            comment_report_counts = (
                SubPostCommentReport.select(Sub.sid, fn.COUNT(SubPostCommentReport.id).alias('count')).join(
                    SubPostComment).join(SubPost).join(Sub).join(SubMod).where(
                    (SubMod.user == self.uid) & SubPostCommentReport.open).group_by(Sub.sid).dicts()
            )

            counts = defaultdict(int)
            for item in post_report_counts:
                counts[item['sid']] = item['count']
            for item in comment_report_counts:
                counts[item['sid']] += item['count']

            return [[sid, count] for sid, count in counts.items()]
        else:
            return []

    def mod_notifications_json(self):
        return json.dumps(self.mod_notifications())

    def is_subban(self, sub):
        """ Returns True if the current user is banned from 'sub' """
        return is_sub_banned(sub, self.user)

    def is_modinv(self, sub):
        """ Returns True if the current user is invited to mod of 'sub' """
        try:
            SubMod.get((SubMod.sid == sub) & (SubMod.uid == self.uid) & (SubMod.invite == True))
            return True
        except SubMod.DoesNotExist:
            return False

    def is_admin(self):
        """ Returns true if the current user is a site admin. """
        return self.admin

    def has_subscribed(self, name):
        """ Returns True if the current user has subscribed to sub """
        if len(name) == 36:  # TODO: BAD NASTY HACK REMOVE THIS.
            return name in self.subsid
        else:
            return name in self.subscriptions

    def has_blocked(self, sid):
        """ Returns True if the current user has blocked sub """
        return sid in self.blocksid

    def likes_scroll(self):
        """ Returns true if user likes scroll """
        return 'noscroll' in self.prefs

    def block_styles(self):
        """ Returns true if user selects to block sub styles """
        return 'nostyles' in self.prefs

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
            umd = UserMetadata.get((UserMetadata.uid == self.uid) & (UserMetadata.key == key))
            umd.value = value
            umd.save()
        except UserMetadata.DoesNotExist:
            UserMetadata.create(uid=self.uid, key=key, value=value)

    @cache.memoize(30)
    def get_global_stylesheet(self):
        if self.subtheme:
            try:
                css = SubStylesheet.select().join(Sub).where(fn.Lower(Sub.name) == self.subtheme.lower()).get()
            except SubStylesheet.DoesNotExist:
                return ''
            return css.content
        return ''


def is_target_user_admin(uid):
    try:
        UserMetadata.get((UserMetadata.uid == uid) & (UserMetadata.key == 'admin') & (UserMetadata.value == '1'))
        return True
    except UserMetadata.DoesNotExist:
        return False


class SiteAnon(AnonymousUserMixin):
    """ A subclass of AnonymousUserMixin. Used for logged out users. """
    uid = False
    subsid = []
    subscriptions = []
    blocksid = []
    prefs = []
    admin = False
    canupload = False
    language = None
    score = 0
    is_a_mod = False
    can_admin = False

    def get_id(self):
        return False

    @classmethod
    def is_mod(cls, _sub, _power_level):
        return False

    @classmethod
    def is_admin(cls):
        """ Anons are not admins. """
        return False

    @classmethod
    def can_pm_users(cls):
        """ Anons may never PM users. """
        return False

    @classmethod
    def likes_scroll(cls):
        """ Anons like scroll. """
        return False

    @classmethod
    def get_top_bar(cls):
        return getDefaultSubs_list(True)

    @classmethod
    def has_subscribed(cls, _sub):
        """ Anons dont get subscribe options. """
        return False

    @classmethod
    def has_blocked(cls, _sub):
        """ Anons dont get blocked options. """
        return False

    @classmethod
    def block_styles(cls):
        """ Anons dont get usermetadata options. """
        return False

    @classmethod
    def is_modinv(cls):
        """ Anons dont get see submod page. """
        return False

    @classmethod
    def is_subban(cls, _sub):
        """ Anons dont get banned by default. """
        return False

    @classmethod
    def get_user_level(cls):
        return 0, 0

    @classmethod
    def get_global_stylesheet(cls):
        return ''


def get_ip():
    """ Return the user's IP address for rate-limiting. """
    addr = ipaddress.ip_address(request.remote_addr or '127.0.0.1')
    if isinstance(addr, ipaddress.IPv6Address):
        return addr.exploded[:19]  # use the /64
    else:
        return str(addr)


limiter = Limiter(key_func=get_ip)
ratelimit = limiter.limit
POSTING_LIMIT = '6/minute;120/hour'
AUTH_LIMIT = '25/5minute'
SIGNUP_LIMIT = '5/30minute'


class MentionRegex:
    def __init__(self, app=None):
        if app is not None:
            self.init_app(app)

    # noinspection PyAttributeOutsideInit
    def init_app(self, app):
        prefix = app.config['THROAT_CONFIG'].site.sub_prefix
        BARE = r'(?<=^|(?<=[^a-zA-Z0-9-_\.\/]))((@|\/u\/|\/' + prefix + r'\/)([A-Za-z0-9\-\_]+))'
        PRE0 = r'(?:(?:\[.+?\]\(.+?\))|(?<=^|(?<=[^a-zA-Z0-9-_\.\/]))(?:(?:@|\/u\/|\/' + prefix + r'\/)(?:[A-Za-z0-9\-\_]+)))'
        PRE1 = r'(?:(\[.+?\]\(.+?\))|' + BARE + r')'
        self.ESCAPED = re.compile(r"```.*{0}.*```|`.*?{0}.*?`|({1})".format(PRE0, PRE1),
                                  flags=re.MULTILINE + re.DOTALL)
        self.LINKS = re.compile(r"\[.*?({1}).*?\]\(.*?\)|({0})".format(PRE1, BARE),
                                flags=re.MULTILINE + re.DOTALL)


re_amention = MentionRegex()


class PhuksDown(m.SaferHtmlRenderer):
    _allowed_url_re = re.compile(r'^(https?:|/|#)', re.I)

    def image(self, raw_url, title='', alt=''):
        return False

    def check_url(self, url, is_image_src=False):
        return bool(self._allowed_url_re.match(url))

    def autolink(self, raw_url, is_email):
        if self.check_url(raw_url):
            url = self.rewrite_url(('mailto:' if is_email else '') + raw_url)
            url = m.escape_html(url)
            return '<a href="%s" rel="noopener nofollow ugc">%s</a>' % (url, m.escape_html(raw_url))
        else:
            return m.escape_html('<%s>' % raw_url)

    def link(self, content, raw_url, title=''):
        if raw_url == '#spoiler':
            return f"<spoiler>{content}</spoiler>"
        if self.check_url(raw_url):
            url = self.rewrite_url(raw_url)
            maybe_title = ' title="%s"' % m.escape_html(title) if title else ''
            url = m.escape_html(url)
            return ('<a rel="noopener nofollow ugc" href="%s"%s>' % (url, maybe_title)) + content + '</a>'
        else:
            return m.escape_html("[%s](%s)" % (content, raw_url))


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
        txt = match.group(3)
        txt = txt.replace('_', '\\_')
        txt = txt.replace('*', '\\*')
        txt = txt.replace('~', '\\~')
        return '[{0}]({1})'.format(txt, ln)

    text = re_amention.ESCAPED.sub(repl, text)

    def repl_spoiler(match):
        if match.group(1) is None:
            return match.group(0)

        return f'[{match.group(1)}](#spoiler)'

    # Spoiler tags. Matches ">!foobar!<" unless it's in a code block
    text = re.sub(r'>!(.+)!<|`.*?>!(?:.+?)!<.*?`|```.*?>!(?:.+?)!<.*?```', repl_spoiler, text)

    try:
        html = md(text)
    except RecursionError:
        return '> tfw tried to break the site'

    return html


@cache.memoize(5)
def is_sub_banned(sub, user=None, uid=None):
    """ Returns True if 'user' is banned 'sub' """
    if isinstance(sub, dict):
        sid = sub['sid']
    elif isinstance(sub, str) or isinstance(sub, int):
        sid = sub
    else:
        sid = sub.sid
    if not uid:
        uid = user['uid']
    try:
        SubBan.get((SubBan.sid == sid) &
                   (SubBan.uid == uid) &
                   ((SubBan.effective == True) & (
                           (SubBan.expires.is_null(True)) | (SubBan.expires > datetime.utcnow()))))
        return True
    except SubBan.DoesNotExist:
        return False


@cache.memoize(5)
def getSubFlairs(sid):
    return list(SubFlair.select().where(SubFlair.sid == sid))


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


@cache.memoize(600)
def enableInviteCode():
    """ Returns true if invite code is required to register """
    try:
        xm = SiteMetadata.get(SiteMetadata.key == 'useinvitecode')
        return False if xm.value == '0' else True
    except SiteMetadata.DoesNotExist:
        return False


@cache.memoize(30)
def getMaxCodes(uid):
    """ Returns how many invite codes a user can create """
    try:
        amt = UserMetadata.get((UserMetadata.key == 'invite_max') & (UserMetadata.uid == uid))
        return amt.value
    except UserMetadata.DoesNotExist:
        try:
            # If there's no setting for the user, use the global setting, but checkk the user's level first
            minlevel = SiteMetadata.get(SiteMetadata.key == 'invite_level')
            if get_user_level(uid)[0] >= int(minlevel.value):
                amt = SiteMetadata.get(SiteMetadata.key == 'invite_max')
                return amt.value
        except SiteMetadata.DoesNotExist:
            return 0
    return 0


@cache.memoize(30)
def getInviteCodeInfo(uid):
    """
    Returns information about who invited a user and who they have invited.
    """
    info = {}

    # The invite code that this user used to sign up
    try:
        invite_code = UserMetadata.get((UserMetadata.key == 'invitecode') & (UserMetadata.uid == uid))
        code = invite_code.value
        invited_by_uid = InviteCode.get((InviteCode.code == code)).uid
        invited_by_name = User.get((User.uid == invited_by_uid)).name
        info['invitedBy'] = {'name': invited_by_name, 'code': code}
    except UserMetadata.DoesNotExist:
        pass

    # Codes that this user has generated, that other users signed up with
    try:
        user_codes = InviteCode.select(User.name, InviteCode.code) \
            .where(InviteCode.user == uid) \
            .join(UserMetadata, JOIN.LEFT_OUTER,
                  on=((UserMetadata.value == InviteCode.code) & (UserMetadata.key == 'invitecode'))) \
            .join(User).dicts()
        info['invitedTo'] = list(user_codes)
    except InviteCode.DoesNotExist:
        pass

    return info


def send_email(to, subject, text_content, html_content, sender=None):
    if 'server' in config.mail:
        if sender is None:
            sender = config.mail.default_from
        send_email_with_smtp(sender, to, subject, text_content, html_content)
    elif 'sendgrid' in config:
        if sender is None:
            sender = config.sendgrid.default_from
        send_email_with_sendgrid(sender, to, subject, html_content)
    else:
        raise RuntimeError('Email not configured')


def send_email_with_smtp(sender, recipients, subject, text_content, html_content):
    if not isinstance(recipients, list):
        recipients = [recipients]
    msg = EmailMessage(subject, sender=sender, recipients=recipients,
                       body=text_content, html=html_content)
    if config.app.testing:
        send_smtp_email_async(current_app, msg)
    else:
        gevent.spawn(send_smtp_email_async, current_app._get_current_object(), msg)


def send_smtp_email_async(app, msg):
    with app.app_context():
        mail.send(msg)


def send_email_with_sendgrid(sender, to, subject, html_content):
    """ Send a mail through sendgrid """
    sg = sendgrid.SendGridAPIClient(api_key=config.sendgrid.api_key)

    mail = sendgrid.helpers.mail.Mail(
        from_email=sender,
        to_emails=to,
        subject=subject,
        html_content=html_content)

    sg.send(mail)


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


def workWithMentions(data, receivedby, post, _sub, cid=None, c_user=current_user):
    """ Does all the job for mentions """
    mts = re.findall(re_amention.LINKS, data)
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
                user = User.get(fn.Lower(User.name) == mtn.lower())
            except User.DoesNotExist:
                continue
            if user.uid != c_user.uid and user.uid != receivedby:
                # Checks done. Send our shit
                if cid:
                    Notification.create(type='COMMENT_MENTION', sub=post.sid, post=post.pid, comment=cid,
                                        sender=c_user.uid, target=user.uid)
                else:
                    Notification.create(type='POST_MENTION', sub=post.sid, post=post.pid, comment=cid,
                                        sender=c_user.uid, target=user.uid)
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
    suffix = ('.png', '.jpg', '.gif', '.tiff', '.bmp', '.jpeg', '.svg')
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
    userbadges = badges.badges_for_user(uid)
    for badge in userbadges:
        xp += badge.score
    if xp <= 0:  # We don't want to do the sqrt of a negative number
        return 0, xp
    level = math.sqrt(xp / 10)
    return int(level), xp


@cache.memoize(300)
def getTodaysTopPosts():
    """ Returns top posts in the last 24 hours """
    td = datetime.utcnow() - timedelta(days=1)
    posts = (SubPost.select(SubPost.pid, Sub.name.alias('sub'), SubPost.title, SubPost.posted, SubPost.score)
             .where(SubPost.posted > td).where(SubPost.deleted == 0).order_by(SubPost.score.desc()).limit(5)
             .join(Sub, JOIN.LEFT_OUTER).dicts())
    top_posts = []
    for p in posts:
        top_posts.append(p)
    return top_posts


def set_sub_of_the_day(sid):
    today = datetime.utcnow()
    tomorrow = datetime(year=today.year, month=today.month, day=today.day) + timedelta(seconds=86400)
    timeuntiltomorrow = tomorrow - today
    if timeuntiltomorrow.total_seconds() < 5:
        timeuntiltomorrow = 86400
    rconn.setex('daysub', value=sid, time=timeuntiltomorrow)


@cache.memoize(10)
def getSubOfTheDay():
    daysub = rconn.get('daysub')
    if not daysub:
        try:
            daysub = Sub.select(Sub.sid, Sub.name, Sub.title).order_by(db.random()).get()
        except Sub.DoesNotExist:  # No subs
            return False
        set_sub_of_the_day(daysub.sid)
    else:
        try:
            daysub = Sub.select(Sub.name, Sub.title).where(Sub.sid == daysub).get()
        except Sub.DoesNotExist:  # ???
            return False
    return daysub


def getChangelog():
    """ Returns most recent changelog post """
    if not config.site.changelog_sub:
        return None
    td = datetime.utcnow() - timedelta(days=15)
    changepost = (SubPost.select(Sub.name.alias('sub'), SubPost.pid, SubPost.title, SubPost.posted)
                  .where(SubPost.posted > td).where(SubPost.sid == config.site.changelog_sub)
                  .join(Sub, JOIN.LEFT_OUTER).order_by(SubPost.pid.desc()).dicts())

    try:
        return changepost.get()
    except SubPost.DoesNotExist:
        return None


def getSinglePost(pid):
    if current_user.is_authenticated:
        posts = SubPost.select(SubPost.nsfw, SubPost.sid, SubPost.content, SubPost.pid, SubPost.title, SubPost.posted,
                               SubPost.score, SubPost.upvotes, SubPost.downvotes, SubPost.distinguish,
                               SubPost.thumbnail, SubPost.link, User.name.alias('user'), Sub.name.alias('sub'),
                               SubPost.flair, SubPost.edited,
                               SubPost.comments, SubPostVote.positive, User.uid, User.status.alias('userstatus'),
                               SubPost.deleted, SubPost.ptype)
        posts = posts.join(SubPostVote, JOIN.LEFT_OUTER,
                           on=((SubPostVote.pid == SubPost.pid) & (SubPostVote.uid == current_user.uid))).switch(
            SubPost)
    else:
        posts = SubPost.select(SubPost.nsfw, SubPost.sid, SubPost.content, SubPost.pid, SubPost.title, SubPost.posted,
                               SubPost.score, SubPost.upvotes, SubPost.downvotes, SubPost.distinguish,
                               SubPost.thumbnail, SubPost.link, User.name.alias('user'), Sub.name.alias('sub'),
                               SubPost.flair, SubPost.edited,
                               SubPost.comments, User.uid, User.status.alias('userstatus'), SubPost.deleted,
                               SubPost.ptype)
    posts = posts.join(User, JOIN.LEFT_OUTER).switch(SubPost).join(Sub, JOIN.LEFT_OUTER).where(
        SubPost.pid == pid).dicts().get()
    posts['slug'] = slugify(posts['title'])
    return posts


def postListQueryBase(*extra, nofilter=False, noAllFilter=False, noDetail=False, adminDetail=False, isSubMod=False):
    reports = SubPostReport.select(SubPostReport.pid, fn.Min(SubPostReport.id).alias("open_report_id"),
                                   fn.Count(SubPostReport.id).alias('open_reports')).where(
        SubPostReport.open == True).group_by(SubPostReport.pid).cte("reports")

    if current_user.is_authenticated and not noDetail:
        posts = SubPost.select(SubPost.nsfw, SubPost.content, SubPost.pid, SubPost.title, SubPost.posted,
                               SubPost.deleted, SubPost.score, SubPost.ptype, SubPost.distinguish,
                               SubPost.thumbnail, SubPost.link, User.name.alias('user'), Sub.name.alias('sub'),
                               SubPost.flair, SubPost.edited, Sub.sid,
                               SubPost.comments, SubPostVote.positive, User.uid, User.status.alias('userstatus'),
                               *extra, *([reports.c.open_report_id, reports.c.open_reports] if isSubMod else [
                                          Value(None).alias('open_report_id'), Value(None).alias('open_reports')]))
        posts = posts.join(SubPostVote, JOIN.LEFT_OUTER,
                           on=((SubPostVote.pid == SubPost.pid) & (SubPostVote.uid == current_user.uid))).switch(
            SubPost)
        if isSubMod:
            posts = posts.join(reports, JOIN.LEFT_OUTER, on=(reports.c.pid == SubPost.pid)).switch(SubPost).with_cte(
                reports)
    else:
        posts = SubPost.select(SubPost.nsfw, SubPost.content, SubPost.pid, SubPost.title, SubPost.posted,
                               SubPost.deleted, SubPost.score, SubPost.ptype, SubPost.distinguish,
                               SubPost.thumbnail, SubPost.link, User.name.alias('user'), Sub.name.alias('sub'),
                               SubPost.flair, SubPost.edited, Sub.sid,
                               SubPost.comments, User.uid, User.status.alias('userstatus'), *extra,
                               Value(None).alias('open_report_id'), Value(None).alias('open_reports'))
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
        return postListQueryBase(noDetail=noDetail, nofilter=nofilter, isSubMod=current_user.can_admin).where(
            SubPost.sid << current_user.subsid)
    else:
        return postListQueryBase(noDetail=noDetail, nofilter=nofilter).join(SiteMetadata, JOIN.LEFT_OUTER,
                                                                            on=(SiteMetadata.key == 'default')).where(
            SubPost.sid == SiteMetadata.value)


def getPostList(baseQuery, sort, page):
    if sort == "top":
        posts = baseQuery.order_by(SubPost.score.desc()).paginate(page, 25)
    elif sort == "new":
        posts = baseQuery.order_by(SubPost.pid.desc()).paginate(page, 25)
    else:
        if 'Postgresql' in config.database.engine:
            posted = fn.EXTRACT(NodeList((SQL('EPOCH FROM'), SubPost.posted)))
        elif 'SqliteDatabase' in config.database.engine:
            posted = fn.datetime(SubPost.posted, 'unixepoch')
        else:
            posted = fn.Unix_Timestamp(SubPost.posted)

        if config.site.custom_hot_sort:
            hot = fn.HOT(SubPost.score, posted)
        else:
            hot = SubPost.score * 20 + (posted - 1134028003) / 1500
        posts = baseQuery.order_by(hot.desc()).limit(100).paginate(page, 25)
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


@cache.memoize(60)
def getStickyPid(sid):
    """ Returns a list of stickied SubPosts """
    x = SubMetadata.select(SubMetadata.value).where(SubMetadata.sid == sid).where(SubMetadata.key == 'sticky').dicts()
    return [int(y['value']) for y in x]


@cache.memoize(60)
def getStickies(sid):
    posts = postListQueryBase().join(SubMetadata, on=(SubPost.sid == SubMetadata.sid) & (
            SubPost.pid == SubMetadata.value.cast("int")) & (SubMetadata.key == 'sticky'))
    posts = posts.where(SubPost.sid == sid)
    posts = posts.order_by(SubMetadata.xid.asc()).dicts()
    return list(posts)


def load_user(user_id):
    mcount = Message.select(fn.Count(Message.mid)).where(
        (Message.receivedby == user_id) & (Message.mtype == 1) & Message.read.is_null(True))
    ncount = Notification.select(fn.Count(Notification.id)).where(
        (Notification.target == user_id) & Notification.read.is_null(True))
    user = User.select(mcount.alias('messages'), ncount.alias('notifications'),
                       User.given, User.score, User.name, User.uid, User.status,
                       User.email, User.language, User.resets)
    user = user.where(User.uid == user_id).dicts().get()
    user['notifications'] += user['messages']

    # This is the only user attribute needed by the error templates, so stash
    # it in the session so that future errors in this session won't have to
    # load the user to show them the correct language.
    session['language'] = user['language']

    if request.path == '/socket.io/':
        return SiteUser(user, [], [])
    else:
        prefs = UserMetadata.select(UserMetadata.key, UserMetadata.value).where(UserMetadata.uid == user_id)
        prefs = prefs.where((UserMetadata.value == '1') | (UserMetadata.key == 'subtheme')).dicts()

        try:
            subs = SubSubscriber.select(SubSubscriber.sid, Sub.name, SubSubscriber.status).join(Sub, on=(
                    Sub.sid == SubSubscriber.sid)).switch(SubSubscriber).where(SubSubscriber.uid == user_id)
            subs = subs.order_by(SubSubscriber.order.asc()).dicts()
            return SiteUser(user, subs, prefs)
        except User.DoesNotExist:
            return None


def user_is_loaded():
    return has_request_context() and hasattr(_request_ctx_stack.top, 'user')


def ensure_locale_loaded():
    if 'language' not in session or not session['language']:
        session['language'] = get_locale_fallback()


@babel.localeselector
def get_locale():
    language = session.get('language', 'sk')
    if language:
        return language
    if current_user.language:
        return current_user.language
    return get_locale_fallback()


def get_locale_fallback():
    return request.accept_languages.best_match(config.app.languages, config.app.fallback_language)


def get_notification_count(uid):
    msg = Message.select().where(
        (Message.receivedby == uid) & (Message.mtype == 1) & Message.read.is_null(True)).count()
    notif = Notification.select().where((Notification.target == uid) & Notification.read.is_null(True)).count()
    return msg + notif


def get_errors(form, first=False):
    """ A simple function that returns a list with all the form errors. """
    if request.method == 'GET':
        return []
    ret = []
    for field, errors in form.errors.items():
        for error in errors:
            ret.append(
                _(u"Error in the '%(field)s' field - %(error)s", field=getattr(form, field).label.text, error=error))
    if first:
        if len(ret) > 0:
            return ret[0]
        else:
            return ""
    return ret


# messages

def getMessagesIndex(page, uid=None):
    """ Returns messages inbox """
    if not uid:
        uid = current_user.uid
    try:
        msg = Message.select(Message.mid, User.name.alias('username'), Message.sentby, Message.receivedby,
                             Message.subject, Message.content, Message.posted, Message.read, Message.mtype,
                             Message.mlink)
        msg = msg.join(User, JOIN.LEFT_OUTER, on=(User.uid == Message.sentby)).where(Message.mtype == 1).where(
            Message.receivedby == uid).order_by(Message.mid.desc()).paginate(page, 20).dicts()
    except Message.DoesNotExist:
        return False
    return msg


def getMentionsIndex(page):
    """ Returns user mentions inbox """
    try:
        msg = Message.select(Message.mid, User.name.alias('username'), Message.sentby, Message.receivedby,
                             Message.subject, Message.content, Message.posted, Message.read, Message.mtype,
                             Message.mlink)
        msg = msg.join(User, JOIN.LEFT_OUTER, on=(User.uid == Message.sentby)).where(Message.mtype == 8).where(
            Message.receivedby == current_user.uid).order_by(Message.mid.desc()).paginate(page, 20).dicts()
    except Message.DoesNotExist:
        return False
    return msg


def getMessagesSent(page):
    """ Returns messages sent """
    try:
        msg = Message.select(Message.mid, Message.sentby, User.name.alias('username'), Message.subject, Message.content,
                             Message.posted, Message.read, Message.mtype, Message.mlink)
        msg = msg.join(User, JOIN.LEFT_OUTER, on=(User.uid == Message.receivedby)).where(
            Message.mtype << [1, 6, 9, 41]).where(
            Message.sentby == current_user.uid).order_by(Message.mid.desc()).paginate(page, 20).dicts()
    except Message.DoesNotExist:
        return False
    return msg


def getMessagesModmail(page):
    """ Returns modmail """
    try:
        msg = Message.select(Message.mid, User.name.alias('username'), Message.receivedby, Message.subject,
                             Message.content, Message.posted, Message.read, Message.mtype, Message.mlink)
        msg = msg.join(User, on=(User.uid == Message.sentby)).where(Message.mtype << [2, 7, 11]).where(
            Message.receivedby == current_user.uid).order_by(Message.mid.desc()).paginate(page, 20).dicts()
    except Message.DoesNotExist:
        return False
    return msg


def getMessagesSaved(page):
    """ Returns saved messages """
    try:
        msg = Message.select(Message.mid, User.name.alias('username'), Message.receivedby, Message.subject,
                             Message.content, Message.posted, Message.read, Message.mtype, Message.mlink)
        msg = msg.join(User, on=(User.uid == Message.sentby)).where(Message.mtype == 9).where(
            Message.receivedby == current_user.uid).order_by(Message.mid.desc()).paginate(page, 20).dicts()
    except Message.DoesNotExist:
        return False
    return msg


def getMsgCommReplies(page):
    """ Returns comment replies messages """
    try:
        msg = Message.select(Message.mid, User.name.alias('username'), Message.sentby, Message.receivedby,
                             Message.subject,
                             Message.posted, Message.read, Message.mtype, Message.mlink, SubPostComment.pid,
                             SubPostComment.content,
                             SubPostComment.score, SubPostCommentVote.positive, Sub.name.alias('sub'))
        msg = msg.join(SubPostComment, on=SubPostComment.cid == Message.mlink).join(SubPost).join(Sub).switch(
            SubPostComment).join(SubPostCommentVote, JOIN.LEFT_OUTER, on=(
                (SubPostCommentVote.uid == current_user.uid) & (SubPostCommentVote.cid == Message.mlink)))
        msg = msg.join(User, on=(User.uid == Message.sentby)).where(Message.mtype == 5).where(
            Message.receivedby == current_user.uid).order_by(Message.mid.desc()).paginate(page, 20).dicts()
    except Message.DoesNotExist:
        return False
    return msg


def getMsgPostReplies(page):
    """ Returns post replies messages """
    try:
        msg = Message.select(Message.mid, User.name.alias('username'), Message.sentby, Message.receivedby,
                             Message.subject,
                             Message.posted, Message.read, Message.mtype, Message.mlink, SubPostCommentVote.positive,
                             SubPostComment.pid,
                             SubPostComment.score, SubPostComment.content, Sub.name.alias('sub'))
        msg = msg.join(SubPostComment, on=SubPostComment.cid == Message.mlink).join(SubPost).join(Sub).switch(
            SubPostComment).join(SubPostCommentVote, JOIN.LEFT_OUTER, on=(
                (SubPostCommentVote.uid == current_user.uid) & (SubPostCommentVote.cid == Message.mlink)))
        msg = msg.join(User, on=(User.uid == Message.sentby)).where(Message.mtype == 4).where(
            Message.receivedby == current_user.uid).order_by(Message.mid.desc()).paginate(page, 20).dicts()
    except Message.DoesNotExist:
        return False
    return msg


# user comments


def getUserComments(uid, page):
    """ Returns comments for a user """
    try:
        com = SubPostComment.select(Sub.name.alias('sub'), SubPost.title, SubPostComment.cid, SubPostComment.pid,
                                    SubPostComment.uid, SubPostComment.time, SubPostComment.lastedit,
                                    SubPostComment.content, SubPostComment.status, SubPostComment.score,
                                    SubPostComment.parentcid, SubPost.posted)
        com = com.join(SubPost).switch(SubPostComment).join(Sub, on=(Sub.sid == SubPost.sid))
        com = com.where(SubPostComment.uid == uid).where(SubPostComment.status.is_null()).order_by(
            SubPostComment.time.desc()).paginate(page, 20).dicts()
    except SubPostComment.DoesNotExist:
        return False

    now = datetime.utcnow()
    limit = timedelta(days=config.site.archive_post_after)
    for c in com:
        c['archived'] = now - c['posted'].replace(tzinfo=None) > limit
    return com


def getSubMods(sid):
    modsquery = SubMod.select(User.uid, User.name, SubMod.power_level).join(User, on=(User.uid == SubMod.uid)).where(
        SubMod.sid == sid)
    modsquery = modsquery.where((User.status == 0) & (SubMod.invite == False))

    owner, mods, janitors, owner_uids, janitor_uids, mod_uids = ({}, {}, {}, [], [], [])
    for i in modsquery:
        if i.power_level == 0:
            owner[i.uid] = i.user.name
            owner_uids.append(i.uid)
        elif i.power_level == 1:
            mods[i.uid] = i.user.name
            mod_uids.append(i.uid)
        elif i.power_level == 2:
            janitors[i.uid] = i.user.name
            janitor_uids.append(i.uid)

    if not owner:
        owner['0'] = config.site.placeholder_account
    return {'owners': owner, 'mods': mods, 'janitors': janitors, 'all': owner_uids + janitor_uids + mod_uids}


def notify_mods(sid):
    """ Send the sub mods an updated open report count. """
    reports = (SubPostReport.select(fn.Count(SubPostReport.id)).
               join(SubPost).
               where((SubPost.sid == sid) & SubPostReport.open))
    comments = (SubPostCommentReport.select(fn.Count(SubPostCommentReport.id)).
                join(SubPostComment).join(SubPost).
                where((SubPost.sid == sid) & SubPostCommentReport.open))
    mods = (SubMod.select(SubMod.uid,
                          reports.alias('reports'),
                          comments.alias('comments')).
            where(SubMod.sub == sid))

    for mod in mods:
        socketio.emit('mod-notification',
                      {'update': [sid, mod.reports + mod.comments]},
                      namespace='/snt', room='user' + mod.uid)


# Relationship between the post type values in the CreateSubPost form
# and the SubMetadata keys that allow those post values.
ptype_names = {'link': 'allow_link_posts',
               'text': 'allow_text_posts',
               'upload': 'allow_upload_posts',
               'poll': 'allow_polls'}


def getSubData(sid, simple=False, extra=False):
    sdata = SubMetadata.select().where(SubMetadata.sid == sid)
    data = {'xmod2': [], 'sticky': []}
    for p in sdata:
        if p.key in ['tag', 'mod2i', 'xmod2', 'sticky']:
            if data.get(p.key):
                data[p.key].append(p.value)
            else:
                data[p.key] = [p.value]
        else:
            data[p.key] = p.value

    if not simple:
        try:
            data['wiki']
        except KeyError:
            data['wiki'] = ''

        if extra:
            if data.get('xmod2'):
                try:
                    data['xmods'] = User.select(User.uid, User.name).where(
                        (User.uid << data['xmod2']) & (User.status == 0)).dicts()
                except User.DoesNotExist:
                    data['xmods'] = []

        try:
            creator = User.select(User.uid, User.name, User.status).where(User.uid == data.get('mod')).dicts().get()
        except User.DoesNotExist:
            creator = {'uid': '0', 'name': 'Nobody'}
        data['creator'] = creator if creator.get('status', None) == 0 else {'uid': '0', 'name': _('[Deleted]')}

        try:
            data['stylesheet'] = SubStylesheet.get(SubStylesheet.sid == sid).content
        except SubStylesheet.DoesNotExist:
            data['stylesheet'] = ''

        try:
            data['rules'] = SubRule.select().join(Sub).where(Sub.sid == sid)
        except SubRule.DoesNotExist:
            data['rules'] = ''

    return data


def getModSubs(uid, power_level):
    # returns all subs that the user can moderate

    subs = SubMod.select(Sub, SubMod.power_level).join(Sub).where(
        (SubMod.uid == uid) & (SubMod.power_level <= power_level) & (SubMod.invite == False))

    return subs


@cache.memoize(5)
def getUserGivenScore(uid):
    pos = SubPostVote.select().where(SubPostVote.uid == uid).where(SubPostVote.positive == 1).count()
    neg = SubPostVote.select().where(SubPostVote.uid == uid).where(SubPostVote.positive == 0).count()
    cpos = SubPostCommentVote.select().where(SubPostCommentVote.uid == uid).where(
        SubPostCommentVote.positive == 1).count()
    cneg = SubPostCommentVote.select().where(SubPostCommentVote.uid == uid).where(
        SubPostCommentVote.positive == 0).count()

    return pos + cpos, neg + cneg, (pos + cpos) - (neg + cneg)


# Note for future self:
#  We keep constantly switching from camelCase to snake_case for function names.
#  For fucks sake make your mind.
def get_ignores(uid):
    return [x.target for x in UserIgnores.select().where(UserIgnores.uid == uid)]


def iter_validate_css(obj, uris):
    for x in obj:
        if x.__class__.__name__ == "URLToken":
            if x.value.startswith('%%') and x.value.endswith('%%'):
                token = x.value.replace('%%', '').strip()
                if uris.get(token):
                    x.value = uris.get(token)
            else:
                return _("URLs not allowed, uploaded files only"), x.source_column, x.source_line
        elif x.__class__.__name__ == "CurlyBracketsBlock":
            return iter_validate_css(x.content, {})
    return True


def validate_css(css, sid):
    """ Validates CSS. Returns parsed stylesheet or (errcode, col, line)"""
    st = tinycss2.parse_stylesheet(css, skip_comments=True, skip_whitespace=True)
    # create a map for uris.
    uris = {}
    for su in SubUploads.select().where(SubUploads.sid == sid):
        uris[su.name] = file_url(su.fileid)
    for x in st:
        if x.__class__.__name__ == "AtRule":
            if x.at_keyword.lower() == "import":
                return _("@import token not allowed"), x.source_column, x.source_line  # we do not allow @import
        elif x.__class__.__name__ == "QualifiedRule":  # down the hole we go.
            validation = iter_validate_css(x.content, uris)
            if validation is not True:
                return validation

    try:
        return 0, tinycss2.serialize(st)
    except TypeError:
        return _("Invalid CSS"), 0, 0


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
    return Message.create(sentby=mfrom, receivedby=to, subject=subject, mlink=link, content=content, posted=posted,
                          mtype=mtype)


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
            content += '<td><a href=' + url + '"><img src="' + thumbnail_url(post[
                                                                                 'thumbnail']) + '" alt="' + post[
                           'title'] + '"/></a></td>'
        content += '<td>Submitted by <a href=/u/' + post['user'] + '>' + post['user'] + '</a><br/>' + our_markdown(
            post['content'])
        if post['link']:
            content += '<a href="' + post['link'] + '">[link]</a> '
        content += '<a href="' + url + '">[comments]</a></td></tr></table>'
        fe = feed.add_entry()
        fe.id(url)
        fe.link(href=url)
        fe.title(post['title'])
        fe.author({'name': post['user']})
        fe.content(content, type="html")
        posted = post['posted'] if not post['edited'] else post['edited']
        fe.updated(posted.replace(tzinfo=timezone.utc))

    return feed


def metadata_to_dict(metadata):
    """ Transforms metadata query objects into dicts """
    res = {}
    for mdata in metadata:
        if mdata.value == '0':
            val = False
        elif mdata.value == '1':
            val = True
        else:
            val = mdata.value
        if mdata.key not in res:
            res[mdata.key] = val
        else:
            if not isinstance(res[mdata.key], list):
                res[mdata.key] = [res[mdata.key]]
            res[mdata.key].append(val)

    return res


def get_postmeta_dicts(pids):
    """Get the metadata for multiple posts."""
    pids = set(pids)
    postmeta_query = SubPostMetadata.select(SubPostMetadata.pid, SubPostMetadata.key, SubPostMetadata.value).where(
        SubPostMetadata.pid << pids)
    postmeta_entries = defaultdict(list)
    for pm in postmeta_query:
        postmeta_entries[pm.pid.pid].append(pm)

    postmeta = {pid: {} for pid in pids}
    for k, v in postmeta_entries.items():
        postmeta[k] = metadata_to_dict(v)
    return postmeta


# Log types
LOG_TYPE_USER = 10
LOG_TYPE_USER_BAN = 19
LOG_TYPE_USER_UNBAN = 54

LOG_TYPE_SUB_CREATE = 20
LOG_TYPE_SUB_SETTINGS = 21
LOG_TYPE_SUB_BAN = 22
LOG_TYPE_SUB_UNBAN = 23
LOG_TYPE_SUB_MOD_INVITE = 24
LOG_TYPE_SUB_MOD_ACCEPT = 25
LOG_TYPE_SUB_MOD_REMOVE = 26
LOG_TYPE_SUB_MOD_INV_CANCEL = 27
LOG_TYPE_SUB_MOD_INV_REJECT = 28
LOG_TYPE_SUB_CSS_CHANGE = 29
LOG_TYPE_SUB_STICKY_ADD = 50
LOG_TYPE_SUB_STICKY_DEL = 51
LOG_TYPE_SUB_DELETE_POST = 52
LOG_TYPE_SUB_DELETE_COMMENT = 53
LOG_TYPE_SUB_UNDELETE_POST = 58
LOG_TYPE_SUB_UNDELETE_COMMENT = 59

LOG_TYPE_SUB_TRANSFER = 30

LOG_TYPE_ANNOUNCEMENT = 41
LOG_TYPE_DOMAIN_BAN = 42
LOG_TYPE_DOMAIN_UNBAN = 43
LOG_TYPE_UNANNOUNCE = 44
LOG_TYPE_DISABLE_POSTING = 45
LOG_TYPE_ENABLE_POSTING = 46
LOG_TYPE_ENABLE_INVITE = 47
LOG_TYPE_DISABLE_INVITE = 48
LOG_TYPE_DISABLE_REGISTRATION = 49
LOG_TYPE_ENABLE_REGISTRATION = 50

LOG_TYPE_REPORT_CLOSE = 55
LOG_TYPE_REPORT_REOPEN = 56
LOG_TYPE_REPORT_CLOSE_RELATED = 57
LOG_TYPE_REPORT_POST_DELETED = 60
LOG_TYPE_REPORT_POST_UNDELETED = 61
LOG_TYPE_REPORT_COMMENT_DELETED = 62
LOG_TYPE_REPORT_COMMENT_UNDELETED = 63
LOG_TYPE_REPORT_USER_SITE_BANNED = 64
LOG_TYPE_REPORT_USER_SUB_BANNED = 65
LOG_TYPE_REPORT_USER_SITE_UNBANNED = 66
LOG_TYPE_REPORT_USER_SUB_UNBANNED = 67
LOG_TYPE_REPORT_NOTE = 68
LOG_TYPE_EMAIL_DOMAIN_BAN = 69
LOG_TYPE_EMAIL_DOMAIN_UNBAN = 70
LOG_TYPE_DISABLE_CAPTCHAS = 71
LOG_TYPE_ENABLE_CAPTCHAS = 72
LOG_TYPE_STICKY_SORT_NEW = 73
LOG_TYPE_STICKY_SORT_TOP = 74


def create_sitelog(action, uid, comment='', link=''):
    SiteLog.create(action=action, uid=uid, desc=comment, link=link)


# Note: `admin` makes the entry appear on the sitelog. I should rename it
def create_sublog(action, uid, sid, comment='', link='', admin=False, target=None):
    SubLog.create(action=action, uid=uid, sid=sid, desc=comment, link=link, admin=admin, target=target)


# `id` is the report id
def create_reportlog(action, uid, obj_id, log_type='', related=False, original_report='', desc=''):
    if log_type == 'post' and related == False:
        PostReportLog.create(action=action, uid=uid, id=obj_id, desc=desc)
    elif log_type == 'comment' and related == False:
        CommentReportLog.create(action=action, uid=uid, id=obj_id, desc=desc)
    elif log_type == 'post' and related == True:
        PostReportLog.create(action=action, uid=uid, id=obj_id, desc=original_report)
    elif log_type == 'comment' and related == True:
        CommentReportLog.create(action=action, uid=uid, id=obj_id, desc=original_report)


def is_domain_banned(addr, domain_type):
    if domain_type == 'link':
        key = 'banned_domain'
        netloc = urlparse(addr).netloc
    elif domain_type == 'email':
        key = 'banned_email_domain'
        netloc = addr.split('@')[1]
    else:
        raise RuntimeError

    bans = SiteMetadata.select().where(SiteMetadata.key == key)
    banned_domains, banned_domains_b = ([], [])
    for ban in bans:
        banned_domains.append(ban.value)
        banned_domains_b.append('.' + ban.value)

    if (netloc in banned_domains) or (netloc.endswith(tuple(banned_domains_b))):
        return True
    return False


def captchas_required():
    try:
        return SiteMetadata.get(SiteMetadata.key == 'require_captchas').value == '1'
    except SiteMetadata.DoesNotExist:
        return True


def create_captcha():
    """ Generates a captcha image.
    Returns a tuple with a token and the base64 encoded image """
    if not captchas_required():
        return None
    token = str(uuid.uuid4())
    captchagen = ImageCaptcha(width=250, height=70)
    if random.randint(1, 50) == 1:
        captcha = random.choice(
            ['help me', 'sorry', 'hello', 'see me', 'observe', 'stop', 'nooooo', 'i can see', 'free me', 'behind you',
             'murder', 'shhhh', 'reeeee', 'come here', 'people die', 'it hurts', 'go away', 'touch me', 'last words',
             'closer', 'rethink', 'it is dark', 'it is cold', 'i am dying', 'quit staring', 'lock door'])
    else:
        captcha = ''.join(random.choice('abcdefghijklmnopqrstuvwxyz0123456789') for _ in range(random.randint(4, 6)))

    data = captchagen.generate(captcha.upper())
    b64captcha = base64.b64encode(data.getvalue()).decode()
    captcha = captcha.replace(' ', '').replace('0', 'o')

    rconn.setex('cap-' + token, value=captcha, time=300)  # captcha valid for 5 minutes.

    return token, b64captcha


def validate_captcha(token, response):
    if config.app.testing or config.app.development or not captchas_required():
        return True
    cap = rconn.get('cap-' + token)
    if cap:
        response = response.replace(' ', '').replace('0', 'o')
        rconn.delete('cap-' + token)
        if cap.decode().lower() == response.lower():
            return True
    return False


def get_comment_tree(pid, sid, comments, root=None, only_after=None, uid=None, provide_context=True,
                     include_history=False, postmeta=None):
    """ Returns a fully paginated and expanded comment tree.

    TODO: Move to misc and implement globally
    @param include_history:
    @param pid: post for comments
    @param sid: sub for post
    @param comments: bare list of comments (only cid and parentcid)
    @param root: if present, the root comment to start building the tree on
    @param only_after: removes all siblings of `root` before the cid on its value
    @param uid:
    @param provide_context:
    @param postmeta: SubPostMetadata dict if it has already been fetched
    """

    if postmeta is None:
        postmeta = metadata_to_dict(SubPostMetadata.select().where((SubPostMetadata.pid == pid) &
                                                                   (SubPostMetadata.key == 'sticky_cid')))
    sticky_cid = postmeta.get('sticky_cid')

    def build_tree(tuff, rootcid=None):
        """ Builds a comment tree """
        res = []
        for i in tuff[::]:
            if i['parentcid'] == rootcid:
                tuff.remove(i)
                i['children'] = build_tree(tuff, rootcid=i['cid'])
                res.append(i)
        return res

    # 2 - Build bare comment tree
    comment_tree = build_tree(list(comments))

    # 2.1 - get only a branch of the tree if necessary
    if root:
        def select_branch(commentslst, rootcid):
            """ Finds a branch with a certain root and returns a new tree """
            for i in commentslst:
                if i['cid'] == rootcid:
                    return i
                k = select_branch(i['children'], rootcid)
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
    elif sticky_cid is not None:
        # If there is a sticky comment, move it to the top.
        elem = list(filter(lambda x: x['cid'] == sticky_cid, comment_tree))
        if elem:
            comment_tree.remove(elem[0])
            if only_after is None:
                comment_tree.insert(0, elem[0])

    # 3 - Trim tree (remove all children of depth=3 comments, all siblings after #5
    cid_list = []
    trimmed = False

    def recursive_check(tree, depth=0, trimmedtree=None, pcid=''):
        """ Recursively checks tree to apply pagination limits """
        or_len = len(tree)
        if only_after and not trimmedtree:
            imf = list(filter(lambda x: x['cid'] == only_after, tree))
            if imf:
                try:
                    tree = tree[tree.index(imf[0]) + 1:]
                except IndexError:
                    return []
                or_len = len(tree)
                trimmedtree = True
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
            i['children'] = recursive_check(i['children'], depth + 1, pcid=i['cid'], trimmedtree=trimmedtree)

        return tree

    comment_tree = recursive_check(comment_tree, trimmedtree=trimmed)

    # 4 - Populate the tree (get all the data and cram it into the tree)
    expcomms = SubPostComment.select(SubPostComment.cid, SubPostComment.content, SubPostComment.lastedit,
                                     SubPostComment.score, SubPostComment.status, SubPostComment.time,
                                     SubPostComment.pid, SubPostComment.distinguish, SubPostComment.parentcid,
                                     User.name.alias('user'),
                                     *([SubPostCommentVote.positive, SubPostComment.uid] if uid else [
                                         SubPostComment.uid]),  # silly hack
                                     User.status.alias('userstatus'), SubPostComment.upvotes, SubPostComment.downvotes)
    expcomms = expcomms.join(User, on=(User.uid == SubPostComment.uid)).switch(SubPostComment)
    if uid:
        expcomms = expcomms.join(SubPostCommentVote, JOIN.LEFT_OUTER,
                                 on=((SubPostCommentVote.uid == uid) & (SubPostCommentVote.cid == SubPostComment.cid)))
    expcomms = expcomms.where(SubPostComment.cid << cid_list).dicts()

    commdata = {}
    is_admin = current_user.is_admin()
    is_mod = current_user.is_mod(sid, 1)
    for comm in expcomms:
        comm['history'] = []
        comm['visibility'] = ''
        comm['sticky'] = (comm['cid'] == sticky_cid)

        if comm['status']:
            if comm['status'] == 1:
                if is_admin:
                    comm['visibility'] = 'admin-self-del'
                elif is_mod:
                    comm['visibility'] = 'mod-self-del'
                else:
                    comm['user'] = _('[Deleted]')
                    comm['uid'] = None
                    comm['content'] = ''
                    comm['lastedit'] = None
                    comm['visibility'] = 'none'
            elif comm['status'] == 2:
                if is_admin or is_mod:
                    comm['visibility'] = 'mod-del'
                else:
                    comm['user'] = _('[Deleted]')
                    comm['uid'] = None
                    comm['content'] = ''
                    comm['lastedit'] = None
                    comm['visibility'] = 'none'

        if comm['userstatus'] == 10:
            comm['user'] = _('[Deleted]')
            comm['uid'] = None
            if comm['status'] == 1:
                comm['content'] = ''
                comm['lastedit'] = None
                comm['visibility'] = 'none'
        # del comm['userstatus']
        commdata[comm['cid']] = comm

    if config.site.edit_history and include_history:
        history = SubPostCommentHistory.select(SubPostCommentHistory.cid, SubPostCommentHistory.content,
                                               SubPostCommentHistory.datetime) \
            .where(SubPostCommentHistory.cid << cid_list) \
            .order_by(SubPostCommentHistory.datetime.desc()).dicts()
        for hist in history:
            if hist['cid'] in commdata:
                hist['content'] = our_markdown(hist['content'])
                commdata[hist['cid']]['history'].append(hist)

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


@cache.memoize(1)
def get_notif_count():
    """ Temporary till we get rid of the old template """
    return Notification.select().where(
        (Notification.target == current_user.uid) & Notification.read.is_null(True)).count()


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
        return jsonify(msg=_("Unknown error. User disappeared")), 403

    if value == "up" or value is True:
        voteValue = 1
    elif value == "down" or value is False:
        voteValue = -1
        if user.given < 0:
            return jsonify(msg=_('Score balance is negative')), 403
    else:
        return jsonify(msg=_("Invalid vote value")), 400

    if target_type == "post":
        target_model = SubPost
        try:
            target = SubPost.select(SubPost.uid, SubPost.score, SubPost.upvotes, SubPost.downvotes,
                                    SubPost.pid.alias('id'), SubPost.posted, SubPost.sid)
            target = target.where((SubPost.pid == pcid) & (SubPost.deleted == 0)).get()
        except SubPost.DoesNotExist:
            return jsonify(msg=_('Post does not exist')), 404

        if target.deleted:
            return jsonify(msg=_("You can't vote on deleted posts")), 400

        try:
            qvote = SubPostVote.select().where(SubPostVote.pid == pcid).where(SubPostVote.uid == uid).get()
        except SubPostVote.DoesNotExist:
            qvote = False
    elif target_type == "comment":
        target_model = SubPostComment
        try:
            target = SubPostComment.select(SubPostComment.uid, SubPost.sid, SubPostComment.pid, SubPostComment.status,
                                           SubPostComment.score,
                                           SubPostComment.upvotes, SubPostComment.downvotes,
                                           SubPostComment.cid.alias('id'), SubPostComment.time.alias('posted'))
            target = target.join(SubPost).where(SubPostComment.cid == pcid).where(SubPostComment.status.is_null(True))
            target = target.objects().get()
        except SubPostComment.DoesNotExist:
            return jsonify(msg=_('Comment does not exist')), 404

        if target.uid_id == user.uid:
            return jsonify(msg=_("You can't vote on your own comments")), 400
        if target.status:
            return jsonify(msg=_("You can't vote on deleted comments")), 400

        try:
            qvote = SubPostCommentVote.select().where(SubPostCommentVote.cid == pcid).where(
                SubPostCommentVote.uid == uid).get()
        except SubPostCommentVote.DoesNotExist:
            qvote = False
    else:
        return jsonify(msg=_("Invalid target")), 400

    if is_sub_banned(target.sid, uid=user.uid):
        return jsonify(msg=_('You are banned on this sub.')), 403

    if (datetime.utcnow() - target.posted.replace(tzinfo=None)) > timedelta(days=config.site.archive_post_after):
        return jsonify(msg=_("Post is archived")), 400

    positive = True if voteValue == 1 else False
    undone = False

    if qvote is not False:
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
                upd_q = target_model.update(score=target_model.score + (voteValue * 2),
                                            upvotes=target_model.upvotes + 1, downvotes=target_model.downvotes - 1)
            else:
                upd_q = target_model.update(score=target_model.score + (voteValue * 2),
                                            upvotes=target_model.upvotes - 1, downvotes=target_model.downvotes + 1)
            new_score = (voteValue * 2)
            User.update(score=User.score + (voteValue * 2)).where(User.uid == target.uid).execute()
            User.update(given=User.given + voteValue).where(User.uid == uid).execute()
    else:  # First vote cast on post
        now = datetime.utcnow()
        if target_type == "post":
            SubPostVote.create(pid=pcid, uid=uid, positive=positive, datetime=now)
        else:
            SubPostCommentVote.create(cid=pcid, uid=uid, positive=positive, datetime=now)

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

        socketio.emit('yourvote',
                      {'pid': target.id, 'status': voteValue if not undone else 0, 'score': target.score + new_score},
                      namespace='/snt',
                      room='user' + uid)
    else:
        upd_q.where(SubPostComment.cid == target.id).execute()

    socketio.emit('uscore', {'score': target.uid.score + new_score},
                  namespace='/snt', room="user" + target.uid_id)

    return jsonify(score=target.score + new_score, rm=undone)


def is_sub_mod(uid, sid, power_level, can_admin=False):
    try:
        SubMod.get((SubMod.sid == sid) & (SubMod.uid == uid) & (SubMod.power_level <= power_level) & (
                SubMod.invite == False))
        return True
    except SubMod.DoesNotExist:
        pass

    if can_admin:  # Admins mod all defaults
        try:
            SiteMetadata.get((SiteMetadata.key == 'default') & (SiteMetadata.value == sid))
            return True
        except SiteMetadata.DoesNotExist:
            pass
    return False


def getReports(view, status, page, *_args, **kwargs):
    # view = STR either 'mod' or 'admin'
    # status = STR: 'open', 'closed', or 'all'
    sid = kwargs.get('sid', None)
    report_type = kwargs.get('type', None)
    report_id = kwargs.get('report_id', None)
    related = kwargs.get('related', None)

    # Get all reports on posts and comments for requested subs,
    Reported = User.alias()
    all_post_reports = SubPostReport.select(
        Value('post').alias('type'),
        SubPostReport.id,
        SubPostReport.pid,
        Value(None).alias('cid'),
        User.name.alias('reporter'),
        Reported.name.alias('reported'),
        SubPostReport.datetime,
        SubPostReport.reason,
        SubPostReport.open,
        Sub.name.alias('sub')
    ).join(User, on=User.uid == SubPostReport.uid) \
        .switch(SubPostReport)

    # filter by if Mod or Admin view and if filtering by sub, specific post, or related posts
    if view == 'admin' and not sid:
        sub_post_reports = all_post_reports.where(SubPostReport.send_to_admin == True).join(SubPost).join(Sub).join(
            SubMod)
    elif view == 'admin' and sid:
        sub_post_reports = all_post_reports.where(SubPostReport.send_to_admin == True).join(SubPost).join(Sub).where(
            Sub.sid == sid).join(SubMod)
    elif view == 'mod' and sid:
        sub_post_reports = all_post_reports.join(SubPost).join(Sub).where(Sub.sid == sid).join(SubMod).where(
            SubMod.user == current_user.uid)
    elif report_id and report_type == 'post' and not related:
        sub_post_reports = all_post_reports.where(SubPostReport.id == report_id).join(SubPost).join(Sub).join(SubMod)
    elif report_id and report_type == 'post' and related:
        base_report = getReports('mod', 'all', 1, type='post', report_id=report_id, related=False)
        sub_post_reports = all_post_reports.where(SubPostReport.pid == base_report['pid']).join(SubPost).join(Sub).join(
            SubMod)
    else:
        sub_post_reports = all_post_reports.join(SubPost).join(Sub).join(SubMod).where(SubMod.user == current_user.uid)

    sub_post_reports = sub_post_reports.join(Reported, on=Reported.uid == SubPost.uid)

    # filter by requested status
    open_sub_post_reports = sub_post_reports.where(SubPostReport.open == True)
    closed_sub_post_reports = sub_post_reports.where(SubPostReport.open == False)

    # Do it all again for comments
    Reported = User.alias()
    all_comment_reports = SubPostCommentReport.select(
        Value('comment').alias('type'),
        SubPostCommentReport.id,
        SubPostComment.pid,
        SubPostCommentReport.cid,
        User.name.alias('reporter'),
        Reported.name.alias('reported'),
        SubPostCommentReport.datetime,
        SubPostCommentReport.reason,
        SubPostCommentReport.open,
        Sub.name.alias('sub')
    ).join(User, on=User.uid == SubPostCommentReport.uid) \
        .switch(SubPostCommentReport)

    # filter by if Mod or Admin view and if filtering by sub or specific post
    if view == 'admin' and not sid:
        sub_comment_reports = all_comment_reports.where(SubPostCommentReport.send_to_admin == True).join(
            SubPostComment).join(SubPost).join(Sub).join(SubMod)
    elif view == 'admin' and sid:
        sub_comment_reports = all_comment_reports.where(SubPostCommentReport.send_to_admin == True).join(
            SubPostComment).join(SubPost).join(Sub).where(Sub.sid == sid).join(SubMod)
    elif view == 'mod' and sid:
        sub_comment_reports = all_comment_reports.join(SubPostComment).join(SubPost).join(Sub).where(
            Sub.sid == sid).join(SubMod).where(SubMod.user == current_user.uid)
    elif report_id and report_type == 'comment' and not related:
        sub_comment_reports = all_comment_reports.where(SubPostCommentReport.id == report_id).join(SubPostComment).join(
            SubPost).join(Sub).join(SubMod)
    elif report_id and report_type == 'comment' and related:
        base_report = getReports('mod', 'all', 1, type='comment', report_id=report_id, related=False)
        sub_comment_reports = all_comment_reports.where(SubPostCommentReport.cid == base_report['cid']).join(
            SubPostComment).join(SubPost).join(Sub).join(SubMod)
    else:
        sub_comment_reports = all_comment_reports.join(SubPostComment).join(SubPost).join(Sub).join(SubMod).where(
            SubMod.user == current_user.uid)

    sub_comment_reports = sub_comment_reports.join(Reported, on=Reported.uid == SubPostComment.uid)

    # filter by requested status
    open_sub_comment_reports = sub_comment_reports.where(SubPostCommentReport.open == True)
    closed_sub_comment_reports = sub_comment_reports.where(SubPostCommentReport.open == False)

    # Define open and closed queries and counts depending on whether query is for specific post
    if report_id and report_type == 'post':
        open_query = open_sub_post_reports
        closed_query = closed_sub_post_reports
    elif report_id and report_type == 'comment':
        open_query = open_sub_comment_reports
        closed_query = closed_sub_comment_reports
    else:
        open_query = open_sub_post_reports | open_sub_comment_reports
        closed_query = closed_sub_post_reports | closed_sub_comment_reports

    open_report_count = open_query.count()
    closed_report_count = closed_query.count()

    # Order and paginate queries
    if status == 'open':
        query = open_query.order_by(open_query.c.datetime.desc())
        query = query.paginate(page, 50)
    elif status == 'closed':
        query = closed_query.order_by(closed_query.c.datetime.desc())
        query = query.paginate(page, 50)
    elif status == 'all':
        query = open_query | closed_query
        query = query.order_by(closed_query.c.datetime.desc())
        query = query.paginate(page, 50)
    else:
        return jsonify(msg=_('Invalid status request')), 400

    if report_id and report_type and not related:
        # If only getting one report, this is a more usable format
        return list(query.dicts())[0]

    return {'query': list(query.dicts()), 'open_report_count': str(open_report_count),
            'closed_report_count': str(closed_report_count)}


def slugify(text):
    slug = s_slugify(text, max_length=80)
    return slug if slug else '_'


def logging_init_app(app):
    config = app.config['THROAT_CONFIG']
    if 'logging' in config:
        logging.config.dictConfig(config.logging)
        add_context_to_log_records(config.logging)
    elif config.app.development or config.app.testing:
        logging.basicConfig(level=logging.DEBUG)
        logging.getLogger(app.logger.name + '.socketio').setLevel(logging.WARNING)
        logging.getLogger("engineio.server").setLevel(logging.WARNING)
        logging.getLogger("socketio.server").setLevel(logging.WARNING)
    else:
        logging.basicConfig(level=logging.WARNING)


def add_context_to_log_records(config):
    # Extract the keys used in the formatters in config.yaml.
    keys = set()
    if 'formatters' in config:
        for formatter in config['formatters'].values():
            if 'format' in formatter:
                keys |= set(re.findall(r'%\((.+?)\)', formatter['format']))

    old_factory = logging.getLogRecordFactory()
    if old_factory.__module__ == __name__:  # So the tests don't make a chain of these.
        old_factory = old_factory.old_factory

    # If any formatter keys refer to request or current_app, fill in the values.
    def record_factory(*args, **kwargs):
        record = old_factory(*args, **kwargs)
        unavailable = ''
        for k in keys:
            splits = k.split('.')
            if len(splits) > 1:
                var, attr = splits[0:2]
                if var == 'request':
                    if request:
                        if attr == 'headers' and len(splits) == 3:
                            record.__dict__[k] = request.headers.get(splits[2], unavailable)
                        else:
                            record.__dict__[k] = getattr(request, attr, unavailable)
                    else:
                        record.__dict__[k] = unavailable
                elif var == 'current_user':
                    # Peek at the current user but don't load it if not loaded.
                    if user_is_loaded():
                        record.__dict__[k] = getattr(current_user, attr, unavailable)
                    else:
                        record.__dict__[k] = unavailable
                elif var == 'g':
                    if has_app_context():
                        record.__dict__[k] = getattr(g, attr, unavailable)
                    else:
                        record.__dict__[k] = unavailable
        return record

    record_factory.old_factory = old_factory
    logging.setLogRecordFactory(record_factory)


def word_truncate(content, max_length, suffix='...'):
    return content if len(content) <= max_length else content[:max_length].rsplit(' ', 1)[0] + suffix


def recent_activity(sidebar=True):
    if not config.site.recent_activity.enabled:
        return False

    # XXX: The queries below don't work on sqlite
    # TODO: Make em work?
    if 'SqliteDatabase' in config.database.engine:
        return []

    # TODO: Pagination?
    post_activity = SubPost.select(
        Value('post').alias('type'),
        SubPost.title.alias('content'),
        User.name.alias('user'),
        SubPost.posted.alias('time'),
        SubPost.pid,
        SubPost.sid
    )
    post_activity = post_activity.join(User).switch(SubPost)
    post_activity = post_activity.where(SubPost.deleted == 0).order_by(SubPost.pid.desc()).limit(50)

    comment_activity = SubPostComment.select(
        Value('comment').alias('type'),
        SubPostComment.content,
        User.name.alias('user'),
        SubPostComment.time.alias('time'),
        SubPost.pid,
        SubPost.sid
    )
    comment_activity = comment_activity.join(User).switch(SubPostComment).join(SubPost)
    comment_activity = comment_activity.where(SubPostComment.status.is_null(True)).order_by(
        SubPostComment.time.desc()).limit(50)

    if sidebar and config.site.recent_activity.defaults_only:
        defaults = [x.value for x in SiteMetadata.select().where(SiteMetadata.key == 'default')]
        post_activity = post_activity.where(SubPost.sid << defaults)
        comment_activity = comment_activity.where(SubPost.sid << defaults)

    activity = (comment_activity | post_activity)
    activity = activity.alias('subquery')
    # This abomination was created to work aroun a bug (?) where mysql won't use the index of the sub table
    data = activity.select(activity.c.type, activity.c.content, activity.c.user, activity.c.time, activity.c.pid,
                           activity.c.sid, Sub.name.alias('sub'))
    data = data.join(Sub, on=Sub.sid == activity.c.sid)
    if sidebar and config.site.recent_activity.comments_only:
        data = data.where(activity.c.type == 'comment')
    data = data.order_by(activity.c.time.desc())
    data = data.limit(config.site.recent_activity.max_entries if sidebar else 50)
    data = data.dicts().execute(db)

    for rec in data:
        if rec['type'] != 'post':
            parsed = BeautifulSoup(our_markdown(rec['content']))
            for spoiler in parsed.findAll('spoiler'):
                spoiler.string.replace_with('█' * len(spoiler.string))
            stripped = parsed.findAll(text=True)
            rec['content'] = word_truncate(''.join(stripped).replace('\n', ' '), 350)

    return data


logger = LocalProxy(lambda: current_app.logger)

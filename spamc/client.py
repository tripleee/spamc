# -*- coding: utf-8 -*-
# vim: ai ts=4 sts=4 et sw=4
# spamc - Python spamassassin spamc client library
# Copyright (C) 2015  Andrew Colin Kissa <andrew@topdog.za.net>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""
spamc: Python spamassassin spamc client library
client
"""
import os
import errno
import types
import socket

from zlib import compress
# from mimetools import Message
from email.parser import Parser

from spamc.utils import load_backend
from spamc.conn import SpamCTcpConnector, SpamCUnixConnector

from spamc.regex import RESPONSE_RE, SPAM_RE, PART_RE, RULE_RE, SPACE_RE
from spamc.exceptions import SpamCError, SpamCTimeOutError, SpamCResponseError

PROTOCOL_VERSION = 'SPAMC/1.5'


def _check_action(action):
    """check for invalid actions"""
    if isinstance(action, types.StringTypes):
        action = action.lower()

    if action not in ['learn', 'forget', 'report', 'revoke']:
        raise SpamCError('The action option is invalid')
    return action


# pylint: disable=R0912,R0915
def get_response(cmd, conn, raw_symbols=False):
    """
    Return a response.

    The optional argument raw_symbols requests for the SYMBOLS command
    to abstain from any parsing, and instead return the raw result in
    the 'symbols' member of the response.
    """
    resp = conn.socket().makefile('rb', -1)
    resp_dict = dict(
        code=0,
        message='',
        isspam=False,
        score=0.0,
        basescore=0.0,
        report=[],
        symbols=[],
        headers={},
    )
    raw_symbols_list = []

    if cmd == 'TELL':
        resp_dict['didset'] = False
        resp_dict['didremove'] = False

    data = resp.read()
    lines = data.split('\r\n')
    for index, line in enumerate(lines):
        if index == 0:
            match = RESPONSE_RE.match(line)
            if not match:
                raise SpamCResponseError(
                    'spamd unrecognized response: %s' % data)
            resp_dict.update(match.groupdict())
            resp_dict['code'] = int(resp_dict['code'])
        else:
            if not line.strip():
                continue
            match = SPAM_RE.match(line)
            if match:
                tmp = match.groupdict()
                resp_dict['score'] = float(tmp['score'])
                resp_dict['basescore'] = float(tmp['basescore'])
                resp_dict['isspam'] = tmp['isspam'] in ['True', 'Yes']
            else:
                if cmd == 'SYMBOLS':
                    if not line.lower().startswith('content-length:'):
                        raw_symbols_list.append(line)
                    match = PART_RE.findall(line)
                    for part in match:
                            resp_dict['symbols'].append(part)
            if not match and cmd != 'PROCESS':
                match = RULE_RE.findall(line)
                if match:
                    resp_dict['report'] = []
                    for part in match:
                        score = part[0] + part[1]
                        score = score.strip()
                        resp_dict['report'].append(
                            dict(score=score,
                                 name=part[2],
                                 description=SPACE_RE.sub(" ", part[3])))
            if line.startswith('DidSet:'):
                resp_dict['didset'] = True
            if line.startswith('DidRemove:'):
                resp_dict['didremove'] = True
    if cmd == 'SYMBOLS' and raw_symbols:
        resp_dict['symbols'] = ['\n'.join(raw_symbols_list)]
    if cmd == 'PROCESS':
        resp_dict['message'] = ''.join(lines[4:]) + '\r\n'
    if cmd == 'HEADERS':
        parser = Parser()
        headers = parser.parsestr('\r\n'.join(lines[4:]), headersonly=True)
        for key in headers.keys():
            resp_dict['headers'][key] = headers[key]
    return resp_dict


# pylint: disable=R0902
class SpamC(object):
    """Spamc Client class"""
    # pylint: disable=R0913

    def __init__(self,
                 host=None,
                 port=783,
                 socket_file='/var/run/spamassassin/spamd.sock',
                 user=None,
                 timeout=None,
                 wait_tries=0.3,
                 max_tries=3,
                 backend="thread",
                 gzip=None,
                 compress_level=6,
                 is_ssl=None,
                 **ssl_args):
        """Init"""
        self.host = host
        self.port = port
        self.socket_file = socket_file
        self.user = user
        if isinstance(backend, str):
            self.backend_mod = load_backend(backend)
        else:
            self.backend_mod = backend
        self.max_tries = max_tries
        self.wait_tries = wait_tries
        self.timeout = timeout
        self.gzip = gzip
        self.compress_level = compress_level
        self.is_ssl = is_ssl
        self.ssl_args = ssl_args or {}

    def get_connection(self):
        """Creates a new connection"""
        if self.host is None:
            connector = SpamCUnixConnector
            conn = connector(self.socket_file, self.backend_mod)
        else:
            connector = SpamCTcpConnector
            conn = connector(
                self.host,
                self.port,
                self.backend_mod,
                is_ssl=self.is_ssl,
                **self.ssl_args)
        return conn

    def get_headers(self, cmd, msg_length, extra_headers):
        """Returns the headers string based on command to execute"""
        cmd_header = "%s %s" % (cmd, PROTOCOL_VERSION)
        len_header = "Content-length: %s" % msg_length
        headers = [cmd_header, len_header]
        if self.user:
            user_header = "User: %s" % self.user
            headers.append(user_header)
        if self.gzip:
            headers.append("Compress: zlib")
        if extra_headers is not None:
            for key in extra_headers:
                if key.lower() != 'content-length':
                    headers.append("%s: %s" % (key, extra_headers[key]))
        headers.append('')
        headers.append('')
        return '\r\n'.join(headers)

    # pylint: disable=E1103
    def perform(self, cmd, msg='', extra_headers=None, raw_symbols=False):
        """Perform the call"""
        tries = 0
        while 1:
            conn = None
            try:
                conn = self.get_connection()
                if hasattr(msg, 'read') and hasattr(msg, 'fileno'):
                    msg_length = str(os.fstat(msg.fileno()).st_size)
                elif hasattr(msg, 'read'):
                    msg.seek(0, 2)
                    msg_length = str(msg.tell() + 2)
                else:
                    if msg:
                        try:
                            msg_length = str(len(msg) + 2)
                        except TypeError:
                            conn.close()
                            raise ValueError(
                                'msg param should be a string or file handle')
                    else:
                        msg_length = '2'

                headers = self.get_headers(cmd, msg_length, extra_headers)

                if isinstance(msg, types.StringTypes):
                    if self.gzip and msg:
                        msg = compress(msg + '\r\n', self.compress_level)
                    else:
                        msg = msg + '\r\n'
                    conn.send(headers + msg)
                else:
                    conn.send(headers)
                    if hasattr(msg, 'read'):
                        if hasattr(msg, 'seek'):
                            msg.seek(0)
                        conn.sendfile(msg, self.gzip, self.compress_level)
                conn.send('\r\n')
                try:
                    conn.socket().shutdown(socket.SHUT_WR)
                except socket.error:
                    pass
                return get_response(cmd, conn, raw_symbols)
            except socket.gaierror, err:
                if conn is not None:
                    conn.release()
                raise SpamCError(str(err))
            except socket.timeout, err:
                if conn is not None:
                    conn.release()
                raise SpamCTimeOutError(str(err))
            except socket.error, err:
                if conn is not None:
                    conn.close()
                errors = (errno.EAGAIN, errno.EPIPE, errno.EBADF,
                          errno.ECONNRESET)
                if err[0] not in errors or tries >= self.max_tries:
                    raise SpamCError("socket.error: %s" % str(err))
            except BaseException:
                if conn is not None:
                    conn.release()
                raise
            tries += 1
            self.backend_mod.sleep(self.wait_tries)

    def check(self, msg):
        """Check if the passed message is spam or not"""
        return self.perform('CHECK', msg)

    def symbols(self, msg):
        """Check if message is spam or not, and return score plus list
        of symbols hit"""
        return self.perform('SYMBOLS', msg)

    def symbols_raw(self, msg):
        """Check if the message is spam or not, and return the score and
        the string of unparsed, raw symbols returned by the server.
        This is different from symbols(), which attempts to parse the
        result."""
        return self.perform('SYMBOLS', msg, raw_symbols=True)

    def report(self, msg):
        """Check if message is spam or not, and return score plus report"""
        return self.perform('REPORT', msg)

    def report_ifspam(self, msg):
        """Check if message is spam or not, and return score plus report
        if the message is spam"""
        return self.perform('REPORT_IFSPAM', msg)

    def ping(self):
        """Return a confirmation that spamd is alive"""
        return self.perform('PING')

    def process(self, msg):
        """Check if message is spam or not, and return modified message"""
        return self.perform('PROCESS', msg)

    def headers(self, msg):
        """Check if message is spam or not, and return only modified
        headers, not body"""
        return self.perform('HEADERS', msg)

    def tell(self, msg, action, learnas=''):
        """Tell what type of we are to process and what should be done
        with that message. This includes setting or removing a local
        or a remote database (learning, reporting, forgetting, revoking)."""
        action = _check_action(action)
        mode = learnas.upper()

        headers = {
            'Message-class': '',
            'Set': 'local',
        }

        if action == 'learn':
            if mode == 'SPAM':
                headers['Message-class'] = 'spam'
            elif mode in ['HAM', 'NOTSPAM', 'NOT_SPAM']:
                headers['Message-class'] = 'ham'
            else:
                raise SpamCError('The learnas option is invalid')
        elif action == 'forget':
            del headers['Message-class']
            del headers['Set']
            headers['Remove'] = 'local'
        elif action == 'report':
            headers['Message-class'] = 'spam'
            headers['Set'] = 'local, remote'
        elif action == 'revoke':
            headers['Message-class'] = 'ham'
            headers['Remove'] = 'remote'
        return self.perform('TELL', msg, headers)

    def learn(self, msg, learnas):
        """Learn message as spam/ham or forget"""
        if not isinstance(learnas, types.StringTypes):
            raise SpamCError('The learnas option is invalid')
        if learnas.lower() == 'forget':
            resp = self.tell(msg, 'forget')
        else:
            resp = self.tell(msg, 'learn', learnas)
        return resp

    def revoke(self, msg):
        """Tell spamd message is not spam"""
        return self.tell(msg, 'revoke')


if __name__ == '__main__':
    import sys
    #print(SpamC(host='localhost', port=783, ssl=True).symbols(open(sys.argv[1])))
    print(SpamC(host='localhost', port=1344, ssl=False).symbols(open(sys.argv[1])))

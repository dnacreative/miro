# Miro - an RSS based video player application
# Copyright (C) 2005, 2006, 2007, 2008, 2009, 2010, 2011
# Participatory Culture Foundation
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301 USA
#
# In addition, as a special exception, the copyright holders give
# permission to link the code of portions of this program with the OpenSSL
# library.
#
# You must obey the GNU General Public License in all respects for all of
# the code used other than OpenSSL. If you modify file(s) with this
# exception, you may extend this exception to your version of the file(s),
# but you are not obligated to do so. If you do not wish to do so, delete
# this exception statement from your version. If you delete this exception
# statement from all source files in the program, then also delete it here.

import itertools

from miro.dl_daemon import command

# Hack: we import from miro.httpauth here even though this module is about to
# replace it.  This works because the timing of how we override modules
from miro.httpauthtools import HTTPPasswordList, decode_auth_header

requestIdGenerator = itertools.count()
waitingHTTPAuthCallbacks = {}

password_list = HTTPPasswordList()

def handle_http_auth_response(id_, authHeader):
    callback = waitingHTTPAuthCallbacks.pop(id_)
    callback(authHeader)

def find_http_auth(url, auth_header=None):
    global password_list
    try:
        if auth_header is not None:
            auth_scheme, realm, domain = decode_auth_header(auth_header)
        else:
            realm = None
    except (AssertionError, ValueError):
        # auth_header is malformed, so use a None realm
        realm = None

    return password_list.find(url, realm)

def remove(auth):
    global password_list
    password_list.remove(auth)
    from miro.dl_daemon import daemon
    command.RemoveHTTPAuthCommand(daemon.LAST_DAEMON, auth.url,
                                  auth.realm).send()

def update_passwords(passwords):
    global password_list
    password_list.replace_passwords(passwords)

def ask_for_http_auth(callback, url, auth_header, location):
    # call decode_auth_header, which will raise ValueError or
    # AssertionError if auth_header is invalid
    decode_auth_header(auth_header)
    id_ = requestIdGenerator.next()
    waitingHTTPAuthCallbacks[id_] = callback
    from miro.dl_daemon import daemon
    c = command.AskForHTTPAuthCommand(daemon.LAST_DAEMON, id_, url,
            auth_header, location)
    c.send()

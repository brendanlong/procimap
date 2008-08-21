############################################################################
#    Copyright (C) 2008 by Michael Goerz                                   #
#    http://www.physik.fu-berlin.de/~goerz                                 #
#                                                                          #
#    This program is free software; you can redistribute it and#or modify  #
#    it under the terms of the GNU General Public License as published by  #
#    the Free Software Foundation; either version 3 of the License, or     #
#    (at your option) any later version.                                   #
#                                                                          #
#    This program is distributed in the hope that it will be useful,       #
#    but WITHOUT ANY WARRANTY; without even the implied warranty of        #
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the         #
#    GNU General Public License for more details.                          #
#                                                                          #
#    You should have received a copy of the GNU General Public License     #
#    along with this program; if not, write to the                         #
#    Free Software Foundation, Inc.,                                       #
#    59 Temple Place - Suite 330, Boston, MA  02111-1307, USA.             #
############################################################################

""" This module provides the ImapMailbox class, which is a wrapper around
    the imaplib module of the standard library, and a full implementation
    of the mailbox.Mailbox interface.
"""


import imaplib
import email.header
import email.utils
import tempfile
import os
import time
from email.generator import Generator
from cStringIO import StringIO
from mailbox import Mailbox

from ImapServer import ImapServer, NoSuchMailboxError
from ImapMessage import ImapMessage


DEFAULT_PAGER = 'less'

FIX_BUGGY_IMAP_FROMLINE = False # I used this for the standard IMAP server
                                # on SuSe Linux, which seems to be extremely
                                # buggy, and responds to requests for messages
                                # with an escaped(!) envelope-header. Don't
                                # use that server!



class ImapNotOkError(Exception):
    """ Raised if the imap server returns a non-OK status on any request """
    pass

class NoSuchUIDError(Exception):
    """ Raised if a message is requested with a non-existing UID """
    pass

class NotSupportedError(Exception):
    """ Raised if a method is called that the Mailbox interface demands,
        but that cannot be surported in IMAP
    """
    pass


class ImapServerWrapper:
    """ Wrapper around ImapServer, exposing all attributes,
        but only a few "safe" methods:
    """
    def __init__(self, server):
        if isinstance(server, ImapServer):
            self._server = server
        else:
            raise TypeError("server must be an instance of ImapServer.")
        for attribute in server.__dict__:
            try:
                self.__dict__[attribute] = server.__dict__[attribute]
            except KeyError:
                print "Can't import %s attribute into ImapServerWrapper" \
                      % attribute

        for method in ['create', 'list', 'lsub', 'subscribe',
                       'unsubscribe', 'uid', 'idle']:
            try:
                self.__class__.__dict__[method] = \
                                               server.__class__.__dict__[method]
            except:
                print "Can't import %s method into ImapServerWrapper" \
                      % method

    def __getattr__(self, name):
        if name in self._server.__dict__:
            return self._server.__dict__[name]
        raise AttributeError("ImapServerWrapper instance has no attribute %s" \
                              % name)


class ImapMailbox(Mailbox):
    """ An abstract representation of a mailbox on an IMAP Server.
        This class implements the mailbox.Mailbox interface, insofar
        possible. Methods for changing a message in-place are not
        available for IMAP; the 'update' and '__setitem__' methods
        will raise a NotSupportedError.

        The class specific attributes are:

        name             name of the mailbox
        server           ImapServer object
        trash            Trash folder
    """
    def __init__(self, path, factory=ImapMessage, create=True):
        """ Initialize an ImapMailbox
            path is a tuple with two elements, consisting of
            1) an instance of ImapServer in any state
            2) the name of a mailbox on the server as a string
            If the mailbox does not exist, it is created unless
            create is set to False, in which case NoSuchMailboxError
            is raised.
            The 'factory' parameter determines to which type the
            messages in the mailbox should be converted.

            Note that two instances of ImapMailbox must never
            share the same instance of server!
        """
        # not calling Mailbox.__init__(self) is on purpose:
        #  my definition of 'path' is incompatibel
        self._factory = factory
        try:
            (server, name) = path
        except:
            raise TypeError, "path must be a tuple, consisting of an "\
                             + " instance of ImapServer and a string"
        if isinstance(server, ImapServer):
            self._server = server
            self.server = ImapServerWrapper(server)
        else:
            raise TypeError, "path must be a tuple, consisting of an "\
                             + " instance of ImapServer and a string"
        if isinstance(name, str):
            self.name = name
        else:
            raise TypeError("path must be a tuple, consisting of an "\
                            + " instance of ImapServer and a string")
        try:
            self._server.select(self.name)
        except NoSuchMailboxError:
            if create:
                self._server.create(self.name)
                self._server.select(self.name)
            else:
                raise NoSuchMailboxError, "mailbox %s does not exist." \
                                           % self.name
        self._cached_uid = None
        self._cached_text = None
        self.trash = None

    def reconnect(self):
        """ Renew the connection to the mailbox """
        self._server.reconnect()
        self._server.login()
        try:
            try:
                self._server.select(self.name)
            except:
                # for some reason I have to do the whole thing twice if the
                # connection was really broken. I'm getting an exception the
                # first time. Not completely sure what's going on.
                self._server.reconnect()
                self._server.login()
                try:
                    self._server.select(self.name)
                except NoSuchMailboxError:
                    if create:
                        self._server.create(self.name)
                        self._server.select(self.name)
                    else:
                        raise NoSuchMailboxError, "mailbox %s does not exist." \
                                                % self.name
        finally:
            self.server = ImapServerWrapper(self._server)


    def switch(self, name, create=False):
        """ Switch to a different Mailbox on the same server """
        self.flush()
        if isinstance(name, str):
            self.name = name
        else:
            raise TypeError("name must be the name of a mailbox " \
                            + "as a string")
        try:
            self._server.select(self.name)
        except NoSuchMailboxError:
            if create:
                self._server.create(self.name)
                self._server.select(self.name)
            else:
                raise NoSuchMailboxError, "mailbox %s does not exist." \
                                           % self.name
        self._cached_uid = None
        self._cached_text = None

    def search(self, criteria='ALL', charset=None ):
        """ Return a list of all the UIDs in the mailbox (as integers)
            that match the search criteria. See documentation
            of imaplib and/or RFC3501 for details.
            Raise ImapNotOkError if a non-OK response is received from
            the server or if the response cannot be parsed into a list
            of integers.

            charset indicates the charset
            of the strings that appear in the search criteria.

            In all search keys that use strings, a message matches the key if
            the string is a substring of the field.  The matching is
            case-insensitive.

            The defined search keys are as follows.  Refer to RFC 3501 for detailed definitions of the
            arguments.

            <sequence set>
            ALL
            ANSWERED
            BCC <string>
            BEFORE <date>
            BODY <string>
            CC <string>
            DELETED
            DRAFT
            FLAGGED
            FROM <string>
            HEADER <field-name> <string>
            KEYWORD <flag>
            LARGER <n>
            NEW
            NOT <search-key>
            OLD
            ON <date>
            OR <search-key1> <search-key2>
            RECENT
            SEEN
            SENTBEFORE <date>
            SENTON <date>
            SENTSINCE <date>
            SINCE <date>
            SMALLER <n>
            SUBJECT <string>
            TEXT <string>
            TO <string>
            UID <sequence set>
            UNANSWERED
            UNDELETED
            UNDRAFT
            UNFLAGGED
            UNKEYWORD <flag>
            UNSEEN

        Example:    search('FLAGGED SINCE 1-Feb-1994 NOT FROM "Smith"')
                    search('TEXT "string not in mailbox"')
        """
        (code, data) = self._server.uid('search', charset, "(%s)" % criteria)
        uidlist = data[0].split()
        if code != 'OK':
            raise ImapNotOkError, "%s in search" % code
        try:
            return [int(uid) for uid in uidlist]
        except ValueError:
            raise ImapNotOkError, "received unparsable response."

    def get_unseen_uids(self):
        """ Get a list of all the unseen UIDs in the mailbox
            Equivalent to search(None, "UNSEEN UNDELETED")
        """
        return(self.search("UNSEEN UNDELETED"))

    def get_all_uids(self):
        """ Get a list of all the undeleted UIDs in the mailbox
            (as integers).
            Equivalent to search(None, "UNDELETED")
        """
        return(self.search("UNDELETED"))

    def _cache_message(self, uid):
        """ Download the RFC822 text of the message with UID and put
            in in the cache. Return the RFC822 text of the message.
            Raise KeyError if there if there is no message with that UID.
        """
        if self._cached_uid != uid:
            (code, data) = self._server.uid('fetch', uid, "(RFC822)")
            if code != 'OK':
                raise ImapNotOkError, "%s in fetch_message(%s)" % (code, uid)
            try:
                rfc822string = data[0][1]
                if FIX_BUGGY_IMAP_FROMLINE:
                    if rfc822string.startswith(">From "):
                        rfc822string = rfc822string[rfc822string.find("\n")+1:]
            except TypeError:
                raise KeyError, "No message %s in _cache_message" % uid
            self._cached_uid = uid
            self._cached_text = rfc822string
        return self._cached_text

    def get_message(self, uid):
        """ Return an ImapMessage object created from the message with UID.
            Raise KeyError if there if there is no message with that UID.
        """
        rfc822string = self._cache_message(uid)
        result = ImapMessage(rfc822string)
        result.set_imapflags(self.get_imapflags(uid))
        result.internaldate = self.get_internaldate(uid)
        result.size = self.get_size(uid)
        if self._factory is ImapMessage:
            return result
        return self._factory(result)

    def display(self, uid, pager=DEFAULT_PAGER, headerfields=None):
        """ Display a stripped down version of the message with UID in
            a pager. The displayed message will contain the the header
            fields set in 'headerfields', and the first BODY section
            (which is usually the human-readable part)

            headerfields defaults to ['Date', 'From', 'To', 'Subject']
            contrary to the declaration.
        """
        header = self.get_header(uid)
        result = ''
        # get body
        body = ''
        (code, data) = self._server.uid('fetch', uid, '(BODY[1])')
        if code == 'OK':
            body = data[0][1]
        # build result
        if headerfields is None:
            headerfields = ['Date', 'From', 'To', 'Subject']
        for field in headerfields:
            if header.has_key(field):
                result += "%s: %s\n" % (field, header[field])
        result += "\n"
        result += body
        _put_through_pager(result, pager)

    def summary(self, uids, printout=True, printuid=True):
        """ generates lines showing some basic information about the messages
            with the supplied uids. Non-existing UIDs in the list are
            silently ignored.

            If printout is True, the lines are printed out as they
            are generated, and the function returns nothing, otherwise,
            nothing is printed out and the function returns a list of
            generated lines.

            The summary contains
            1) an index (the uid if printuid=True)
            2) the from name (or from address), truncated
            3) the date of the message
            4) the subject, truncated

            Each line has the indicated fields truncated so that it is at
            most 79 characters wide.
        """
        counter = 0
        result = [] # array of lines
        if isinstance(uids, (str, int)):
            uids = [uids]
        for uid in uids:
            try:
                header = self.get_header(uid)
            except: # unspecified on purpose, might be ProcImap.imaplib2.error
                continue
            counter += 1
            index = counter
            if printuid:
                index = str(uid)
            index = "%2s" % index
            (from_name, address) = email.utils.parseaddr(header['From'])
            if from_name == '':
                from_name = address
            date = str(header['Date'])
            datetuple = email.utils.parsedate_tz(date)
            date = date[:16].ljust(16, " ")
            if datetuple is not None:
                date = "%02i/%02i/%04i %02i:%02i" \
                        % tuple([datetuple[i] for i in (1,2,0,3,4)])
            subject = str(header['Subject'])
            subject = ' '.join([s for (s, c) in \
                                email.header.decode_header(subject)])
            length_from = 25-len(index) # width of ...
            length_subject = 35         # ... truncated strings
            generated_line = "%s %s %s %s" \
                % (index,
                from_name[:length_from].ljust(length_from, " "),
                date,
                subject[:length_subject].ljust(length_subject, " "))
            if printout:
                print generated_line
            else:
                result.append(generated_line)
        if not printout:
            return result

    def __getitem__(self, uid):
        """ Return an ImapMessage object created from the message with UID.
            Raise KeyError if there if there is no message with that UID.
        """
        return self.get_message(uid)

    def get(self, uid, default=None):
        """ Return an ImapMessage object created from the message with UID.
            Return default if there is no message with that UID.
        """
        try:
            return self[uid]
        except KeyError:
            return default

    def get_string(self, uid):
        """ Return a RFC822 string representation of the message
            corresponding to key, or raise a KeyError exception if no
            such message exists.
        """
        return self._cache_message(uid)

    def get_file(self, uid):
        """ Return a cStringIO.StringIO of the message corresponding
            to key, or raise a KeyError exception if no such message
            exists.
        """
        return StringIO(self._cache_message(uid))

    def has_key(self, uid):
        """ Return True if key corresponds to a message, False otherwise.
        """
        return (uid in self.search('ALL'))

    def __contains__(self, uid):
        """ Return True if key corresponds to a message, False otherwise.
        """
        return self.has_key(uid)

    def __len__(self):
        """ Return a count of messages in the mailbox. """
        return len(self.search('ALL'))

    def clear(self):
        """ Delete all messages from the mailbox and expunge"""
        for uid in self.get_all_uids():
            self.discard(uid)
        self.expunge()

    def pop(self, uid, default=None):
        """ Return a representation of the message corresponding to key,
            delete and expunge the message. If no such message exists,
            return default if it was supplied (i.e. is not None) or else
            raise a KeyError exception. The message is represented as an
            instance of ImapMessage unless a custom message factory was
            specified when the Mailbox instance was initialized.
        """
        try:
            message = self[uid]
            del self[uid]
            self.expunge()
            return message
        except KeyError:
            if default is not None:
                return default
            else:
                raise KeyError, "No such UID"

    def popitem(self):
        """ Return an arbitrary (key, message) pair, where key is a key
            and message is a message representation, delete and expunge
            the corresponding message. If the mailbox is empty, raise a
            KeyError exception. The message is represented as an instance
            of ImapMessage unless a custom message factory was specified
            when the Mailbox instance was initialized.
        """
        self.expunge()
        uids = self.search("ALL")
        if len(uids) > 0:
            uid = uids[0]
            result = (uid, self[uid])
            del self[uid]
            self.expunge()
            return result
        else:
            raise KeyError, "Mailbox is empty"

    def update(self, arg=None):
        """ Parameter arg should be a key-to-message mapping or an iterable
            of (key, message) pairs. Updates the mailbox so that, for each
            given key and message, the message corresponding to key is set
            to message as if by using __setitem__().
            This operation is not supported for IMAP mailboxes and will
            raise NotSupportedError
        """
        raise NotSupportedError, "Updating items in IMAP not supported"


    def flush(self):
        """ Equivalent to expunge() """
        self.expunge()

    def lock(self):
        """ Do nothing """
        pass

    def unlock(self):
        """ Do nothing """
        pass

    def get_header(self, uid):
        """ Return an ImapMessage object containing only the Header
            of the message with UID.
            Raise KeyError if there if there is no message with that UID.
        """
        (code, data) = self._server.uid('fetch', uid, "(BODY.PEEK[HEADER])")
        if code != 'OK':
            raise ImapNotOkError, "%s in fetch_header(%s)" % (code, uid)
        try:
            rfc822string = data[0][1]
        except TypeError:
            raise KeyError, "No UID %s in get_header" % uid
        result = ImapMessage(rfc822string)
        result.set_imapflags(self.get_imapflags(uid))
        result.internaldate = self.get_internaldate(uid)
        result.size = self.get_size(uid)
        if self._factory is ImapMessage:
            return result
        return self._factory(result)

    def get_size(self, uid):
        """ Get the number of bytes contained in the message with UID """
        try:
            (code, data) = self._server.uid('fetch', uid, '(RFC822.SIZE)')
            sizeresult = data[0]
            if code != 'OK':
                raise ImapNotOkError, "%s in get_imapflags(%s)" % (code, uid)
            if sizeresult is None:
                raise NoSuchUIDError, "No message %s in get_size" % uid
            startindex = sizeresult.find('SIZE') + 5
            stopindex = sizeresult.find(' ', startindex)
            return int(sizeresult[startindex:stopindex])
        except (TypeError, ValueError):
            raise ValueError, "Unexpected results while fetching flags " \
                              + "from server for message %s" % uid

    def get_imapflags(self, uid):
        """ Return a list of imap flags for the message with UID
            Raise exception if there if there is no message with that UID.
        """
        try:
            (code, data) = self._server.uid('fetch', uid, '(FLAGS)')
            flagresult = data[0]
            if code != 'OK':
                raise ImapNotOkError, "%s in get_imapflags(%s)" % (code, uid)
            return list(imaplib.ParseFlags(flagresult))
        except (TypeError, ValueError):
            raise ValueError, "Unexpected results while fetching flags " \
                         + "from server for message %s; response was (%s, %s)" \
                                                             % (uid, code, data)

    def get_internaldate(self, uid):
        """ Return a time tuple representing the internal date for the
            message with UID
            Raise exception if there if there is no message with that UID.
        """
        try:
            (code, data) = self._server.uid('fetch', uid, '(INTERNALDATE)')
            dateresult = data[0]
            if code != 'OK':
                raise ImapNotOkError, "%s in get_internaldate(%s)" % (code, uid)
            if dateresult is None:
                raise NoSuchUIDError, "No message %s in get_internaldate" % uid
            return imaplib.Internaldate2tuple(dateresult)
        except (TypeError, ValueError):
            raise ValueError, "Unexpected results while fetching flags " \
                              + "from server for message %s" % uid


    def __eq__(self, other):
        """ Equality test:
            mailboxes are equal if they are equal in server and name
        """
        if not isinstance(other, ImapMailbox):
            return False
        return (    (self._server == other._server) \
                and (self.name == other.name) \
               )

    def __ne__(self, other):
        """ Inequality test:
            mailboxes are unequal if they are not equal
        """
        return (not (self == other))

    def copy(self, uid, targetmailbox):
        """ Copy the message with UID to the targetmailbox.
            targetmailbox can be a string (the name of a mailbox on the
            same imap server), any of mailbox.Mailbox. Note that not all
            imap flags will be preserved if the targetmailbox is not on
            an ImapMailbox. Copying is efficient (i.e. the message is not
            downloaded) if the targetmailbox is on the same server.
            Do nothing if there if there is no message with that UID.
        """
        if isinstance(targetmailbox, ImapMailbox):
            if targetmailbox._server == self._server:
                targetmailbox = targetmailbox.name # set as string
        if isinstance(targetmailbox, Mailbox):
            if self != targetmailbox:
                targetmailbox.lock()
                targetmailbox.add(self[uid])
                targetmailbox.flush()
                targetmailbox.unlock()
        elif isinstance(targetmailbox, str):
            if targetmailbox != self.name:
                (code, data) = self._server.uid('copy', uid, targetmailbox)
                if code != 'OK':
                    raise ImapNotOkError, "%s in copy: %s" % (code, data)
        else:
            raise TypeError, "targetmailbox in copy is of unknown type."



    def move(self, uid, targetmailbox):
        """ Copy the message with UID to the targetmailbox and delete it
            in the original mailbox
            targetmailbox can be a string (the name of a mailbox on the
            same imap server), an instance of ImapMailbox, or an instance
            of mailbox.Mailbox.
            Do nothing if there if there is no message with that UID.
        """
        self.copy(uid, targetmailbox)
        if (targetmailbox != self) and (targetmailbox != self.name):
            (code, data) = self._server.uid('store', uid, \
                                           '+FLAGS', "(\\Deleted)")
            if code != 'OK':
                raise ImapNotOkError, "%s in move: %s" % (code, data)

    def discard(self, uid):
        """ If trash folder is defined, move the message with UID to 
            trash; else, just add the \Deleted flag to the message with UID.
            Do nothing if there if there is no message with that UID.
        """
        if self.trash is None:
            self.add_imapflag(uid, "\\Deleted")
        else:
            print "Moving to %s" % self.trash
            self.move(uid, self.trash)

    def remove(self, uid):
        """ Discard the message with UID.
            If there is no message with that UID, raise a KeyError
        """
        if uid not in self.search("ALL"):
            raise KeyError, "No UID %s" % uid
        self.discard(uid)

    def __delitem__(self, uid):
        """ Add the \Deleted flag to the message with UID.
            If there is no message with that UID, raise a KeyError
        """
        self.remove(uid)

    def __setitem__(self, uid, message):
        """ Replace the message corresponding to key with message.
            This operation is not supported for IMAP mailboxes
            and will raise NotSupportedError
        """
        raise NotSupportedError, "Setting items in IMAP not supported"

    def iterkeys(self):
        """ Return an iterator over all UIDs
            This is an iterator over the list of UIDs at the time iterkeys()
            is a called.
        """
        return iter(self.search("ALL"))

    def keys(self):
        """ Return a list of all UIDs """
        return self.search(None, "ALL")

    def itervalues(self):
        """ Return an iterator over all messages. The messages are
            represented as instances of ImapMessage unless a custom message
            factory was specified when the Mailbox instance was initialized.
        """
        for uid in self.search("ALL"):
            yield self[uid]

    def __iter__(self):
        """ Return an iterator over all messages.
            Identical to itervalues
        """
        return self.itervalues()

    def values(self):
        """ Return a list of all messages
            The messages are represented as instances of ImapMessage unless
            a custom message factory was specified when the Mailbox instance
            was initialized.
            Beware that this method can be extremely expensive in terms
            of time, bandwidth, and memory.
        """
        messagelist = []
        for message in self:
            messagelist.append(message)
        return messagelist

    def iteritems(self):
        """ Return an iterator over (uid, message) pairs,
            where uid is a key and message is a message representation.
        """
        for uid in self.keys():
            yield((uid, self[uid]))

    def items(self):
        """ Return a list (uid, message) pairs,
            where uid is a key and message is a message representation.
            Beware that this method can be extremely expensive in terms
            of time, bandwidth, and memory.
        """
        result = []
        for uid in self.keys():
            result.append((uid, self[uid]))
        return result

    def add(self, message):
        """ Add the message to mailbox.
            Message can be an instance of email.Message.Message
            (including instaces of mailbox.Message and its subclasses );
            or an open file handle or a string containing an RFC822 message.
            Return the highest UID in the mailbox, which should be, but
            is not guaranteed to be, the UID of the message that was added.
            Raise ImapNotOkError if a non-OK response is received from
            the server
        """
        message = ImapMessage(message)
        flags = message.flagstring()
        date_time = message.internaldatestring()
        memoryfile = StringIO()
        generator = Generator(memoryfile, mangle_from_=False)
        generator.flatten(message)
        message_str = memoryfile.getvalue()
        (code, data) = self._server.append(self.name, flags, \
                                      date_time, message_str)
        if code != 'OK':
            raise ImapNotOkError, "%s in add: %s" % (code, data)
        return self.get_all_uids()[-1]


    def add_imapflag(self, uid, *flags):
        """ Add imap flag to message with UID.
        """
        for flag in flags:
            (code, data) = self._server.uid('store', uid, '+FLAGS', \
                                           "(%s)" % flag )
            if code != 'OK':
                raise ImapNotOkError, "%s in add_flags(%s, %s): %s" \
                                                       % (uid, flag, code, data)

    def remove_imapflag(self, uid, *flags):
        """ Remove imap flags from message with UID
        """
        for flag in flags:
            (code, data) = self._server.uid('store', uid, '-FLAGS', \
                                           "(%s)" % flag )
            if code != 'OK':
                raise ImapNotOkError, "%s in remove_flag(%s, %s): %s" \
                                                       % (uid, flag, code, data)

    def set_imapflags(self, uid, flags):
        """ Set imap flags for message with UID
            flags must be an iterable of flags, or a string.
            If flags is a string, it is taken as the single flag
            to be set.
        """
        if isinstance(flags, str):
            flags = [flags]
        flagstring = "(%s)" % ' '.join(flags)
        (code, data) = self._server.uid('store', uid, 'FLAGS', flagstring )
        if code != 'OK':
            raise ImapNotOkError, "%s in set_imapflags(%s, %s): %s" \
                                                      % (code, uid, flags, data)

    def close(self):
        """ Flush mailbox, close connection to server """
        self.flush()
        self._server.close()
        self._server.logout()

    def expunge(self):
        """ Expunge the mailbox"""
        self._server.expunge()


# Helper functions
def _put_through_pager(displaystring, pager='less'):
    """ Put displaystring through the 'less' pager """
    (temp_fd, tempname) = tempfile.mkstemp(".mail")
    temp_fh = os.fdopen(temp_fd, "w")
    temp_fh.write(displaystring)
    temp_fh.close()
    os.system("%s %s" % (pager, tempname))
    os.unlink(tempname)


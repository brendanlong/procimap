#!/usr/bin/env python
"""
    This example shows how to create a backup of an IMAP mailbox into an mbox folders.
    The IMAP attributes are stored in each message in special header fields"
"""
from ProcImap.ImapMailbox import ImapMailbox
from ProcImap.ImapServer import ImapServer
from mailbox import mbox
import sys

# usage: backup_mailbox.py imapmailbox backupmbox

server = ImapServer("mail.physik.fu-berlin.de", "goerz", "secret", ssl=True)
mailbox = ImapMailbox((server, sys.argv[1]))
backuptarget = mbox(sys.argv[2])

backuptarget.lock()

for message in mailbox:
    message.add_header("X-ProcImap-Imapflags", message.flagstring())
    message.add_header("X-ProcImap-ImapInternalDate", message.internaldatestring())
    backuptarget.add(message)

mailbox.close()
backuptarget.close()
sys.exit(0)